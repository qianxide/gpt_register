# mail.com SDK service

An HTTP adapter for the existing mailbox application's `MailcodeClient`, using
the pinned `maildotcom-sdk@1.0.9` package. Requires Node.js 20 or newer.

Account import persists configuration. The application's subsequent domain query
performs real mail.com login; its success or failure is shown per primary account.
Credentials remain saved when mail.com requires verification or rejects login.
Alias creation requires a successful live domain query and SDK creation result.

The service binds to loopback. Set `MAILCODE_ADMIN_KEY` (at least 24 characters),
`MAILCODE_PORT` (default 18787), and `MAILCODE_DATA_DIR` before `npm start`.
Point the Python application at `MAILCODE_API_URL=http://127.0.0.1:18787` with
the same management key. Do not expose this internal API publicly.

Supported management endpoints (all require `X-API-Key`):

- `POST /api/v1/import` with `accounts: [{ email, password }]`.
- `GET /api/v1/accounts`; `include_passwords=1` is reserved for the authenticated
  backend restoring its form.
- `GET /api/v1/alias-domains?account_email=...`.
- `GET /api/v1/aliases?account_email=...` returns service-managed aliases.
- `POST /api/v1/aliases` with `account_email` and `domain` (or `random`).
  An optional `address` requests a particular address in that domain.
- `DELETE /api/v1/aliases/:address?account_email=...` deletes only aliases created
  by this service for that primary account.

`GET /api/v1/messages`, `/api/v1/code`, and `/api/v1/code/wait` accept a dedicated
alias key and its exact email address. Messages are matched to that alias's To/Cc
recipients and filtered by `after`, `max_age_seconds`, and excluded codes. Message
bodies are read with `markRead:false`. Poll intervals are at least three seconds.
The existing Python consumer expects six-digit codes. Messages whose canonical
OTP has a different length are omitted rather than allowing the consumer to
mistake an unrelated number in their body for a code. Code responses retain the
message timestamp so the caller can enforce its own freshness boundary.

`GET /api/health` is unauthenticated and contains no account information.
Credentials and alias keys are encrypted in `accounts.enc`; `accounts.key` is
stored beside it with owner-only permissions. Back up both together, privately.
The systemd unit uses a dedicated user and `/var/lib/gc-mailcode` for state.

Per-account locks serialize alias mutations. A create interrupted after the
upstream request is reconciled against the live alias list when possible. An
uncertain result remains `unconfirmed` until listing succeeds; retrying the same
explicit address reconciles its reservation, and confirmed absent reservations
can be removed without touching an unrelated address. SDK errors are sanitized before returning
to the page, so upstream cookies/tokens/redirect query strings are never exposed.

Run `npm ci --ignore-scripts` and `npm test` for offline API, persistence, and
mailbox-isolation tests. `probe.mjs` accepts email/password only from stdin for an
explicit login check; it prints only login status, domains and redacted diagnostics.
