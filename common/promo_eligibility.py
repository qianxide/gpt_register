"""Read-only Plus promotion and checkout metadata checks.

The checker creates a quote/checkout session only to read the server returned
price, discount and payment methods. It never confirms, authorizes or submits a
payment. Results are deliberately kept separate from the existing trial probe
so a pricing failure cannot overwrite a valid account health result.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from curl_cffi import requests

from common.account_health import _identity_headers, _request_identity, _session
from common.trial_proxy_pool import selected_trial_proxy

ACCOUNT_CHECK_PATH = "/backend-api/accounts/check/v4-2023-04-27"
LABELS = {
    "card": "银行卡", "paypal": "PayPal", "link": "Link", "upi": "UPI",
    "pix": "PIX", "momo": "MoMo", "gopay": "GoPay", "grabpay": "GrabPay",
    "gcash": "GCash", "ideal": "iDEAL", "blik": "BLIK", "twint": "TWINT",
    "kakao": "Kakao Pay", "naver_pay": "Naver Pay", "bizum": "Bizum",
    "cashapp": "Cash App", "amazon_pay": "Amazon Pay",
}
CURRENCY = {
    "US": "USD", "CA": "CAD", "GB": "GBP", "AU": "AUD", "DE": "EUR",
    "FR": "EUR", "IT": "EUR", "ES": "EUR", "JP": "JPY", "IN": "INR",
    "ID": "IDR", "TH": "THB", "MY": "MYR", "PH": "PHP", "VN": "VND",
    "BR": "BRL", "MX": "MXN", "SG": "SGD", "KR": "KRW", "PL": "PLN",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _walk(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk(nested)


def _amount(value: Any) -> int | None:
    if isinstance(value, dict):
        for key in ("minorUnitsAmount", "minor_units_amount", "amount", "value"):
            if key in value:
                return _amount(value[key])
        return None
    if isinstance(value, bool) or value is None:
        return None
    try:
        text = str(value).strip()
        if not text or any(ch not in "0123456789.-" for ch in text):
            return None
        return int(float(text))
    except (TypeError, ValueError):
        return None


def _find_amount(payload: Any, names: tuple[str, ...]) -> int | None:
    for item in _walk(payload):
        for key in names:
            if key in item:
                found = _amount(item.get(key))
                if found is not None:
                    return found
    return None


def _payment_methods(payload: Any) -> list[str]:
    methods: list[str] = []
    seen: set[str] = set()
    for item in _walk(payload):
        values = item.get("payment_method_types") or item.get("paymentMethodTypes")
        if isinstance(values, list):
            for value in values:
                if isinstance(value, dict):
                    value = value.get("type") or value.get("id")
                name = str(value or "").strip().lower()
                if name and name not in seen:
                    seen.add(name); methods.append(name)
        custom = item.get("custom_payment_methods") or item.get("customPaymentMethods")
        if isinstance(custom, list):
            for value in custom:
                if not isinstance(value, dict):
                    continue
                text = json.dumps(value, ensure_ascii=True).lower()
                name = next((key for key in LABELS if key in text), "")
                if not name:
                    name = str(value.get("type") or value.get("id") or "").lower()
                if name and name not in seen:
                    seen.add(name); methods.append(name)
    return methods


def _country_and_proxy(record: dict[str, Any]) -> tuple[str, str]:
    country, proxy = selected_trial_proxy()
    if not proxy:
        proxy = str(record.get("proxy") or "").strip()
    return (country or "US").upper(), proxy


def _headers(record: dict[str, Any], token: str, identity: dict[str, str], path: str, referer: str) -> dict[str, str]:
    headers = _identity_headers(identity)
    headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://chatgpt.com",
        "Referer": referer,
        "Cache-Control": "no-cache",
        "OAI-Session-Id": str(uuid.uuid4()),
        "OAI-Language": identity.get("locale") or "en-US",
        "x-openai-target-path": path,
        "x-openai-target-route": path,
    })
    if record.get("account_id"):
        headers["ChatGPT-Account-ID"] = str(record["account_id"])
    return headers


def _sentinel_headers(session: Any, identity: dict[str, str], device_id: str) -> dict[str, str]:
    """Best-effort reuse of the project's existing Sentinel implementation."""
    try:
        from register.protocol_http import _build_sentinel_headers
        return _build_sentinel_headers(
            session, device_id, "chatgpt_checkout", fingerprint=identity
        ) or {}
    except Exception:
        return {}


