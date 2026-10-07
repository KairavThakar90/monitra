# Client onboarding: invitation, password, sign-in

How an external client gets an account and signs in. A client is a `client`-role
user who can read the projects an administrator shares with them and nothing
else (`clients:view_shared`); the client portal is `frontend/src/features/client/`.

```
admin adds the client            Admin > Clients > + Add Client  (email + projects + what they may see)
        |
        v
email to the client              "You're invited to Monitra"  -- [Set Your Password]  [Not Needed / Reject]
        |
        v
set-password page                /client/set-password/<token>   (public; opened from the email)
        |  chooses a password -> invitation accepted, account activated, NO session issued
        v
login screen                     /login?client_invite=password_set   (email filled in, "password has been set")
        |  email + password
        v
client portal                    /client/dashboard
```

## The pieces

| Step | Where | Notes |
|---|---|---|
| Invite | `POST /api/v1/clients/invitations` (`clients:manage`) | Creates the `Client`, an **inactive** `client` user with **no password**, and a single-use token (only its hash is stored). Sends the email immediately; the token is never written to the outbox. |
| The email | `build_client_invitation_email` + `templates/emails/client_invitation.html` | *Set Your Password* -> `MONITRA_APP_URL/client/set-password/<token>`. *Reject* -> `API_BASE_URL/clients/invitations/<token>/reject`. Names how long the link lasts (`CLIENT_INVITATION_EXPIRE_HOURS`, 72). Never CC'd (`copy_exempt`). |
| Open the link | `GET /api/v1/clients/invitations/{token}` | Read-only. Returns `{email}` so the page can say which account this is, and reports a dead link *before* anyone types a password. Claims nothing. |
| Choose a password | `POST /api/v1/clients/invitations/{token}/password` | `{password}`, the shared `Password` rule (8-128, length only). Hashes it, claims the link, sets `password_hash`, activates the account (`Client.status`, `User.is_active`, `User.status`). Returns `{email}`. **Issues no session.** |
| Sign in (client) | `POST /api/v1/auth/client/sign-in-method` then `POST /api/v1/auth/client/login` | See below. |

No schema change: `users.password_hash` and `client_invitations` already existed.

## Signing in

The web sign-in form posts a staff member's password straight to the external
provider, never to this backend. A client's password is the opposite: it goes to
this backend and never to the provider. The form decides with a question that
carries **the email alone**:

1. `POST /auth/client/sign-in-method {email}` -> `{password_required}`. True only
   for an *active* client who chose a password. It is false for everyone else
   (staff, unknown address, an older client with no password, a deactivated
   client), so it says no more than a client's own sign-in screen would. If it
   cannot be answered (slow, down) the form reads that as "no" and signs in the
   way it always has: an outage here must never become a staff outage.
2. If true: `POST /auth/client/login {email, password}` -> a session.
   Any failure (no such address, not a client, wrong password, not active) is the
   same `401 Invalid email or password`. An administrator's exclusion
   (`can_login`) is still honoured, and is only revealed once the password is right.
3. If false: the provider flow, unchanged (`signInAPI` in `frontend/src/api/auth.ts`).

`/auth/login` (the desktop's endpoint) never signs a client in.

## Security properties (each has a test)

* **The link is single use.** Claimed with a conditional `UPDATE ... WHERE status = 'pending'`;
  two submissions race to it and exactly one wins. Expired, used, rejected,
  unknown and replaced links all answer with the same `401`.
* **Reading the link uses nothing up**, so neither opening the page nor a mail
  scanner fetching it can spend the invitation. The old *Approve* link used to
  approve on a plain GET; it is now only a redirect to the page and changes nothing.
* **A newer invitation replaces older ones.** When a client accepts, every other
  pending link they were sent becomes `superseded`; an old link in a mailbox
  cannot change a password that has been chosen. A link for an already-active
  client is refused.
* **A link can only reach a `client` account.** Never a staff account, whatever the rows say.
* **The password is hashed before the link is claimed**, so a failure there does
  not burn a link that did nothing.
* **Passwords are never trimmed, normalised or logged** (docs/VALIDATION.md).
  Sign-in uses the `Credential` rule (upper bound only) so an account made under
  an older policy is not locked out.
* **The browser never puts the password in a URL**, and the set-password link
  does not use `?token=`, which `AuthProvider` would consume as a single sign-on token.

## Older clients (accounts with no password)

Clients invited before this change have no password and keep working exactly as
before: signing in with the email alone (`POST /auth/client/login` without a
password). **From the moment a client has a password, that shortcut is refused
for them (`401`, "Enter your email address and password to sign in.")** --
otherwise the password would be decoration. Nobody is locked out by the change.

Giving an older client a password: `Deactivate`, then `Resend` in Admin > Clients
(a resend is refused for an *active* client), then they follow the new email.

Turning the email-only shortcut off for everyone is a product decision (it is
"deliberately weaker than every other credential in this system", see
`AuthService.client_direct_login`); do it once every client has a password.

## Configuration and deployment

* `MONITRA_APP_URL` **must be set** (e.g. `https://staff.peakworkos.com`): it is where the
  *Set Your Password* button points. `API_BASE_URL` is still needed for *Reject*.
  Unset, the email still sends but its buttons are broken relative links, and the
  backend logs `CLIENT_INVITATION_LINKS_UNCONFIGURED`.
* **Deploy the frontend before the backend.** An invitation sent by the new backend
  links to a page the old frontend does not have; an invitation sent by the old
  backend (with an *Approve* link) still works against the new frontend, because
  that link now forwards to the new page.
* Invitations sent before the change are still valid: their *Approve* button now
  leads to the set-password page, so those clients are also asked for a password.

## Known limits

* **No "forgot password" for clients yet.** Recovery is the Deactivate + Resend above.
* **bcrypt reads at most 72 bytes of a password** (`bcrypt==4.3.0`, pinned). A longer
  password is accepted and works, but only its first 72 bytes count. The policy
  allows 128 characters.
* The *Reject* link is still a plain GET, so a mail scanner that follows it would
  reject the invitation. (Unchanged by this work; *Set Your Password* is not exposed to it.)
