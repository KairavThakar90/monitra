# Input Validation

This document is the contract for every user-controlled input in this
repository — the ones that exist today and the ones added tomorrow.

Read section 4 before adding any field, form, dialog or endpoint.

---

## 1. The model

```
USER INPUT
    ↓
FRONTEND or DESKTOP VALIDATION      immediate, friendly, keeps bad requests from being sent
    ↓
API REQUEST
    ↓
BACKEND VALIDATION                  the final authority — always runs
    ↓
BUSINESS LOGIC
    ↓
DATABASE
```

The clients validate for the **user's** benefit. The backend validates for
**security**. Those are different jobs, and only the second one is a control.

**The backend never trusts client validation.** A request can arrive from a
browser, from the desktop app, from `curl`, from a script replaying a captured
request with the body edited, or from a custom HTTP client. Only two of those
ran our code. So every user-controlled field is validated server-side
regardless of origin — the client checks are a convenience that can be skipped
entirely, and the system must be exactly as safe when they are.

The corollary matters just as much and is easier to get wrong:

> **A client must never be stricter than the backend.**

If the desktop refuses a value the server would accept, the field is simply
unusable, and the bug reaches you as "the app won't let me save my work". When
in doubt, let the client be permissive and let the server decide.

## 2. Where the code lives

| Layer | Package | Consumed as |
|---|---|---|
| Backend | `backend/app/core/validation/` | `Annotated` Pydantic types on request schemas |
| Desktop | `desktop/core/validation/` | `validate_*` functions returning a `ValidationResult` |
| Frontend | `frontend/src/validation/` | `validate*` functions + the `useFormValidation` hook |

Each has the same four pieces: `rules` (the catalogue: kinds, limits,
patterns), `sanitizer` (safe normalisation), `validators` (enforcement), and a
consumption layer shaped for that platform.

The three copies are deliberate duplication, not an accident. The desktop must
validate offline with no shared package to install, and the frontend is a
different language. What keeps them honest is a test:
`desktop/tests/test_validation_framework.py` reads **both** `rules.py` files
and asserts every shared limit and pattern matches. Change a limit on one side
only and that test fails.

### Two places the frontend legitimately differs

Both are platform facts, not policy choices, and both are documented in
`frontend/src/validation/rules.ts`:

* **`IDENTIFIER_MAX` is `Number.MAX_SAFE_INTEGER`, not 2^63−1.** JavaScript
  cannot represent the 64-bit maximum — the literal silently rounds. Any id past
  2^53 has already lost precision before the check runs, so it is no longer the
  id that was sent. This does not make the browser stricter in practice: real
  keys are nowhere near that range.
* **`validateSearchTerm` does not escape SQL `LIKE` wildcards.** The browser
  sends a *term*; the server builds the *pattern*. Escaping in both places would
  put a backslash in front of every `%` the user typed. The desktop behaves the
  same way, for the same reason.

## 3. The rules

Pick the rule that matches what the field *means*. There is deliberately no
single global "letters and numbers only" rule — different fields hold
different things, and one blanket pattern is either too loose for identifiers
or too tight for prose.

| Rule | For | Limit | Accepts | Rejects |
|---|---|---|---|---|
| `NAME` | People, projects, tasks, titles | 150 | Unicode, spaces, `'`, `-`, `&`, `.`, `#`, `%` | Markup, whole JSON/XML, control chars |
| `DESCRIPTION` | Prose: notes, reasons, comments, feedback | 5000 | The above plus line breaks | Same as above |
| `PLAIN_TEXT` | Single-line free text | 255 | As `NAME`; newlines collapse to spaces | Same as above |
| `EMAIL` | Addresses | 254 | Trimmed; domain lower-cased | Malformed addresses |
| `PASSWORD` | A secret being **chosen** | 8–128 | **Every character** | Nothing but the length policy |
| `INTEGER` / `DECIMAL` | Numbers | per field | Range-checked | Text, `NaN`, `Infinity`, booleans |
| `IDENTIFIER` | Database keys | ≥ 1 | Positive whole numbers | `0`, negatives, floats, booleans, strings |
| `UUID` | Canonical UUIDs | 36 | Hyphenated, any version | Unhyphenated or malformed |
| `DATE` | Calendar dates | — | `YYYY-MM-DD` or a `date` | Other formats, impossible days |
| `ENUM` | Fixed sets | — | Members only | Everything else |
| `URL` | Links | 2048 | Absolute `http(s)`, full URL punctuation | `javascript:`, `data:`, `file:`, relative |
| `DOMAIN` | Hostnames | 255 | Lower-cased hostnames | Anything with a scheme or path |
| `SEARCH` | Search boxes | 100 | Plain text; wildcards escaped server-side | Markup |
| `VERSION` | Release versions | 32 | `major.minor.patch` only | `v1.0.0`, `1.0`, `1.0.0-rc1`, leading zeros |
| `SHA256` | Artifact checksums | 64 | 64 hex characters; case folded to lower | Any other length or alphabet |
| `TIME_OF_DAY` | A wall-clock time in a schedule | 5 | `HH:MM`, 24-hour (trimmed) | `9:30`, `24:00`, `09:30:00`, AM/PM — rejected, never repaired |