def _checkout_one(record: dict[str, Any], token: str, country: str, proxy: str, timeout: float) -> dict[str, Any]:
    identity = _request_identity(record)
    device_id = identity.get("device_id") or str(uuid.uuid4())
    currency = CURRENCY.get(country, "USD")
    campaign = str(record.get("trial_eligibility_campaign_id") or "plus-1-month-free").strip()
    session = _session(proxy, impersonate=identity["impersonate"])
    try:
        pricing_path = f"/backend-api/checkout_pricing_config/configs/{country}"
        pricing = session.get(
            "https://chatgpt.com" + pricing_path,
            headers=_headers(record, token, identity, pricing_path, "https://chatgpt.com/"),
            timeout=timeout,
        )
        if pricing.status_code >= 400:
            return {"ok": False, "country": country, "currency": currency, "status": pricing.status_code, "promo": "error", "methods": [], "error": f"定价配置 HTTP {pricing.status_code}"}

        checkout_path = "/backend-api/payments/checkout"
        checkout_headers = _headers(record, token, identity, checkout_path, f"https://chatgpt.com/?promo_campaign={campaign}")
        checkout_headers.update(_sentinel_headers(session, identity, device_id))
        payload = {
            "entry_point": "all_plans_pricing_modal",
            "plan_name": "chatgptplusplan",
            "billing_details": {"country": country, "currency": currency},
            "checkout_ui_mode": "custom",
            "promo_campaign": {"promo_campaign_id": campaign, "is_coupon_from_query_param": False},
        }
        response = session.post("https://chatgpt.com" + checkout_path, json=payload, headers=checkout_headers, timeout=timeout)
        try:
            data = response.json()
        except Exception:
            data = {}
        if response.status_code >= 400:
            return {"ok": False, "country": country, "currency": currency, "status": response.status_code, "promo": "error", "methods": [], "error": f"checkout HTTP {response.status_code}: {str(data)[:180]}"}

        methods = _payment_methods(data)
        session_id = str(data.get("checkout_session_id") or data.get("id") or "")
        session_type = "oaics" if session_id.startswith("oaics_") else "cs_live" if session_id.startswith("cs_") else ""
        state = data
        if session_type == "oaics" and session_id:
            entity = str(data.get("processor_entity") or ("openai_llc" if country == "US" else "openai_ie"))
            try:
                state_resp = session.get(
                    f"https://chatgpt.com/backend-api/payments/checkout/{entity}/{session_id}",
                    headers=_headers(record, token, identity, f"/backend-api/payments/checkout/{entity}/{session_id}", "https://chatgpt.com/"),
                    timeout=timeout,
                )
                if state_resp.status_code == 200:
                    state = state_resp.json()
                    methods = methods or _payment_methods(state)
            except Exception:
                pass
        elif session_type == "cs_live" and data.get("publishable_key") and session_id:
            # Stripe init only returns invoice/payment-method metadata; no payment action.
            try:
                stripe = _session(proxy, impersonate=identity["impersonate"])
                try:
                    init = stripe.post(
                        f"https://api.stripe.com/v1/payment_pages/{session_id}/init",
                        data={"key": data["publishable_key"], "browser_locale": identity.get("locale", "en-US"), "browser_timezone": identity.get("timezone", "UTC")},
                        headers={"User-Agent": identity.get("user_agent", "Mozilla/5.0")},
                        timeout=timeout,
                    )
                    init_data = init.json() if init.text else {}
                    methods = methods or _payment_methods(init_data)
                    state = init_data if isinstance(init_data, dict) else state
                finally:
                    stripe.close()
            except Exception:
                pass

        amount = _find_amount(state, ("amount_due", "amountDue", "checkout_amount_minor", "amount_total", "amountTotal", "due", "taxInclusive"))
        subtotal = _find_amount(state, ("subtotal", "subtotal_amount", "subtotalAmount", "unit_amount"))
        discounted = bool(amount is not None and subtotal and amount < subtotal)
        percent = int(round((1 - amount / subtotal) * 100)) if discounted else 0
        zero = amount == 0
        promo = "yes" if zero else "partial" if discounted else "no"
        labels = [LABELS.get(name, name) for name in methods]
        return {
            "ok": bool(methods) and (zero or discounted), "country": country, "currency": currency,
            "status": response.status_code, "session_type": session_type, "amount": amount,
            "subtotal": subtotal, "discount_percent": percent, "promo": promo,
            "campaign_id": campaign, "methods": methods, "method_labels": labels,
            "error": "" if zero or discounted else f"优惠后金额不是 0: {amount}",
        }
    finally:
        session.close()


