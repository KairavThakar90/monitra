# Email notifications

Monitra sends two automated emails. Both are queued into a database outbox and
delivered from there; neither is sent inline from the request that triggers it.

| | Trigger | Recipients | Sent |
|---|---|---|---|
| **Welcome** | A Monitra account is provisioned for the first time | The new user | Exactly once, ever |
| **Feedback notification** | Feedback is submitted from the desktop client | Configured Admin + HR | Exactly once per feedback record |

---

## 1. Why there is an outbox

Two properties had to hold, and both are hard to get right by wrapping a send
call in `try`:

**The user's work must not depend on a mail server.** A feedback submission is
saved whether or not Admin and HR can be told about it. An account is
provisioned whether or not the welcome email goes out. Persisting the *intent*
to send, separately from sending, makes that true by construction.

**A retry must not be able to send twice.** The identity of an email is the
event it belongs to — user 42's welcome, feedback 17's notification — not the
number of times something asked for it. `email_notifications` is unique on
`(notification_type, dedupe_key)`, so a client retry, a replayed request, two
concurrent workers and a redeploy mid-send all collapse onto one row.

Nothing was introduced to run this — no queue broker, no worker process, no new
Python dependency. The table is the queue and an HTTP endpoint on a timer is
the worker, which is what suits a backend deployed as serverless functions.

```
  Desktop client                    Backend                      Mail server
       │                               │                              │
       │  POST /feedback               │                              │
       ├──────────────────────────────►│                              │
       │                               │ 1. persist feedback (commit) │
       │                               │ 2. queue notification row    │
       │  201 Created                  │                              │
       │◄──────────────────────────────┤                              │
       │                               │ 3. background attempt ───────►│   fast path
       │                               │                              │
                                       │ 3'. POST /internal/email/    │
                                       │     dispatch (scheduled) ────►│   guarantee
```

Step 3 is an optimisation: the response is already written, so a slow mail
server delays nobody. On a serverless platform the invocation can be frozen
before it runs, so it is **not** the guarantee. Step 3' is — anything the fast
path missed is picked up by the sweeper and retried with backoff.

---

## 2. Where everything lives

| Path | What it is |
|---|---|
| `backend/app/services/email/provider.py` | The transport. The only module that imports `smtplib`. |
| `backend/app/services/email/outbox.py` | Queueing, claiming, retry, backoff. |
| `backend/app/services/email/workflows.py` | The two triggers. Neither can raise into its caller. |
| `backend/app/services/email/messages.py` | Builds each email from its stored payload. |
| `backend/app/services/email/templates.py` | The renderer. Escapes every value by default. |
| `backend/app/services/email/recipients.py` | Resolves Admin/HR from configuration. |
| `backend/app/services/email/assets.py` | Logo resolution: Content-ID or hosted URL. |
| `backend/app/templates/emails/` | `base.html`, `welcome.html`, `feedback.html`. |
| `backend/app/assets/email/` | The logo artwork. |
| `backend/app/models/email_notification.py` | The outbox table. |
| `backend/app/api/email_notifications.py` | The sweeper endpoint and `/email-assets`. |

The desktop client is unchanged and holds nothing: no SMTP credential, no
provider key, no recipient address. It posts feedback to the existing
authenticated API, exactly as it did before.

---

## 3. Triggers

### Welcome — provisioning, not signing in

`AuthService._welcome_new_user` is called from the two places a Monitra account
comes into existence: the provisioning branch of `login_exchange`, and the
provisioning branch of `sso_exchange`. Nowhere else.

That distinction is the whole correctness argument. An account is created the
first time somebody authenticates successfully, so creation *is* activation;
every later sign-in reaches the synchronise branch and never gets here.
Welcoming on sign-in instead would have mailed every existing employee on the
first working day after deployment — and the outbox would have deduplicated
them to exactly one each, which is precisely the problem.

A test asserts the call stays inside the provisioning branch, because an edit
that hoists it out is the mistake with the largest blast radius here.

