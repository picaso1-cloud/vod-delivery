# Device pairing — app plan

Goal: no more copying tokens by hand. The app asks for access, the owner approves on
Telegram (or the web panel), and the app receives its own token once.

The server side is implemented. The Android UI is planned but not built yet — the test
phone is not connected to adb.

## UX flow

1. First launch / Settings → **Pair this device**.
2. The app shows the server URL (LAN or public) and the device name (defaults to
   `Build.MANUFACTURER + " " + Build.MODEL`).
3. Tap **Request access** → `POST /api/app/pair/request`.
4. The app displays a short code, e.g. `D74E06BD`, plus “Waiting for approval…”.
5. The owner gets a Telegram message:
   `Pixel 8 · android 14 · IP 192.168.1.164 · Code D74E06BD` with **Approve / Deny**.
   The same request appears on the web **App access** page.
6. The app polls `GET /api/app/pair/status?request_id=&code=` every 2 s (10 min max).
7. On `approved` the response carries the token once. The app stores it, calls
   `/api/app/whoami`, and enters the normal UI. On `denied` / `expired`, it shows why and
   offers **Try again**.

Manual token entry stays as an “Advanced” fallback for debugging.

## Server contract (already live)

```http
POST /api/app/pair/request
Content-Type: application/json
{"device_name":"Pixel 8","platform":"android 14","instance_id":"<uuid>"}

200 {"ok":true,"request_id":"<24 hex>","code":"D74E06BD","expires_in":600}
```

```http
GET /api/app/pair/status?request_id=<id>&code=<CODE>

200 {"ok":true,"status":"pending"}
200 {"ok":true,"status":"approved","token":"<token>"}   # only on the first poll
200 {"ok":true,"status":"claimed"}                       # subsequent polls
200 {"ok":true,"status":"denied"}                        # or "expired"
404 unknown request or wrong code
429 rate limited
```

Limits: 6 requests/min/IP, 20 pending requests, 10-minute TTL. The token is stored in
`app_tokens` with the device label and can be revoked from the panel or `/tokens`.

## Android implementation plan

### Storage (DataStore / SharedPreferences)

- `instance_id` — random UUID, generated once and kept for the install.
- `base_url`, `public_url`, `token`, `device_name` — existing settings plus the name.
- Pairing state is transient (screen state), not persisted.

### UI

- **Settings** gains a “Pair this device” card: server URL, device name, **Request access**.
- **Pair screen** (or dialog): code, device name, “Approve in Telegram”, spinner, Cancel,
  Retry; link/button to open Telegram (`tg://resolve?domain=<bot_username>` is optional —
  the bot username is not currently in `/api/app/config`, can be added later).
- **Advanced** collapsible keeps the existing LAN/public URL + token fields.
- After pairing: success state → Discover. `AppLog` records the flow with the token
  redacted.

### Networking

- `Api.pairRequest(deviceName, platform, instanceId)` and
  `Api.pairStatus(requestId, code)` with no `Authorization` header.
- Poll with a 2 s delay, cancel on screen exit; stop after `expires_in`.
- On 429: back off 10 s. On 404: the request expired — start a new one. On repeated
  network errors: try the other URL (LAN ↔ public) once.
- On entering the app with a stored token, `whoami` 401 → clear the token and show Pair.

### Verification when the phone is back

1. `adb install -r android/dist/vodcast-*.apk`; clear app data.
2. Request access; confirm the Telegram notification shows the same code.
3. Approve; confirm the app lands on Discover and the token survives an app restart.
4. Revoke from the web page; confirm the next API call returns 401 and the Pair screen
   appears.
5. Deny path: request again, press Deny, confirm the app shows “denied” and can retry.

## Security notes

- The request/status endpoints are tokenless by design; the 8-char code plus the
  unguessable request id are the only handoff secret. Public access must be HTTPS.
- The owner sees the code in Telegram, so approving a request you did not initiate is
  visible. Approve only when the code matches the app screen.
- Tokens are shown once in the app flow; the web list is masked. Tokens are currently
  stored plaintext in SQLite (same as before) — hashing is a future hardening step.
- No auto-approval, no wildcard tokens, no token in URLs.

## Open questions

- Add the bot username to `/api/app/config` for a one-tap “Open Telegram” link.
- QR code on the web page for the server URL (nice-to-have).
- Multiple owners: the notification currently goes to the first `allowed_ids` entry.
- Token scopes (read-only vs control) if the API is shared beyond the household.