def check_account_promo(record: dict[str, Any], *, countries: list[str] | None = None, timeout: float = 20.0, attempts: int = 2) -> dict[str, Any]:
    token = str(record.get("access_token") or "").strip()
    checked_at = _now()
    if not token:
        return {"ok": False, "checked_at": checked_at, "error": "缺少 access_token", "promo_summary": ""}
    default_country, proxy = _country_and_proxy(record)
    # Refresh plan/campaign metadata first when the account row is stale. This
    # keeps paid plans and non-free campaigns on their server-issued campaign
    # id instead of assuming the one-month free campaign.
    metadata_result: dict[str, Any] = {}
    if not str(record.get("trial_eligibility_campaign_id") or "").strip() or not str(record.get("plan_type") or "").strip():
        try:
            from common.trial_eligibility import check_trial_eligibility
            metadata = check_trial_eligibility(record, proxy=proxy, direct=not bool(proxy), timeout=timeout)
            if metadata.get("ok"):
                metadata_result = metadata
                record = {
                    **record,
                    "plan_type": metadata.get("plan_type") or record.get("plan_type"),
                    "trial_eligibility_campaign_id": metadata.get("promo_campaign_id") or record.get("trial_eligibility_campaign_id"),
                }
        except Exception:
            pass
    selected = [str(item or "").strip().upper() for item in (countries or [default_country])]
    selected = list(dict.fromkeys(item for item in selected if len(item) == 2 and item.isalpha())) or [default_country]
    results: list[dict[str, Any]] = []
    for country in selected:
        item: dict[str, Any] = {}
        for attempt in range(max(1, int(attempts or 1))):
            item = _checkout_one(record, token, country, proxy, timeout)
            if item.get("promo") in {"yes", "partial"} or attempt + 1 >= attempts:
                break
            time.sleep(0.4)
        results.append(item)
    methods: list[str] = []
    labels: list[str] = []
    parts: list[str] = []
    for item in results:
        for name, label in zip(item.get("methods") or [], item.get("method_labels") or []):
            if name not in methods:
                methods.append(name); labels.append(label)
        status = "0元" if item.get("promo") == "yes" else f"{item.get('discount_percent', 0)}%off" if item.get("promo") == "partial" and item.get("discount_percent") else item.get("promo") or "error"
        stype = f"[{item.get('session_type')}]" if item.get("session_type") else ""
        pays = ",".join(item.get("method_labels") or [])
        parts.append(f"{item.get('country', '?')}:{status}{stype}" + (f"({pays})" if pays else ""))
    promo_countries = [item.get("country") for item in results if item.get("promo") in {"yes", "partial"} and item.get("ok")]
    return {
        "ok": True, "checked_at": checked_at, "promo_ok": bool(promo_countries),
        "plan_type": metadata_result.get("plan_type") or record.get("plan_type") or "",
        "plus_trial_eligible": bool(metadata_result.get("eligible")),
        "plus_trial_campaign_id": metadata_result.get("promo_campaign_id") or record.get("trial_eligibility_campaign_id") or "",
        "promo_countries": promo_countries, "promo_methods": methods,
        "promo_method_labels": labels, "promo_summary": ";".join(parts),
        "promo_session_types": list(dict.fromkeys(item.get("session_type") for item in results if item.get("session_type"))),
        "promo_results": results, "proxy_used": bool(proxy), "error": None,
    }