### Feedback — after the row is committed

`FeedbackService.submit_feedback` persists first, then queues. The queue call
does not raise — it logs and returns `None` — so there is no path from "the
mail server is down" or "nobody is configured" to a person being told their
feedback failed.

Two clicks on Submit are two feedback records and therefore two emails, which
is correct: they are two distinct submissions. Idempotency is per *record*.
(The desktop dialog also de-duplicates in flight, with a fixed task key, and
disables the button — see `desktop/ui/feedback_dialog.py`.)

---

## 4. Retry and failure

* Attempts are **counted before the send**, not after, so a process dying
  mid-send parks the row until its backoff expires rather than letting the next
  sweep deliver it again. It prefers "possibly never delivered, visibly" over
  "possibly delivered twice".
* Backoff is exponential with **full jitter**, capped. A queue that all retries
  in the same second after an outage is a self-inflicted load test.
* After `EMAIL_MAX_ATTEMPTS` the row is parked as `failed` with the reason in
  `last_error`.
* A deployment that **cannot send at all** (no `SMTP_HOST`, no
  `EMAIL_FROM_ADDRESS`) is detected before a row is claimed, so a configuration
  mistake never consumes a notification's attempts. Fix the configuration and
  the backlog delivers on the next sweep.
* Errors are redacted before they are logged or stored: anything matching
  `SMTP_PASSWORD`, `SMTP_USERNAME` or `EMAIL_DISPATCH_TOKEN` is replaced, and
  the text is stripped of line breaks and truncated.

Statuses: `pending` → `sent` | `failed`, plus `cancelled` for a notification
deliberately withdrawn.

---

## 5. Configuration

Every variable is documented with placeholders in **`backend/.env.example`**.
The ones a production deployment must set:

| Variable | Why |
|---|---|
| `EMAIL_PROVIDER=smtp` | Selects the transport. |
| `EMAIL_FROM_ADDRESS` | The envelope sender. Nothing is delivered without it. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD` | The mail server. |
| `FEEDBACK_ADMIN_EMAIL`, `FEEDBACK_HR_EMAIL` | Who is notified. No address is hard-coded. |
| `EMAIL_DISPATCH_TOKEN` | Authorises the sweeper. Without it, retry does not run. |

Optional: `EMAIL_REPLY_TO`, `FEEDBACK_NOTIFICATION_EMAILS`, `MONITRA_APP_URL`,
`MONITRA_SUPPORT_EMAIL`, `EMAIL_ASSET_BASE_URL`, `WELCOME_EMAIL_ENABLED`, and
the retry tuning values.

`GET /health` reports which of these are populated — never their values — so a
deployment can be checked after a redeploy without reading platform logs:

```json
"email": {
  "configured": true, "provider": "smtp", "asset_mode": "cid",
  "feedback_recipients": {"configured": true, "recipient_count": 2,
                          "admin_set": true, "hr_set": true},
  "dispatch_endpoint_enabled": true
}
```

### The sweeper

```
POST /internal/email/dispatch
X-Email-Dispatch-Token: <EMAIL_DISPATCH_TOKEN>
```

Authorised by a dedicated shared secret, not a user session. A scheduler has no
user, and pointing it at somebody's account — let alone an admin's — would hand
a cron job a long-lived credential with broad authority for the sake of one
narrow operation. This token grants exactly this endpoint. Unset, the endpoint
answers 503 and is never open.

The secret is also accepted as `Authorization: Bearer`, and the same operation
is exposed on `GET`, because Vercel Cron can only issue a GET with an
`Authorization` header. `vercel.json` schedules it hourly:

```json
"crons": [{ "path": "/internal/email/dispatch", "schedule": "0 * * * *" }]
```

> **Vercel Hobby plans run cron jobs once per day**, at an hour Vercel chooses.
> That is enough for correctness — nothing is lost, retries just take longer —
> but a Pro plan (or any external scheduler hitting the same URL) is what makes
> the hourly schedule above actually hourly.

Calling it manually, to drain the queue or check a deployment:

```bash
curl -sS -X POST "https://<host>/internal/email/dispatch" \
     -H "X-Email-Dispatch-Token: $EMAIL_DISPATCH_TOKEN"
