# Screenshot privacy: rules, members, and who a new rule applies to

Which applications and websites are blanked out of a member's screenshots.
Admin page: **Settings > Screenshot Privacy** (`/admin/settings/screenshot-privacy`).

## The model

| Table | Meaning |
|---|---|
| `screenshot_applications` / `screenshot_urls` | The **shared catalogue** of rules (no organization column). |
| `screenshot_exclusions` | One row per member per rule that is **switched on** for them (`user_id`, `application_id` *or* `url_id`, `is_excluded`). |

A catalogue rule does nothing for a member until they have an exclusion row for it.
The desktop reads the caller's own rows from `GET /api/v1/screenshot/privacy-config`
and skips those applications/URLs while capturing.

## The page

* **Member picker** (top). The same filter the Reports page uses (`MemberMultiSelect`),
  in its one-at-a-time mode: it names the member, closes on a pick and shows that
  member's rules, each with a Captured / Excluded switch. Offered: active members who
  run the desktop app (not clients, not the release service account).
* **The rule list** (`PrivacyRuleCatalogue`). Until a member is picked, the page shows every rule
  that has been added, as **Applications** and **Websites** tabs (the same tab strip as the Feedback
  page, with counts): name, process or domain, URL pattern for a website, category, Active/Inactive,
  and the date added, newest first, with a search box. It opens on whichever tab has rules in it.
  It is read-only. Picking a member replaces it with that member's Captured / Excluded switches.
* **+ Add Privacy Rule** opens a dialog: *Application* or *Website URL*, its details,
  and **Applies to**, the same member filter in several-at-a-time mode.

### Applies to

* **All members** (the default, as on every report page) switches the rule on for every
  active member of the organization *at that moment*.
* **Chosen members** switches it on for exactly those people.
* The dialog states which, with a count, before you save.

Either way the rule is written as ordinary per-member exclusion rows, so it can still be
turned off for any one member afterwards from the member picker.

**A member who joins later is not covered by "All members".** There is no standing
"everyone" flag, only the rows that exist; a new hire needs the rule switched on for them
(or the rule added again). Making "all" a live setting would need a column on the catalogue
and a change to what `privacy-config` returns; it was deliberately not done here.

## The API

`POST /api/v1/screenshot/applications` and `POST /api/v1/screenshot/urls` take an optional
`apply_to`:

```json
{ "name": "Slack", "process_name": "slack.exe", "category": "Communication",
  "apply_to": { "scope": "all" } }

{ "...": "...", "apply_to": { "scope": "members", "user_ids": [12, 15] } }
```

The response is the rule plus `applied_to_count`.

* **Omit `apply_to`** and nothing changes from before: the rule is only added to the catalogue.
* **Only an administrator** (`administrator`, `org_admin`, `super_admin`) may send `apply_to`;
  anyone else gets `403`. Checked before anything is written.
* **One transaction.** The rule and its exclusions commit together. An unknown member, a member
  of another organization, a client or the service account answers `400` and **creates nothing**
  (not the rule either); so does a failure part-way.
* `user_ids` is the shared bounded id list: at least one for `members`, none for `all`,
  duplicates refused (`422`).
* Applying is idempotent: a member who already has a row (even switched off) has it switched
  on, nobody gets two.

Code: `backend/app/services/screenshot_privacy.py`; tests: `backend/tests/test_screenshot_privacy_apply.py`
and `frontend/src/features/admin/__tests__/screenshotPrivacyMembers.test.tsx`.

## Known gap (predates this work)

The catalogue and per-member exclusion endpoints (create/update/delete a rule, read or change any
user's exclusions) have **no role check**: any signed-in user can call them, for any user id, and the
catalogue is not scoped to an organization. Only the new `apply_to` is administrator-gated. Closing
the rest means gating those routes on the same administrator set and scoping `user_id` to the
caller's organization; it is a separate change because it alters who can do what today.