### Versions and checksums

Both arrived with the desktop update system, and both are strict for the same
reason: they are the two things that decide *what gets executed on a user's
machine*.

`VERSION` refuses everything but `major.minor.patch` — no `v` prefix, no
pre-release suffix, no leading zeros — because that pattern is what makes
version *ordering* meaningful. A version that cannot be ordered is one the
update system would have to guess about, which is how a fleet ends up being
told to "update" to something older. Leading zeros are refused so that `1.01.0`
and `1.1.0` cannot name the same release in two ways. It is rejected rather
than repaired: quietly turning `v1.2` into `1.2.0` would let two spellings name
what the system then treats as one build.

`SHA256` is the one place a transformation is applied, and it is
meaning-preserving: the digest is folded to lower case, because `Get-FileHash`
on Windows reports upper case and `shasum` on macOS reports lower case for the
very same bytes. Comparing the spelling rather than the digest would reject an
artifact that downloaded perfectly.

### Passwords are special

Do not apply text rules to a password. Do not trim it, normalise its Unicode,
collapse its whitespace, run an allowlist over it, or check it for markup.
`<script>alert(1)</script>` is a valid password; so is one with a leading
space or a combining accent. Every one of those transformations silently
changes a secret, and the user experiences it as "wrong password" with no way
to diagnose it.

Only length is enforced, because length is the one property checkable without
altering or inspecting the value. Passwords are never logged.

There are two password rules, and the distinction is load-bearing:

* **`Password`** — a password being *chosen*. Full policy, minimum included.
* **`Credential`** — a password being *presented at login*. Upper bound only.
  Enforcing the minimum at sign-in would lock out every account created before
  the policy existed, and would let an attacker probe the policy from the login
  form.

### Stored text must contain something

`NAME`, `DESCRIPTION` and `PLAIN_TEXT` require at least one letter or digit,
in any script. A value of nothing but punctuation — `!!!`, `...`, `@@@`, `---`
— is refused.

Length and structure checks alone do not catch these: `!!!` has a length, is
not markup, and is not JSON, so every other rule passed it. It is a way to
satisfy a required field without answering it.

Punctuation *alongside* real content is untouched: `Fixed!!!`, `C++`, `v2.0!`
and `R&D / Prototype #4` all pass. The test is Unicode-aware, so Japanese,
Cyrillic, Arabic and every other script satisfy it exactly as ASCII does.

Two deliberate exclusions:

* **`SEARCH` is exempt.** Searching for `???` is harmless — a search term
  filters, it is not stored — and requiring content would make a client
  stricter than the server.
* **A value of only emoji or symbols (`👍`) is refused**, since no codepoint in
  it is alphanumeric. Accepted knowingly: for a project name or a task
  description, refusing is the right answer far more often than not.

### Reject, don't scrub

Invalid input is refused. We do not strip the dangerous part and carry on.

Silently removing `<script>` from a message and storing the remainder changes
what the user said — a data-integrity problem wearing a security costume. The
only changes made are ones that cannot alter meaning: trimming surrounding
whitespace, Unicode NFC normalisation, and line-ending consistency.

The same reasoning explains an ordering detail worth preserving: control
characters are checked against the **raw** submission, before normalisation.
Normalising first would quietly remove a NUL and hand back something that
looks clean.

### The deliberate trade-off