# {"attempted":2,"sent":2,"retrying":0,"failed":0,...}
```

---

## 6. Email images

A mail client cannot open a file path, and a link to `localhost` resolves on
the *recipient's* machine. So there are two strategies and both are implemented:

* **Content-ID (default).** The logo bytes travel inside the message. Nothing
  needs public hosting, images render even when the client blocks remote
  content, and mail read years later still looks right.
* **Hosted URL.** Set `EMAIL_ASSET_BASE_URL` to a public https:// prefix — this
  backend serves the files at `/email-assets/` — and the templates reference
  them instead. Smaller messages, at the cost of a host dependency.

A `localhost` value for `EMAIL_ASSET_BASE_URL` is refused at render time and
falls back to embedding, so a development value cannot reach a real mailbox as
a broken image.

### The artwork

`backend/app/assets/email/monitra-logo.png` is the official Monitra logo
(`frontend/public/logo.png`), trimmed, resized to 560px and flattened onto
white — drawn at 280px so it stays sharp on a high-density display.

**`store-transform-logo.png` is not yet installed.** Until the official Store
Transform artwork is dropped into that directory, both templates render the
Store Transform name as styled text. That is deliberate: an absent logo becomes
words, never a substitute image and never an approximation of the mark. Adding
the file is the whole change — no code edit, no configuration.

Prepare it the same way:

```bash
python - <<'PY'
from PIL import Image, ImageChops
src = Image.open("path/to/store-transform-logo.png").convert("RGBA")
bg = Image.new("RGBA", src.size, (255, 255, 255, 255))
flat = Image.alpha_composite(bg, src).convert("RGB")
diff = ImageChops.difference(flat, Image.new("RGB", flat.size, (255, 255, 255))).convert("L")
box = diff.point(lambda p: 255 if p > 28 else 0).getbbox()
out = src.crop(box)
out = out.resize((560, round(out.height * 560 / out.width)), Image.LANCZOS)
canvas = Image.new("RGB", out.size, (255, 255, 255))
canvas.paste(out, (0, 0), out)
canvas.save("backend/app/assets/email/store-transform-logo.png", optimize=True)
PY
```

Assets over 200 KB are refused rather than mailed to everybody.

---

## 7. Security

* **User content is escaped by default.** The renderer substitutes
  `{{ name }}` with the HTML-escaped value, always. A value that genuinely is
  markup must be a `markupsafe.Markup` instance constructed in Python — there is
  no "raw" syntax to reach for by accident. A feedback message containing
  `<script>` renders as those characters.
* **Header injection is refused, not repaired.** A recipient address containing
  a line break raises. A Subject folds its whitespace to one line (a Subject is
  single-line by definition) and refuses any genuine control character.
* **No secret is ever in a message or a log.** Payloads carry only what the
  email displays — no token, no permission map, no password hash. Provider
  errors are redacted before they are stored or logged.
* **Recipients are validated** and de-duplicated; a malformed one is dropped
  with a log line naming the *setting*, never the value.
* **TLS is not optional.** There is no code path that offers an SMTP username
  over an unencrypted connection; the provider refuses rather than falling back.
* **`/email-assets` cannot be walked out of.** Paths are resolved and checked
  against the asset directory, and only image types are served.

---

## 8. Tests

`backend/tests/test_email_notifications.py` — 85 tests covering idempotency,
isolation (email failure never changes the user's outcome), escaping, retry and
backoff, recipient configuration, header safety, secret redaction, the dispatch
endpoint's authorisation, and that the existing feedback API behaves exactly as
it did.

No test sends a real message: the transport is always a mock or an in-memory
recorder.

```bash
cd backend
python -m pytest tests/ -q
python -m alembic upgrade head
```
