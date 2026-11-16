"""mail.com aliases managed by the Node mailcode adapter.

Only attributed local reservations may be allocated again automatically. Shared
release helpers should retain their metadata with state ``available``; deleting
a local record intentionally removes it from automatic reuse. Remote aliases
are retained throughout registration, cancellation, failure, and reauth.
"""

from __future__ import annotations

import os
import re
import secrets
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import BoundedSemaphore, Condition, RLock
from typing import Any
from urllib.parse import quote

import mail as mail_state
from mail import FastmailApiError, FastmailApiProvider, ICloudApiError, ICloudApiProvider
from mail.parsing import parse_fastmail_accounts

__all__ = ["MailcomApiProvider", "MailcomApiError"]

MAILCOM_API_DEFAULT_BASE = "http://127.0.0.1:18787"
MAILCOM_ALIAS_NAME_MODES = {"random", "name_digits"}
MAILCOM_ALIAS_NAME_LENGTH_MIN = 8
MAILCOM_ALIAS_NAME_LENGTH_MAX = 32
MAILCOM_FULL_NAME_LENGTH_MIN = 13
MAILCOM_ALIAS_LIMIT = 9
MAILCOM_ALIAS_GIVEN_NAMES = (
    "elena", "olivia", "amelia", "sophia", "isabella", "charlotte", "eleanor",
    "victoria", "elizabeth", "abigail", "natalie", "madeline", "gabriella",
    "josephine", "caroline", "genevieve", "penelope", "vivienne", "claire",
    "emily", "grace", "emma", "ella", "lily", "nora", "rose",
    "alexander", "benjamin", "christopher", "nathaniel", "sebastian",
    "theodore", "william", "frederick", "jonathan", "nicholas", "daniel",
    "matthew", "oliver", "samuel", "henry", "james", "jack", "liam", "noah",
)
MAILCOM_ALIAS_SURNAMES = (
    "parker", "morgan", "bennett", "carter", "wilson", "miller", "walker",
    "harris", "turner", "taylor", "anderson", "mitchell", "campbell", "collins",
    "edwards", "harrison", "lawrence", "sullivan", "thompson", "robinson",
    "hamilton", "richardson", "washington", "williams", "fletcher", "pearson",
    "spencer", "reynolds", "morrison", "reed", "cole", "hill", "wood", "king",
)
MAILCOM_ALIAS_NAMES = tuple(
    first + last for first in MAILCOM_ALIAS_GIVEN_NAMES for last in MAILCOM_ALIAS_SURNAMES
)
_ACTIVE_RESERVATIONS_LOCK = RLock()
_ACTIVE_RESERVATIONS: set[str] = set()
_ALIAS_CREATION_LOCK = RLock()
_ALIAS_CREATION_CLAIMS: set[str] = set()
_ALIAS_COLLISION_RETRIES = 8
_ACCOUNT_SCHEDULER_LOCK = RLock()
_ACCOUNT_ROTATION_CURSORS: dict[tuple[str, str, tuple[str, ...]], int] = {}
_ACCOUNT_COOLDOWN_UNTIL: dict[tuple[str, str], float] = {}
_ACCOUNT_EXHAUSTED: set[tuple[str, str]] = set()
_ACCOUNT_ALLOCATION_CLAIMS: set[tuple[str, str]] = set()
_ACCOUNT_ALLOCATION_CONDITION = Condition(_ACCOUNT_SCHEDULER_LOCK)
_ACCOUNT_FAILURE_COOLDOWN_SECONDS = 5 * 60.0
_ACCOUNT_ATTEMPTS_PER_ALLOCATION = 3
_ALIAS_CREATE_TIMEOUT_SECONDS = 40.0
# The job's user-selected concurrency remains authoritative up to the panel's
# supported maximum. Per-account locks in mailcode still serialize mutations
# for one primary mailbox while different mailboxes can make progress.
_ALLOCATION_SEMAPHORE = BoundedSemaphore(50)


def _stop_requested(stop_check: Any = None) -> bool:
    return bool(callable(stop_check) and stop_check())


@contextmanager
def _allocation_slot(stop_check: Any = None):
    """Acquire an allocation slot without making stop wait on the semaphore."""
    while not _ALLOCATION_SEMAPHORE.acquire(timeout=0.2):
        if _stop_requested(stop_check):
            raise MailcomApiError("cancelled")
    try:
        if _stop_requested(stop_check):
            raise MailcomApiError("cancelled")
        yield
    finally:
        _ALLOCATION_SEMAPHORE.release()


class MailcomApiError(ICloudApiError):
    """Invalid adapter response, alias ownership, or mailbox credentials."""