A description reading `wrap it in a <div>` is rejected. The tag pattern cannot
distinguish that from injected markup. This is a known cost, accepted so that
prose fields cannot carry markup.

What is *not* affected, and is covered by tests so it stays that way:
`5 < 10`, `a -> b`, `50%`, `O'Brien & Sons`, `{"a":1}` quoted mid-sentence,
and every non-Latin script.

## 4. Adding a new field — the standard

Every new feature with user-controlled input follows this. It is not optional,
and it is not a later cleanup pass.

1. **List every user-controlled field.** Include search boxes, filters, sort
   parameters, ids and pagination — not just the obvious form inputs.
2. **Give each one an expected type.**
3. **Assign a rule from section 3.** Only invent a new rule for a genuinely new
   kind of data; a different length is a `max_length` override, not a new rule.
4. **Add client validation** in the frontend and/or desktop, whichever the
   feature touches.
5. **Add backend validation. Always.** Even when both clients already validate.
6. **Confirm invalid input is refused** and produces a friendly message.
7. **Confirm valid input is not blocked** — including Unicode, punctuation and
   passwords with special characters.
8. **Add tests for both**, in every layer you touched.

### Backend

```python
from app.core.validation import Name, OptionalDescription, Identifier, Email

class WidgetCreate(BaseModel):
    widget_name: Name
    description: OptionalDescription = None
    owner_id: Identifier
    contact: Email
```

Need a different limit? Use the factory, don't hand-roll a validator:

```python
short_code: Annotated[str, name_field(max_length=32, label="Short code")]
```

Rejections raise a `ValueError` subclass, which FastAPI turns into its standard
`422 {"detail": [...]}`. No new error envelope, and no 500.

**Any user text reaching a SQL `LIKE` must go through `like_pattern()`** paired
with `escape=LIKE_ESCAPE_CHARACTER`. Unescaped, a search for `100%` matches
every row and a lone `%` turns a filtered query into a full table scan:

```python
column.ilike(like_pattern(search), escape=LIKE_ESCAPE_CHARACTER)
```

### Desktop

```python
from core.validation import validate_description, validate_identifier

result = validate_description(
    self.desc_input.toPlainText(), field_label="Description", required=True
)
if not result.ok:
    self._show_error(result.error)
    return
payload["description"] = result.value      # the normalised value, not the widget text
```

Validators return a result rather than raising, so validation failures and real
faults stay on separate paths and no submit handler needs a broad `except`.
Always submit `result.value`; re-reading the widget throws the normalisation
away. Set `setMaxLength()` from the shared limits so an over-long value cannot
be typed at all.

Validation is synchronous and instant — never hand it to the `TaskRunner`.

### Frontend

Use `useFormValidation` for per-field errors, or call a `validate*` function
directly. Mirror the limit into the input's `maxLength`, and wire
`aria-invalid`/`aria-describedby` so the error is announced.

## 5. Error messages

Client messages are written for the person who typed the value:

> "Please enter a valid email address."
> "Description must be at most 5000 characters."
> "This field contains unsupported content."

They never expose a regular expression, an internal limit's origin, a stack
trace, a SQL fragment, or anything about how the check works. The one
exception is `ENUM`, which names the permitted values — those are part of the
API's public contract, and a caller cannot fix the request without them.

Backend errors follow the existing convention: FastAPI's `422` for schema
validation, `HTTPException` elsewhere.

## 6. What this does not replace

Input validation is a layer, not the whole defence.

* **SQL injection** is prevented by SQLAlchemy's parameter binding. Never build
  a query by string concatenation. `like_pattern()` escapes *wildcards*, which
  is a correctness and load concern, not injection protection.
* **Output encoding** is React's job on the web. Validation reduces what can be
  stored; it does not make unescaped rendering safe.
* **Authorisation** is separate. A perfectly valid `project_id` may still be one
  this user may not touch — that is `app/core/permissions.py`.

## 7. Verifying

```bash
# Backend
cd backend && python -m pytest tests/test_validation_framework.py -q

# Desktop  (also checks the desktop and backend catalogues still agree)
cd desktop && python -m pytest tests/test_validation_framework.py -q
cd desktop && python tools/check_architecture.py

# Frontend
cd frontend && npm run test && npm run build && npm run lint
```