class MailcomApiProvider(FastmailApiProvider):
    name = "mailcom_api"

    def __init__(self, entry: dict, conf: dict):
        self.conf = conf
        self.provider_ref = str(entry.get("provider_ref") or "")
        self.label = str(entry.get("label") or self.provider_ref or "mail.com")
        self.api_base = str(
            entry.get("api_base")
            or os.environ.get("MAILCODE_API_URL")
            or MAILCOM_API_DEFAULT_BASE
        ).strip().rstrip("/") or MAILCOM_API_DEFAULT_BASE
        self.admin_key = self._resolve_admin_key(entry)
        self.accounts = parse_fastmail_accounts(str(entry.get("accounts") or ""))
        preferred = str(entry.get("preferred_account") or "").strip().lower()
        self.preferred_account = preferred if preferred in self.accounts else ""
        configured_domains = entry.get("alias_domains")
        if isinstance(configured_domains, str):
            configured_domains = re.split(r"[,\n\r]+", configured_domains)
        if isinstance(configured_domains, (list, tuple, set)):
            self.alias_domains = []
            for raw in configured_domains:
                domain = str(raw or "").strip().lower()
                if domain and domain != "random" and re.fullmatch(
                    r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}", domain
                ) and domain not in self.alias_domains:
                    self.alias_domains.append(domain)
        else:
            self.alias_domains = []
        self.alias_domain = str(
            entry.get("alias_domain") or entry.get("default_domain") or "random"
        ).strip().lower() or "random"
        if not self.alias_domains and self.alias_domain != "random":
            self.alias_domains = [self.alias_domain]
        self.default_domain = self.alias_domain
        alias_name_mode = str(entry.get("alias_name_mode") or "random").strip().lower()
        self.alias_name_mode = alias_name_mode if alias_name_mode in MAILCOM_ALIAS_NAME_MODES else "random"
        try:
            alias_name_length = int(entry.get("alias_name_length") or 17)
        except (TypeError, ValueError):
            alias_name_length = 17
        self.alias_name_length = max(
            MAILCOM_FULL_NAME_LENGTH_MIN if self.alias_name_mode == "name_digits" else MAILCOM_ALIAS_NAME_LENGTH_MIN,
            min(MAILCOM_ALIAS_NAME_LENGTH_MAX, alias_name_length),
        )
        self.message_limit = max(1, min(100, int(entry.get("message_limit") or 10)))
        self.session = mail_state._http_session()
        self._accounts_cache = None

    @classmethod
    def _resolve_admin_key(cls, entry: dict) -> str:
        env_name = str(entry.get("admin_key_env") or "MAILCODE_ADMIN_KEY").strip()
        # An explicitly chosen env var replaces the default; Fastmail's key is
        # never an implicit fallback for this independent adapter.
        for raw in (entry.get("admin_key"), os.environ.get(env_name)):
            token = cls._normalize_admin_key(raw)
            if token and token not in {env_name, "${" + env_name + "}"}:
                return token
        return ""

    def _admin_headers(self) -> dict[str, str]:
        if not self.admin_key:
            raise MailcomApiError(
                "mail.com admin key missing: set admin_key or MAILCODE_ADMIN_KEY "
                "(or select admin_key_env)"
            )
        return {
            "Accept": "application/json",
            "X-API-Key": self.admin_key,
            "User-Agent": self.conf["user_agent"],
        }

    def _safe_error(self, error: Any, *keys: str) -> str:
        text = str(error)
        for key in (self.admin_key, *keys):
            if key:
                text = text.replace(key, "***")
        return mail_state._redact_secrets(text)

    def _request_json(self, method: str, path: str, *, action: str, **kwargs: Any) -> dict:
        try:
            data = super()._request_json(method, path, action=action, **kwargs)
        except FastmailApiError as error:
            raise MailcomApiError(
                self._safe_error(error).replace("Fastmail", "mail.com")
            ) from None
        if not isinstance(data, dict):
            raise MailcomApiError(f"mail.com {action}: expected a JSON object")
        if data.get("ok") is False or data.get("success") is False:
            detail = data.get("error") or data.get("message") or "adapter error"
            raise MailcomApiError(f"mail.com {action}: {self._safe_error(detail)}")
        return data

    @staticmethod
    def _address(raw: Any) -> str:
        address = str(raw or "").strip().lower()
        if not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", address):
            raise MailcomApiError("mail.com requires a complete email address")
        return address

    def fetch_accounts(self, force: bool = False) -> list[dict[str, Any]]:
        cached = self._accounts_cache
        if cached is not None and not force and time.monotonic() - cached[0] < 30:
            return [dict(row) for row in cached[1]]
        data = self._request_json("GET", "/api/v1/accounts", action="accounts")
        if not isinstance(data.get("accounts"), list):
            raise MailcomApiError("mail.com accounts: expected accounts[]")
        rows = []
        seen = set()
        for row in data["accounts"]:
            if not isinstance(row, dict):
                raise MailcomApiError("mail.com accounts: expected account objects")
            email = self._address(row.get("email"))
            if email not in seen:
                seen.add(email)
                public = {field: row[field] for field in (
                    "enabled", "status", "hasPassword", "aliasCount", "aliasLimit", "createdAt", "updatedAt",
                    "proxy", "proxyAssigned", "proxyActive",
                ) if field in row and isinstance(row[field], (str, bool, int, float, type(None)))}
                rows.append(dict(public, email=email))
        self._accounts_cache = (time.monotonic(), rows)
        return [dict(row) for row in rows]

    def _lookup_accounts(self) -> list[str]:
        # Configured accounts are an ownership allowlist, not a hint to expand
        # with every account reachable using the admin key.
        return list(self.accounts) or [row["email"] for row in self.fetch_accounts()]

    def _candidate_accounts(self) -> tuple[list[str], bool]:
        return self._lookup_accounts(), not bool(self.accounts)

    def _rotated_accounts(self, accounts: list[str]) -> list[str]:
        """Choose a different first owner across short-lived provider instances."""
        if len(accounts) < 2:
            return list(accounts)
        key = (self.api_base, self.provider_ref, tuple(accounts))
        with _ACCOUNT_SCHEDULER_LOCK:
            start = _ACCOUNT_ROTATION_CURSORS.get(key, 0) % len(accounts)
            _ACCOUNT_ROTATION_CURSORS[key] = (start + 1) % len(accounts)
        rotated = accounts[start:] + accounts[:start]
        if self.preferred_account in rotated:
            return [self.preferred_account, *(item for item in rotated if item != self.preferred_account)]
        return rotated

    def _account_cooling_down(self, account: str) -> bool:
        key = (self.api_base, account)
        now = time.monotonic()
        with _ACCOUNT_SCHEDULER_LOCK:
            until = _ACCOUNT_COOLDOWN_UNTIL.get(key, 0.0)
            if until <= now:
                _ACCOUNT_COOLDOWN_UNTIL.pop(key, None)
                return False
            return True

    def _mark_account_cooldown(self, account: str) -> None:
        with _ACCOUNT_SCHEDULER_LOCK:
            _ACCOUNT_COOLDOWN_UNTIL[(self.api_base, account)] = (
                time.monotonic() + _ACCOUNT_FAILURE_COOLDOWN_SECONDS
            )

    def _clear_account_cooldown(self, account: str) -> None:
        with _ACCOUNT_SCHEDULER_LOCK:
            _ACCOUNT_COOLDOWN_UNTIL.pop((self.api_base, account), None)

    def _account_exhausted(self, account: str) -> bool:
        with _ACCOUNT_SCHEDULER_LOCK:
            return (self.api_base, account) in _ACCOUNT_EXHAUSTED

    def _mark_account_exhausted(self, account: str) -> None:
        with _ACCOUNT_SCHEDULER_LOCK:
            _ACCOUNT_EXHAUSTED.add((self.api_base, account))

    def _clear_account_exhausted(self, account: str) -> None:
        with _ACCOUNT_SCHEDULER_LOCK:
            _ACCOUNT_EXHAUSTED.discard((self.api_base, account))

    def _claim_account_allocation(self, account: str, stop_check: Any = None) -> bool:
        key = (self.api_base, account)
        with _ACCOUNT_ALLOCATION_CONDITION:
            while key in _ACCOUNT_ALLOCATION_CLAIMS:
                if _stop_requested(stop_check):
                    return False
                _ACCOUNT_ALLOCATION_CONDITION.wait(timeout=0.15)
            if _stop_requested(stop_check):
                return False
            _ACCOUNT_ALLOCATION_CLAIMS.add(key)
            return True

    def _release_account_allocation(self, account: str) -> None:
        with _ACCOUNT_ALLOCATION_CONDITION:
            _ACCOUNT_ALLOCATION_CLAIMS.discard((self.api_base, account))
            _ACCOUNT_ALLOCATION_CONDITION.notify_all()

    @staticmethod
    def _quota_exhausted(error: Exception) -> bool:
        value = str(error).lower()
        return any(marker in value for marker in (
            "别名名额已满", "最多创建 9", "最多 9 个别名",
            "alias limit", "alias quota", "maximum number of alias",
        ))

    @staticmethod
    def _account_auth_failure(error: MailcomApiError) -> bool:
        value = str(error).lower()
        return (
            "登录失败" in value
            or "login failed" in value
            or "account verification" in value
            or "complete account verification" in value
            or "http 401" in value
            or "http 403" in value
            or ("http 502" in value and any(
                marker in value for marker in ("alias domains", "alias-domain", "upstream", "验证")
            ))
        )

    def import_account(self, email: str, password: str) -> None:
        """Import into the adapter; a later domain-login failure needs no rollback.

        Login can outlast the ordinary ten-second management request timeout.
        Invalidate discovery even on error, since the adapter may have saved the
        account before attempting authentication. The password is never cached.
        """
        account = self._address(email)
        if not isinstance(password, str) or not password:
            raise MailcomApiError("mail.com account password is required")
        try:
            data = self._request_json(
                "POST", "/api/v1/import", action="import account",
                json={"accounts": [{"email": account, "password": password}]},
                timeout=max(120.0, float(self.conf["request_timeout"])),
            )
            errors = data.get("errors")
            if isinstance(errors, list):
                for item in errors:
                    if not isinstance(item, dict):
                        continue
                    failed_email = str(item.get("email") or "").strip().lower()
                    if failed_email in {account, ""}:
                        detail = item.get("error") or item.get("message") or "adapter rejected account"
                        raise MailcomApiError(f"mail.com import account: {self._safe_error(detail, password)}")
        except MailcomApiError as error:
            raise MailcomApiError(self._safe_error(error, password)) from None
        finally:
            self._accounts_cache = None
            self._clear_account_exhausted(account)

    def delete_account(self, email: str) -> dict[str, Any]:
        """Remove one primary mailbox from this adapter's local store."""
        account = self._address(email)
        try:
            result = self._request_json(
                "DELETE",
                f"/api/v1/accounts/{quote(account, safe='')}",
                action="delete account",
            )
            self._clear_account_exhausted(account)
            return result
        finally:
            self._accounts_cache = None

    def fetch_proxy_pool(self) -> dict[str, Any]:
        data = self._request_json("GET", "/api/v1/proxy-pool", action="proxy pool")
        value = data.get("proxy_pool")
        if not isinstance(value, dict):
            raise MailcomApiError("mail.com proxy pool: expected proxy_pool object")
        items = value.get("items")
        if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
            raise MailcomApiError("mail.com proxy pool: expected items[]")
        return {
            "enabled": value.get("enabled") is True,
            "count": max(0, int(value.get("count") or 0)),
            "assigned": max(0, int(value.get("assigned") or 0)),
            "generation": max(0, int(value.get("generation") or 0)),
            "items": [str(item) for item in items],
        }

    def save_proxy_pool(
        self,
        *,
        enabled: bool,
        proxies: list[str] | None = None,
        reassign: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"enabled": bool(enabled), "reassign": bool(reassign)}
        if proxies is not None:
            body["proxies"] = [str(item or "").strip() for item in proxies if str(item or "").strip()]
        data = self._request_json("PUT", "/api/v1/proxy-pool", action="save proxy pool", json=body)
        self._accounts_cache = None
        value = data.get("proxy_pool")
        if not isinstance(value, dict):
            raise MailcomApiError("mail.com save proxy pool: expected proxy_pool object")
        return self.fetch_proxy_pool()

    def available_domains(self, account_email: str) -> list[str]:
        account = self._address(account_email)
        data = self._request_json(
            "GET", "/api/v1/alias-domains", action="alias domains",
            params={"account_email": account},
            timeout=max(30.0, float(self.conf["request_timeout"])),
        )
        values = data.get("domains")
        if not isinstance(values, list):
            raise MailcomApiError("mail.com alias domains: expected domains[]")
        domains = []
        for value in values:
            domain = str(value or "").strip().lower()
            if (
                not isinstance(value, str)
                or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}", domain)
            ):
                raise MailcomApiError("mail.com alias domains: invalid domain")
            if domain not in domains:
                domains.append(domain)
        return domains

    def fetch_alias_domains(self, account: str) -> list[str]:
        return self.available_domains(account)

    def _aliases(self, account: str) -> list[dict[str, Any]]:
        data = self._request_json(
            "GET", "/api/v1/aliases", action="aliases",
            params={"account_email": account},
        )
        items = data.get("items")
        if not isinstance(items, list) or any(not isinstance(row, dict) for row in items):
            raise MailcomApiError("mail.com aliases: expected items[]")
        return items

    def fetch_alias_inventory(self) -> list[dict[str, Any]]:
        """Read the adapter's encrypted local alias store without an SDK login."""
        data = self._request_json(
            "GET", "/api/v1/alias-inventory", action="local alias inventory",
        )
        items = data.get("items")
        if not isinstance(items, list) or any(not isinstance(row, dict) for row in items):
            raise MailcomApiError("mail.com local alias inventory: expected items[]")
        rows: list[dict[str, Any]] = []
        for item in items:
            try:
                address = self._address(item.get("address"))
                account = self._address(item.get("primaryEmail"))
            except MailcomApiError:
                continue
            if address == account:
                continue
            rows.append({
                "address": address,
                "primaryEmail": account,
                "status": str(item.get("status") or "active").strip().lower(),
                "hasKey": item.get("hasKey") is True,
            })
        return rows

    def alias_usage_summaries(
        self,
        items: list[dict[str, Any]],
        *,
        accounts: list[str] | None = None,
        state: dict[str, dict[str, Any]] | None = None,
        bindings: dict[str, str] | None = None,
    ) -> dict[str, dict[str, int]]:
        owners = [self._address(value) for value in (accounts or self._lookup_accounts())]
        summaries = {
            owner: {"total": 0, "bound": 0, "unbound": 0, "available": 0, "quarantined": 0, "uncertain": 0}
            for owner in owners
        }
        bindings = bindings if bindings is not None else mail_state._known_mailbox_bindings()
        store = state if state is not None else mail_state._load_state()
        for item in items:
            owner = str(item.get("primaryEmail") or "").strip().lower()
            summary = summaries.get(owner)
            if summary is None:
                continue
            try:
                address = self._address(item.get("address"))
            except MailcomApiError:
                continue
            if address == owner or str(item.get("status") or "").strip().lower() == "deleted":
                continue
            status = str(item.get("status") or "active").strip().lower()
            summary["total"] += 1
            if address in bindings:
                summary["bound"] += 1
                continue
            summary["unbound"] += 1
            if status in {"pending", "unconfirmed"}:
                summary["uncertain"] += 1
            local_entry = store.get(address)
            reusable = status == "active" and item.get("hasKey") is True and (
                local_entry is None
                or (
                    self._locally_owned(address, local_entry, [owner])
                    and self._available(address, local_entry)
                )
            )
            if reusable:
                summary["available"] += 1
            else:
                summary["quarantined"] += 1
        return summaries

    def list_aliases(self, account_email: str) -> list[dict[str, Any]]:
        account = self._address(account_email)
        bindings = mail_state._known_mailbox_bindings()
        rows: list[dict[str, Any]] = []
        for item in self._aliases(account):
            try:
                address = self._address(item.get("address"))
            except MailcomApiError:
                continue
            if address == account or str(item.get("primaryEmail") or "").strip().lower() != account:
                continue
            rows.append({
                "address": address,
                "status": str(item.get("status") or "active").strip().lower(),
                "bound": address in bindings,
                "created_at": item.get("createdAt") or item.get("created_at") or "",
                "deletable": True,
            })
        return rows

    def delete_aliases(self, account_email: str, addresses: list[str] | None = None) -> dict[str, Any]:
        account = self._address(account_email)
        payload: dict[str, Any] = {"account_email": account}
        if addresses is None:
            payload["all"] = True
        else:
            payload["addresses"] = [self._address(value) for value in addresses]
        result = self._request_json(
            "DELETE", "/api/v1/aliases", action="delete aliases", json=payload,
            # A full mailbox runs up to nine SDK deletions, each involving
            # login/listing/deletion/verification. A short management timeout
            # reports failure while the adapter is still deleting remotely.
            timeout=max(900.0, float(self.conf["request_timeout"])),
        )
        self._clear_account_exhausted(account)
        return result

    def alias_usage_summary(
        self,
        account_email: str,
        *,
        state: dict[str, dict[str, Any]] | None = None,
        bindings: dict[str, str] | None = None,
    ) -> dict[str, int]:
        account = self._address(account_email)
        items = []
        for item in self._aliases(account):
            row = dict(item)
            row["hasKey"] = bool(item.get("key"))
            items.append(row)
        return self.alias_usage_summaries(
            items, accounts=[account], state=state, bindings=bindings,
        )[account]

    def _validated_alias(
        self, item: Any, account: str, *, address: str = "", domain: str = ""
    ) -> dict[str, str]:
        if not isinstance(item, dict):
            raise MailcomApiError("mail.com alias response missing item")
        actual = self._address(item.get("address"))
        owner = str(item.get("primaryEmail") or "").strip().lower()
        key = str(item.get("key") or "").strip()
        if owner != account:
            raise MailcomApiError(f"mail.com alias owner mismatch: {actual}")
        if actual == account or (address and actual != address):
            raise MailcomApiError("mail.com alias address mismatch")
        if domain and actual.rsplit("@", 1)[1] != domain:
            raise MailcomApiError("mail.com alias domain mismatch")
        if str(item.get("status") or "").strip().lower() != "active":
            raise MailcomApiError(f"mail.com alias is not active: {actual}")
        if not key:
            raise MailcomApiError(f"mail.com alias missing key: {actual}")
        return {"address": actual, "account": account, "key": key}

    def _check_state_owner(self, address: str, entry: dict, account: str = "") -> None:
        address = self._address(address)
        expected = {"provider": self.name, "provider_ref": self.provider_ref,
                    "api_base": self.api_base}
        if account:
            expected["account"] = account
        expected["domain"] = address.rsplit("@", 1)[1]
        for field, value in expected.items():
            if field not in entry:
                continue
            actual = str(entry[field] or "").strip()
            if field == "api_base":
                actual = actual.rstrip("/")
            elif field in {"account", "domain"}:
                actual = actual.lower()
            if actual != value:
                raise MailcomApiError(f"mail.com local reservation {field} mismatch: {address}")

    def _locally_owned(self, address: str, entry: dict, accounts: list[str]) -> bool:
        if not all(field in entry for field in ("provider", "provider_ref", "api_base", "account")):
            return False
        account = str(entry.get("account") or "").strip().lower()
        if account not in accounts:
            return False
        try:
            self._check_state_owner(address, entry, account)
        except MailcomApiError:
            return False
        return True

    @classmethod
    def _reservation_is_active(cls, address: str, entry: dict) -> bool:
        with _ACTIVE_RESERVATIONS_LOCK:
            if address not in _ACTIVE_RESERVATIONS:
                return False
            if str(entry.get("state") or "") != "in_use":
                _ACTIVE_RESERVATIONS.discard(address)
                return False
            return True

    @staticmethod
    def _mark_reservation_active(address: str) -> None:
        with _ACTIVE_RESERVATIONS_LOCK:
            _ACTIVE_RESERVATIONS.add(address)

    def _available(self, address: str, entry: dict) -> bool:
        if not entry:
            return True
        if (
            entry.get("state") not in {"available", "in_use", "cooldown"}
            or entry.get("_prev_state") in {"used", "bound"}
        ):
            return False
        if mail_state._entry_available(entry):
            return True
        # mail.com is a rebind-only provider. A process restart can leave an
        # unclaimed preflight reservation as ``in_use`` even though the remote
        # alias is still active and unbound. Reclaim those orphaned rows, while
        # keeping addresses reserved by this process locked until release.
        return (
            str(entry.get("state") or "") == "in_use"
            and str(entry.get("provider") or "") == self.name
            and str(entry.get("reason") or "") != "rebind_begin_accepted"
            and not self._reservation_is_active(address, entry)
        )

    def find_alias(self, address: str) -> dict[str, str] | None:
        target = self._address(address)
        previous = self._state_entry(target)
        self._check_state_owner(target, previous)
        accounts = self._lookup_accounts()
        owner = str(previous.get("account") or "").strip().lower()
        if owner:
            if owner not in accounts:
                raise MailcomApiError(f"mail.com account outside configured accounts: {owner}")
            accounts = [owner]
        for account in accounts:
            for item in self._aliases(account):
                if str(item.get("address") or "").strip().lower() == target:
                    return self._validated_alias(item, account, address=target)
        return None

    def _reserve_address(
        self, address: str, *, account: str, alias_key: str,
        allow_used: bool = False, local_only: bool = False,
    ) -> None:
        target = self._address(address)
        bound = target in mail_state._known_mailbox_bindings()
        with mail_state._mailbox_state_lock:
            with mail_state._with_state_file_lock():
                store = mail_state._load_state_unlocked()
                entry = store.get(target) or {}
                self._check_state_owner(target, entry, account)
                if local_only and not self._locally_owned(target, entry, [account]):
                    raise MailcomApiError("mail.com local reservation changed during allocation")
                if local_only and not self._available(target, entry):
                    raise MailcomApiError("mail.com local reservation is no longer available")
                state = str(entry.get("state") or "")
                used = bound or state in {"used", "bound"} or entry.get("_prev_state") in {"used", "bound"}
                available = self._available(target, entry)
                if ((used and not allow_used)
                        or not (available or (allow_used and state in {"used", "bound"}))):
                    raise MailcomApiError(f"mail.com mailbox unavailable: {target} ({state or 'bound'})")
                store[target] = {
                    **entry,
                    "state": "in_use", "reason": "",
                    "_prev_state": "used" if used else state,
                    "updated_at": datetime.now(mail_state.BJ_TZ).strftime("%Y-%m-%d %H:%M:%S%z"),
                    "provider": self.name, "provider_ref": self.provider_ref,
                    "label": self.label, "api_base": self.api_base,
                    "account": account, "key": alias_key,
                    "domain": target.rsplit("@", 1)[1],
                }
                mail_state._save_state_unlocked(store)
                self._mark_reservation_active(target)

    def _generated_alias_address(self, domain: str) -> str:
        length = self.alias_name_length
        if self.alias_name_mode == "name_digits":
            names = [name for name in MAILCOM_ALIAS_NAMES if len(name) <= length - 5]
            # Keep both names whole, and favour readable letters over a long
            # number tail. At least five unpredictable digits always remain.
            preferred_min = min(length - 8, max(map(len, names)))
            names = [name for name in names if len(name) >= preferred_min]
            name = secrets.choice(names)
            local_part = name + "".join(
                secrets.choice("0123456789") for _ in range(length - len(name))
            )
        else:
            local_part = "m" + secrets.token_hex((length - 1 + 1) // 2)[: length - 1]
        return self._address(f"{local_part}@{domain}")

    @staticmethod
    def _alias_address_collision(error: MailcomApiError) -> bool:
        # A 409 can also mean all nine slots are full or a create is uncertain.
        # Only an explicit address collision is safe to retry with a new name.
        text = str(error).lower()
        if "http 409:" not in text or any(part in text for part in ("等待确认", "pending", "unconfirmed")):
            return False
        detail = text.split("http 409:", 1)[1]
        return any(part in detail for part in (
            "该别名已存在", "该别名地址已被占用", "主邮箱地址已存在",
            "alias already exists", "alias address is not available",
        ))

    def _claim_generated_address(self, domain: str, known: set[str]) -> str:
        # No network calls under this lock: independent primary mailboxes can
        # still create in parallel, but never submit the same generated address.
        with _ALIAS_CREATION_LOCK:
            known.update(mail_state._load_state())
            for _ in range(64):
                address = self._generated_alias_address(domain)
                if address not in known and address not in _ALIAS_CREATION_CLAIMS:
                    _ALIAS_CREATION_CLAIMS.add(address)
                    known.add(address)
                    return address
        raise MailcomApiError("mail.com 连续生成重复候选地址，请稍后重试")

    def _create_generated_mailbox(
        self,
        account: str,
        domain: str,
        known: set[str],
        *,
        skip_domain_check: bool = False,
        stop_check: Any = None,
    ) -> dict[str, Any]:
        for _ in range(_ALIAS_COLLISION_RETRIES):
            if _stop_requested(stop_check):
                raise MailcomApiError("cancelled")
            address = self._claim_generated_address(domain, known)
            try:
                body = {"account_email": account, "domain": domain, "address": address}
                if skip_domain_check:
                    body["skip_domain_check"] = True
                try:
                    data = self._request_json(
                        "POST", "/api/v1/aliases", action="create alias",
                        json=body, timeout=_ALIAS_CREATE_TIMEOUT_SECONDS,
                    )
                    if _stop_requested(stop_check):
                        raise MailcomApiError("cancelled")
                except MailcomApiError as error:
                    if self._alias_address_collision(error):
                        continue
                    raise
                alias = self._validated_alias(data.get("item"), account, address=address, domain=domain)
                self._reserve_address(address, account=account, alias_key=alias["key"])
                return self._mailbox_payload(address, account, alias["key"])
            finally:
                with _ALIAS_CREATION_LOCK:
                    _ALIAS_CREATION_CLAIMS.discard(address)
        raise MailcomApiError("mail.com 候选别名连续被占用，已尝试新名称，请稍后重试")

    def create_mailbox(
        self,
        username: str | None = None,
        *,
        allow_used: bool = False,
        skip_domain_check: bool = False,
        stop_check: Any = None,
    ) -> dict[str, Any]:
        if _stop_requested(stop_check):
            raise MailcomApiError("cancelled")
        requested = self._address(username) if str(username or "").strip() else ""
        if requested:
            found = self.find_alias(requested)
            if found:
                self._reserve_address(
                    requested, account=found["account"], alias_key=found["key"], allow_used=allow_used,
                )
                return self._mailbox_payload(requested, found["account"], found["key"])
            # A recorded or bound address must be recovered explicitly, never
            # recreated after an uncertain rebind or an incomplete lookup.
            if self._state_entry(requested) or requested in mail_state._known_mailbox_bindings():
                raise MailcomApiError(f"mail.com managed alias missing upstream: {requested}")

        accounts = self._lookup_accounts()
        if not requested:
            accounts = self._rotated_accounts(accounts)
        store = mail_state._load_state()
        bindings = mail_state._known_mailbox_bindings()
        known_addresses = set(store) | set(bindings) | set(accounts)
        inventory_summaries: dict[str, dict[str, int]] = {}
        try:
            inventory_summaries = self.alias_usage_summaries(
                self.fetch_alias_inventory(),
                accounts=accounts,
                state=store,
                bindings=bindings,
            )
        except MailcomApiError:
            # An older adapter may not expose the bulk local inventory yet.
            # Per-account local alias reads below remain the compatibility path.
            inventory_summaries = {}
        if not requested and inventory_summaries:
            order = {account: index for index, account in enumerate(accounts)}
            accounts = sorted(accounts, key=lambda account: (
                0 if (
                    inventory_summaries.get(account, {}).get("available", 0) > 0
                    or inventory_summaries.get(account, {}).get("uncertain", 0) > 0
                ) else 1 if inventory_summaries.get(account, {}).get("total", 0) < MAILCOM_ALIAS_LIMIT else 2,
                order[account],
            ))
        errors = []
        cooling_accounts = 0
        full_accounts = 0
        busy_accounts = 0
        attempted_accounts = 0
        with _allocation_slot(stop_check):
            for account in accounts:
                if _stop_requested(stop_check):
                    raise MailcomApiError("cancelled")
                if self._account_cooling_down(account):
                    cooling_accounts += 1
                    continue
                summary = inventory_summaries.get(account, {})
                can_reuse_locally = (
                    not requested
                    and (
                        int(summary.get("available") or 0) > 0
                        or int(summary.get("uncertain") or 0) > 0
                    )
                )
                if summary and not can_reuse_locally and (
                    int(summary.get("total") or 0) >= MAILCOM_ALIAS_LIMIT
                    or self._account_exhausted(account)
                ):
                    self._mark_account_exhausted(account)
                    full_accounts += 1
                    continue
                if not self._claim_account_allocation(account, stop_check):
                    raise MailcomApiError("cancelled")
                try:
                    # Another worker may have put this account into cooldown
                    # while this worker was waiting for the process-level
                    # allocation claim.
                    if self._account_cooling_down(account):
                        cooling_accounts += 1
                        continue
                    wanted = requested.rsplit("@", 1)[1] if requested else ""
                    # Reuse an existing active alias before querying the domain
                    # endpoint. The adapter may temporarily time out there even
                    # while its alias list is available, and reuse does not need
                    # domain discovery.
                    reusable_domains = [wanted] if wanted else list(self.alias_domains)
                    if not requested:
                        uncertain_aliases: list[dict[str, Any]] = []
                        if _stop_requested(stop_check):
                            raise MailcomApiError("cancelled")
                        try:
                            remote_aliases = self._aliases(account)
                        except MailcomApiError:
                            remote_aliases = []
                        known_addresses.update(
                            str(item.get("address") or "").strip().lower() for item in remote_aliases
                        )
                        for item in remote_aliases:
                            status = str(item.get("status") or "active").strip().lower()
                            if status in {"pending", "unconfirmed"}:
                                try:
                                    uncertain_address = self._address(item.get("address"))
                                except MailcomApiError:
                                    continue
                                if str(item.get("primaryEmail") or "").strip().lower() != account:
                                    continue
                                if reusable_domains and uncertain_address.rsplit("@", 1)[1] not in reusable_domains:
                                    continue
                                if uncertain_address in bindings:
                                    continue
                                uncertain_entry = store.get(uncertain_address)
                                if uncertain_entry is not None and (
                                    not self._locally_owned(uncertain_address, uncertain_entry, [account])
                                    or not self._available(uncertain_address, uncertain_entry)
                                ):
                                    continue
                                uncertain_aliases.append({"address": uncertain_address})
                                continue
                            try:
                                alias = self._validated_alias(item, account)
                            except MailcomApiError:
                                continue
                            address = alias["address"]
                            if reusable_domains and address.rsplit("@", 1)[1] not in reusable_domains:
                                continue
                            if address in bindings:
                                continue
                            local_entry = store.get(address)
                            local_only = local_entry is not None
                            if local_only and (
                                not self._locally_owned(address, local_entry, [account])
                                or not self._available(address, local_entry)
                            ):
                                continue
                            try:
                                self._reserve_address(
                                    address,
                                    account=account,
                                    alias_key=alias["key"],
                                    local_only=local_only,
                                )
                            except MailcomApiError:
                                continue
                            self._clear_account_cooldown(account)
                            self._clear_account_exhausted(account)
                            return self._mailbox_payload(address, account, alias["key"])
                        # A timed-out create may have reached mail.com even
                        # though the old Python client never received its
                        # response. Ask the adapter to reconcile that exact
                        # reserved address before consuming another alias slot.
                        if uncertain_aliases:
                            if _stop_requested(stop_check):
                                raise MailcomApiError("cancelled")
                            if attempted_accounts >= _ACCOUNT_ATTEMPTS_PER_ALLOCATION:
                                continue
                            address = uncertain_aliases[0]["address"]
                            domain = address.rsplit("@", 1)[1]
                            data = self._request_json(
                                "POST", "/api/v1/aliases", action="reconcile alias",
                                json={
                                    "account_email": account,
                                    "domain": domain,
                                    "address": address,
                                    "skip_domain_check": True,
                                },
                                timeout=_ALIAS_CREATE_TIMEOUT_SECONDS,
                            )
                            if _stop_requested(stop_check):
                                raise MailcomApiError("cancelled")
                            alias = self._validated_alias(
                                data.get("item"), account, address=address, domain=domain,
                            )
                            self._reserve_address(address, account=account, alias_key=alias["key"])
                            self._clear_account_cooldown(account)
                            self._clear_account_exhausted(account)
                            return self._mailbox_payload(address, account, alias["key"])
                        local_alias_count = sum(
                            1 for item in remote_aliases
                            if str(item.get("primaryEmail") or "").strip().lower() == account
                            and str(item.get("address") or "").strip().lower() != account
                            and str(item.get("status") or "active").strip().lower() != "deleted"
                        )
                        if local_alias_count >= MAILCOM_ALIAS_LIMIT:
                            self._mark_account_exhausted(account)
                            full_accounts += 1
                            continue
                    if self._account_exhausted(account):
                        full_accounts += 1
                        continue
                    if attempted_accounts >= _ACCOUNT_ATTEMPTS_PER_ALLOCATION:
                        continue
                    # A configured/explicit domain is already the operator's
                    # selection.  Skip the extra alias-domains login and let
                    # the create endpoint validate it.  Random mode still
                    # needs discovery to choose a domain.
                    direct_domain = bool(
                        skip_domain_check
                        and (wanted or self.alias_domains or self.alias_domain != "random")
                    )
                    if direct_domain:
                        if wanted:
                            domains = [wanted]
                        elif self.alias_domains:
                            domains = list(self.alias_domains)
                        else:
                            domains = [self.alias_domain]
                    else:
                        available_domains = self.fetch_alias_domains(account)
                        if wanted:
                            domains = [wanted] if wanted in available_domains else []
                        elif self.alias_domains:
                            domains = [domain for domain in self.alias_domains if domain in available_domains]
                        else:
                            domains = available_domains
                    if not domains:
                        raise MailcomApiError(f"mail.com no supported alias domain for {account}: {wanted}")
                    domain = secrets.choice(domains)
                    body = {"account_email": account, "domain": domain}
                    if direct_domain:
                        body["skip_domain_check"] = True
                    if requested:
                        body["address"] = requested
                    elif self.alias_name_mode != "random" or self.alias_name_length != 17:
                        result = self._create_generated_mailbox(
                            account,
                            domain,
                            known_addresses,
                            skip_domain_check=direct_domain,
                            stop_check=stop_check,
                        )
                        self._clear_account_cooldown(account)
                        self._clear_account_exhausted(account)
                        return result
                    data = self._request_json(
                        "POST", "/api/v1/aliases", action="create alias",
                        json=body, timeout=_ALIAS_CREATE_TIMEOUT_SECONDS,
                    )
                    if _stop_requested(stop_check):
                        raise MailcomApiError("cancelled")
                    alias = self._validated_alias(
                        data.get("item"), account, address=str(body.get("address") or ""), domain=domain
                    )
                    # allow_used is restricted to explicit lookups, never random
                    # allocation or an unexpected existing address from POST.
                    self._reserve_address(alias["address"], account=account, alias_key=alias["key"])
                    self._clear_account_cooldown(account)
                    self._clear_account_exhausted(account)
                    return self._mailbox_payload(alias["address"], account, alias["key"])
                except Exception as error:
                    # A broken primary mailbox must be isolated to this
                    # candidate.  Adapter/library versions have historically
                    # raised both MailcomApiError and plain transport errors;
                    # normalize both here so the next configured account is
                    # always attempted.
                    if _stop_requested(stop_check):
                        raise MailcomApiError("cancelled") from error
                    normalized = error if isinstance(error, MailcomApiError) else MailcomApiError(str(error))
                    if self._quota_exhausted(normalized):
                        self._mark_account_exhausted(account)
                        full_accounts += 1
                        continue
                    attempted_accounts += 1
                    self._mark_account_cooldown(account)
                    errors.append(str(normalized))
                finally:
                    self._release_account_allocation(account)
        detail = "; ".join(errors[-3:])
        if full_accounts and not errors and not cooling_accounts and not busy_accounts:
            raise MailcomApiError("mail.com 全部主邮箱别名已满（每个最多 9 个）")
        if cooling_accounts:
            detail = f"{detail}; " if detail else ""
            detail += f"mail.com 已临时跳过 {cooling_accounts} 个失败账号"
        if attempted_accounts >= _ACCOUNT_ATTEMPTS_PER_ALLOCATION and attempted_accounts < len(accounts):
            detail = f"{detail}; " if detail else ""
            detail += f"本次已尝试 {attempted_accounts} 个主邮箱，请由后续任务继续轮换"
        if full_accounts:
            detail = f"{detail}; " if detail else ""
            detail += f"mail.com 已跳过 {full_accounts} 个别名已满的主邮箱"
        if busy_accounts:
            detail = f"{detail}; " if detail else ""
            detail += f"mail.com 有 {busy_accounts} 个主邮箱正由其他任务分配"
        raise MailcomApiError(detail or "mail.com has no configured adapter accounts")

    def pool_summary(self, *, force: bool = False) -> dict[str, Any]:
        """Report local reservations only; the adapter exposes no pool capacity."""
        try:
            if force:
                self._accounts_cache = None
            accounts = self._lookup_accounts()
            bindings = mail_state._known_mailbox_bindings()
            rows = [
                (address, entry) for address, entry in mail_state._load_state().items()
                if self._locally_owned(address, entry, accounts)
                and (
                    not self.alias_domains
                    or address.rsplit("@", 1)[1] in self.alias_domains
                )
            ]
            return {"accounts": len(accounts), "total": len(rows), "error": "",
                    "available": sum(self._available(address, entry) and address not in bindings for address, entry in rows)}
        except MailcomApiError as error:
            return {"accounts": 0, "total": 0, "available": 0, "error": str(error)}

    def _alias_credentials(self, mailbox: dict[str, Any]) -> tuple[str, str, str]:
        address = self._address(mailbox.get("address"))
        self._check_state_owner(address, mailbox)
        for field in ("login_address", "base_address"):
            if mailbox.get(field) and self._address(mailbox[field]) != address:
                raise MailcomApiError("mail.com mailbox address mismatch")
        entry = self._state_entry(address)
        self._check_state_owner(address, entry)
        accounts = self._lookup_accounts()
        if self._locally_owned(address, entry, accounts) and entry.get("key"):
            found = {"address": address, "account": str(entry["account"]).strip().lower(), "key": str(entry["key"])}
        else:
            found = self.find_alias(address)
        if not found:
            raise MailcomApiError(f"mail.com managed alias missing: {address}")
        if mailbox.get("account") and str(mailbox["account"]).strip().lower() != found["account"]:
            raise MailcomApiError("mail.com mailbox owner mismatch")
        for field in ("alias_key", "key"):
            if mailbox.get(field) and str(mailbox[field]).strip() != found["key"]:
                raise MailcomApiError("mail.com invalid key for exact alias")
        return address, found["account"], found["key"]

    def _inbox_request(
        self, mailbox: dict[str, Any], path: str, *, exclude: list[str], timeout: int = 0,
    ) -> dict[str, Any]:
        address, account, key = self._alias_credentials(mailbox)
        params: dict[str, Any] = {
            "email": address, "account": account, "key": key,
            "max_age_seconds": mail_state.ICLOUD_WAIT_MAX_AGE_SECONDS,
        }
        after = ICloudApiProvider._after_epoch(self, mailbox)
        if after is not None:
            params["after"] = after
        if exclude:
            params["exclude"] = ",".join(exclude)
        if path.endswith("/wait"):
            params["timeout"] = max(1, min(60, int(timeout)))
        else:
            params["limit"] = self.message_limit
        try:
            response = self.session.get(
                f"{self.api_base}{path}", params=params,
                headers={"Accept": "application/json", "User-Agent": self.conf["user_agent"]},
                timeout=max(float(self.conf["request_timeout"]), params.get("timeout", 0) + 15.0),
            )
        except Exception as error:
            raise MailcomApiError(f"mail.com inbox request failed: {self._safe_error(error, key)}") from None
        status = int(response.status_code or 0)
        if status in {401, 403, 404}:
            raise MailcomApiError(f"mail.com invalid key or alias (HTTP {status})")
        if status != 200:
            raise MailcomApiError(f"mail.com inbox HTTP {status}")
        try:
            data = response.json()
        except Exception:
            raise MailcomApiError("mail.com inbox: invalid JSON") from None
        if not isinstance(data, dict):
            raise MailcomApiError("mail.com inbox: expected a JSON object")
        if data.get("ok") is False or data.get("success") is False:
            raise MailcomApiError("mail.com inbox: adapter reported an error")
        # Preserve the envelope timestamp on the top-level code AND nested
        # message. Otherwise a timestamp-less duplicate could bypass freshness.
        for field, expected in (("email", address), ("address", address), ("account", account), ("primaryEmail", account)):
            if isinstance(data.get(field), str) and str(data[field]).strip().lower() != expected:
                raise MailcomApiError(f"mail.com inbox {field} mismatch")
        raw_messages = mail_state._icloud_payload_messages(data)
        messages = []
        for raw in raw_messages:
            item = dict(raw)
            item["received_at"] = (
                raw.get("received_at") or raw.get("receivedAt") or raw.get("receivedDateTime")
                or raw.get("date") or raw.get("timestamp") or raw.get("created_at") or data.get("received_at")
            )
            if not item.get("text_content"):
                item["text_content"] = raw.get("bodyPreview") or raw.get("preview") or raw.get("text") or ""
            sender = raw.get("from")
            if isinstance(sender, dict):
                sender = sender.get("emailAddress") or sender
                if isinstance(sender, dict):
                    sender_address = str(sender.get("address") or "")
                    sender_name = str(sender.get("name") or "")
                    item["sender"] = f"{sender_name} <{sender_address}>" if sender_name and sender_address else sender_address or sender_name
            messages.append(item)
        payload = {"messages": messages, "code": data.get("code"),
                   "received_at": data.get("received_at") or (messages[0].get("received_at") if messages else None)}
        return payload

    def _request_code(self, mailbox: dict[str, Any], *, timeout: int, exclude: list[str]) -> dict[str, Any]:
        return self._inbox_request(mailbox, "/api/v1/code/wait", timeout=timeout, exclude=exclude)

    def _request_messages(self, mailbox: dict[str, Any], *, exclude: list[str]) -> dict[str, Any]:
        return self._inbox_request(mailbox, "/api/v1/messages", exclude=exclude)

    def fetch_recent_messages(self, mailbox: dict[str, Any]) -> list[dict[str, Any]]:
        data = self._request_messages(mailbox, exclude=[])
        messages = ICloudApiProvider._normalize_direct_messages(self, mailbox, data)
        for message in messages:
            if not message["recipients"]:
                message["recipients"] = [self._address(mailbox.get("address"))]
        return messages[:self.message_limit]

    def wait_for_code(self, mailbox: dict[str, Any], stop_check=None) -> str | None:
        def stopped() -> bool:
            return bool(callable(stop_check) and stop_check())

        if stopped():
            return None
        self._alias_credentials(mailbox)
        seen = mailbox.setdefault("_seen_otp_codes", [])
        if not isinstance(seen, list):
            seen = mailbox["_seen_otp_codes"] = []
        exclude = [str(code) for code in seen if str(code).strip()]
        deadline = time.monotonic() + float(self.conf["wait_timeout"])
        mailbox.pop("_last_code_wait_error", None)
        mailbox["_code_wait_attempts"] = 0
        while time.monotonic() < deadline and not stopped():
            chunk = max(1, int(min(60, float(self.conf["wait_chunk"]), deadline - time.monotonic())))
            mailbox["_code_wait_attempts"] += 1
            try:
                data = self._request_code(mailbox, timeout=chunk, exclude=exclude)
            except MailcomApiError as error:
                mailbox["_last_code_wait_error"] = str(error)[:300]
                if "invalid key" in str(error).lower() or "mismatch" in str(error).lower():
                    raise
                data = {}
            if stopped():
                return None
            candidates = mail_state._icloud_payload_code_candidates(data)
            # Read the cache once per empty poll, including transient failures.
            if not candidates:
                try:
                    data = self._request_messages(mailbox, exclude=exclude)
                    candidates = mail_state._icloud_payload_code_candidates(data)
                except MailcomApiError as error:
                    mailbox["_last_code_wait_error"] = str(error)[:300]
                    if "invalid key" in str(error).lower() or "mismatch" in str(error).lower():
                        raise
            if stopped():
                return None
            for code, subject, received_at in candidates:
                if received_at is not None and (
                    mail_state._message_before_code_boundary(mailbox, {"received_at": received_at})
                    or (datetime.now(timezone.utc) - received_at).total_seconds() > mail_state.ICLOUD_WAIT_MAX_AGE_SECONDS
                ):
                    continue
                if code in exclude:
                    continue
                seen.append(code)
                exclude.append(code)
                if re.fullmatch(r"\d{6}", code) and not any(
                    marker in subject.lower() for marker in ("new sign-in", "new login", "security alert")
                ):
                    mailbox.pop("_last_code_wait_error", None)
                    return code
            pause_until = min(deadline, time.monotonic() + max(0.2, float(self.conf["wait_interval"])))
            while time.monotonic() < pause_until:
                if stopped():
                    return None
                time.sleep(min(0.2, max(0, pause_until - time.monotonic())))
        return None
