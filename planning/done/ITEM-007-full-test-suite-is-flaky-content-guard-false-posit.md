---
# id is assigned by /scope on pickup — leave it blank
id: ITEM-007
type: bug
title: content_guard hard-blocks real names (Matsushita/Harshita/Scunthorpe) + flaky phone-test entropy
priority: P2
effort: M
owner: backend
opened: 2026-06-29
depends_on: []
related: [ITEM-005, ITEM-006]   # surfaced while building these
links: []
---

# Full test suite is flaky — content_guard false-positive on random test emails + a phone user left uncleaned

## What & Why
`bin/run_tests --agent` (the default suite) is **flaky**: a single, *different* test
fails on most full runs (~1 failure / 2255), while every module passes in isolation.
This breaks the "green baseline" invariant the build process depends on (a green run
is luck, not a guarantee). Two independent root causes were identified during the
ITEM-005/006 builds (2026-06-29); both are pre-existing and unrelated to those items.

## Acceptance Criteria
- [ ] The default suite passes **reliably** across repeated full runs (e.g. 5×) — 0 failures.
- [ ] Cause 1 (content_guard false-positive on random test emails) fixed.
- [ ] Cause 2 (a test leaving a phone user uncleaned) fixed — the polluting test cleans up.
- [ ] No test masks real behavior or asserts a bug; isolation is restored.

## Repro — bugs only
Run `bin/run_tests --agent` several times. Observe ~1 failure per full run, a
*different* test each time, from the two families below. `bin/run_tests --agent -t
test_register` alone passes 94/0 (both flaky tests pass in isolation).

Observed instances:
- `test_register::test_register_handler_raise_rolls_back` → 400 "Invalid display name:
  contains inappropriate content" instead of the expected 5xx.
- `test_register/configurable_form::test_phone_register_verify_account_exists` →
  "account_exists must be False for a phone with no account" (it came back True).

## Investigation
**Cause 1 — content_guard false-positive on random emails (flaky).**
`_fresh_email()` (`tests/test_register/register.py:51`, `tests/test_register/extra_fields.py:26`)
returns `reg_<suffix>_<uuid4().hex[:8]>@register.test`. The register flow derives a
display name from this; `User.validate_name_fields` (`mojo/apps/account/models/user.py:570-587`)
runs `content_guard.check_text(value, surface="name", policy={"text_block_threshold": 50})`,
which **occasionally returns `decision == "block"` on a random hex string**, 400-ing the
registration before the test's intended path runs. Fix options: make test emails/display
names content-guard-safe/deterministic, OR (if content_guard is over-blocking benign
hex) treat that as a real content_guard tuning bug and fix the heuristic/threshold.

**Cause 2 — phone-number test-data pollution (isolation).**
`configurable_form::test_phone_register_verify_account_exists` expects `account_exists`
False for a phone with no account, but a prior test in the full run created a User with
that phone and did not delete it (violates the rule "setup must clean up test data before
creating it"). Find the polluting phone-register/login test using the same number and add
cleanup (delete by phone_number in setup/teardown), or make each test use a unique phone.

Both fail ONLY in the cross-module full suite; both pass when `test_register` runs alone.
Neither is caused by ITEM-005 or ITEM-006.

**Regression-test feasibility:** MEDIUM — loop the full suite N× in CI and require 0
failures; add a unit test that `content_guard.check_text` does not block plain hex/ascii
identifiers; assert the phone test is isolation-safe (no pre-existing user for its number).

## Plan

### Goal
Make the full test suite reliably green by fixing its two flaky tests at the root —
and fix the underlying production bug one of them exposed: content_guard **hard-blocking
legitimate display names** that merely *contain* a profanity substring (Matsushita,
Harshita, Scunthorpe).

### Context — what exists
**Cause A — content_guard over-blocks real names (production bug + the email flake).**
- `User.validate_name_fields` (`mojo/apps/account/models/user.py:570-587`), called during
  user creation via `on_rest_pre_save` (user.py:858), checks `display_name`/`first_name`/
  `last_name`:
  ```python
  584      result = content_guard.check_text(value, surface="name", policy={"text_block_threshold": 50})
  585      if result.decision == "block":
  586          label = field.replace("_", " ")
  587          raise merrors.ValueException(f"Invalid {label}: contains inappropriate content")
  ```
- `display_name` is auto-derived when not supplied: `generate_display_name()` (user.py:749-772)
  → email local-part → replace `_`/`.` with space → Title-case. So `reg_rollback_<hex>@...`
  → "Reg Rollback <Hex>".
- content_guard (`mojo/helpers/content_guard/core.py:260` `check_text`) is **deterministic**
  (no randomness/LLM). It matches `data/deny.txt` as **naive substrings** (core.py:286:
  `if term in searchable`) over a normalized form that strips punctuation and applies
  leet-decode (`normalize.py:204-229`, LEET_MAP digits→letters). High-severity terms
  (`data/high_severity.txt`: cunt, shit, fuck, slurs…) score `high_sev_weight=50`; the name
  policy's `text_block_threshold=50`. **A single high-severity substring → score 50 → block.**
  - Verified real-world casualties: **Matsushita** & South-Asian names **Harshita/Akshita/
    Shital** (contain "shit"), **Scunthorpe** ("cunt") → all hard-blocked at signup. (Lower-sev
    substrings like "ass" in "Cassidy" score 30 → warn/allow, so only high-sev substrings block.)
  - A safelist (`data/safe.txt`, applied core.py:298-305) spares a curated set ("assistant",
    "hancock", "shitake") but doesn't scale to all names.
- The random test emails hit this same trap ~0.1% of runs → the flaky 400 in
  `test_register::test_register_handler_raise_rolls_back` (register.py:386).

**Cause B — `_fresh_phone()` collisions (the phone flake).**
- `tests/test_register/configurable_form.py:29-34`:
  ```python
  def _fresh_phone():
      suffix = _uuid.uuid4().hex[:7]
      digits = "".join(c for c in suffix if c.isdigit()).ljust(7, "1")[:7]
      return f"+1555{digits}"
  ```
  Extracts ONLY the digit chars from 7 hex chars and pads with "1" → a tiny, biased space
  (letter-heavy uuids → "1111111"). Under parallel execution (`-j 3`, shared DB — see
  `docs/django_developer/testit/Overview.md`), two tests generate the **same** `+1555…`, so
  `test_phone_register_verify_account_exists` (configurable_form.py:314) sees another test's
  leftover user → `account_exists` True instead of False.
- Well-behaved phone tests already clean up (try/finally `User.objects.filter(phone_number=
  phone).delete()`, e.g. configurable_form.py:206-226) — but **cleanup can't fix a parallel
  race; only uniqueness can.**

### Changes — what to do
1. **`mojo/apps/account/models/user.py` — `validate_name_fields` (570-587): flag, don't block.**
   On `result.decision == "block"` for a name field, do NOT raise — **log a warning** and
   continue (allow the save):
   ```python
   if result.decision == "block":
       logger.warning(
           "flagged display-name content (allowed): user=%s field=%s reasons=%s",
           self.username or self.email or "<new>", field, result.reasons)
   ```
   Use the project logit logger (`from mojo.helpers import logit`; `logger = logit.get_logger(
   "account", "account.log")` — match existing usage in this module; do NOT use stdlib logging).
   Remove the `raise merrors.ValueException(...)`. Keep the loop/skip logic otherwise unchanged.
   Display-name moderation becomes **advisory** — registration never fails on a name; abusive
   names are surfaced in logs for review.
   - content_guard core and the `comment`/`chat`/`contact_form` surfaces are **untouched** (they
     still block). The change is localized to the name path by living in `validate_name_fields`.
2. **`tests/test_register/configurable_form.py` — `_fresh_phone` (29-34): full entropy.**
   ```python
   def _fresh_phone():
       # 7 fully-random digits — 10M space, no cross-test collisions under -j parallelism.
       return f"+1555{_uuid.uuid4().int % 10_000_000:07d}"
   ```
   Grep `def _fresh_phone` across `tests/` and apply the same to any other copy (recon found
   only this one — confirm). `_fresh_email` is **NOT** changed — change #1 removes the
   registration block, so the email-derived-name flake disappears at the root.
3. **Docs + CHANGELOG** (see Docs).

### Design decisions
- **Flag-don't-block for names** (user-approved). Name-based signup blocking has a high
  false-positive cost — the "Scunthorpe problem": substring matching cannot distinguish
  "Harshita"/"Matsushita" from real profanity without over-blocking real users (especially
  South-Asian names). Advisory logging eliminates all false-positive *blocks* while still
  surfacing abusive names. *Rejected:* word-boundary matching (still over-blocks "Shital";
  can't perfectly solve substring precision and would weaken comment moderation if global);
  safe-list expansion (whack-a-mole). A future tightening (hard-block only an exact whole-word
  slur) can layer on later.
- **Fix in the caller (`validate_name_fields`), not content_guard core** — keeps content_guard's
  scoring intact for comment/chat/contact_form (which legitimately want aggressive substring
  matching); minimal change.
- **No `_fresh_email` change** — change #1 makes it unnecessary (KISS). The flag is
  `logit.warning` only (no DB incident), so the ~0.1% of test registrations that now log a flag
  don't pollute any table.
- **`_fresh_phone` entropy, not extra cleanup** — collisions (not missing teardown) are the root
  cause; full entropy removes the race regardless of timing. Existing try/finally cleanups stay.

### Edge cases & risks
- **An abusive display name (e.g. a slur) is now allowed-but-logged** rather than blocked at
  signup — accepted tradeoff (user-approved); flagged in logs for moderation; easy to re-tighten.
- **logit timing** — `validate_name_fields` runs pre-save; `logit.warning` is always safe (no
  DB/FK dependency). Avoid `report_incident` here (it may need a saved user PK and would write
  test-time rows).
- **Other `surface="name"` callers?** Only `validate_name_fields` (verified: `public_message`
  uses `contact_form`, `chat` uses `chat`) — change is fully localized.
- **`_fresh_phone` other copies** — grep and fix all.
- **Residual flakiness** — if any third flaky test exists, repeated full runs surface it; the AC
  requires 5× green.

### Tests
testit (`from testit import helpers as th`). Add:
1. **content_guard name over-block regression (the production fix):** create/save a `User` (or
   call `validate_name_fields` directly) with `display_name` ∈ {"Matsushita", "Harshita",
   "Scunthorpe"} and assert it **succeeds — no `merrors.ValueException`**. FAILS on current code
   (raises), PASSES after. Also assert a plainly-fine name ("Jane Smith") still saves. Home:
   near existing User name tests (grep `validate_name_fields` / "inappropriate content" in
   `tests/` — likely `tests/test_account/` or `tests/test_register/`).
   - Optional: in `tests/test_helpers/content_guard.py`, assert content_guard STILL scores these
     as `block` — documents that the guard is unchanged and only the caller's response changed.
2. **`_fresh_phone` uniqueness:** call it ~1000× and assert ~1000 unique results. FAILS on the
   current low-entropy impl (collisions), PASSES after. Home: `tests/test_register/configurable_form.py`.
3. **Stability (acceptance):** after the fix, run `bin/run_tests --agent` ≥5× and confirm 0
   failures each. The deterministic per-cause regressions above are the durable guard; the
   repeat-run is the acceptance check.

### Docs
- `docs/django_developer/helpers/content_guard.md` — document that the **name surface is
  advisory**: display-name validation flags rather than hard-blocks, because substring matching
  over-blocks legitimate names; content_guard's scoring is unchanged.
- `docs/django_developer/account/*` (wherever display-name rules live) — note display names are
  moderated advisorily (flagged, not rejected at signup).
- `CHANGELOG.md` — "account: display-name moderation no longer rejects registration for names
  that merely contain a profanity substring (e.g. Matsushita, Harshita, Scunthorpe); such names
  are allowed and logged for review. Fixes false-positive signup blocks."

### Open questions
None blocking. Optional future item: route flagged names to a moderation/review queue and add an
opt-in hard-block for exact whole-word slurs — out of scope here.

## Notes
**Build baseline (2026-06-29, `bin/run_tests --agent`):** `status: passed` — total 2255,
passed 2199, **failed 0**, skipped 56. Green THIS run, but the suite is known-flaky (this
item's target). Pre-existing flaky set accepted by the user:
`test_register::test_register_handler_raise_rolls_back` and
`configurable_form::test_phone_register_verify_account_exists`. Any OTHER new failure after
my change is mine; after the fix those two must be stable across ≥5 runs.

Surfaced during ITEM-006 build: two consecutive full-suite runs produced two *different*
single failures, both green in isolation — the signature of flakiness, not a regression.
The content_guard angle may be a real product bug (over-blocking benign strings), not just
test data — worth confirming during scope.

## Resolution
- closed: 2026-06-29
- branch: main
- files changed: mojo/apps/account/models/user.py, tests/test_account/test_name_moderation.py, tests/test_register/configurable_form.py, tests/test_register/passwordless.py, tests/test_auth/forgot_password_phone.py, docs/django_developer/account/user.md, docs/django_developer/helpers/content_guard.md, docs/web_developer/account/user.md, docs/web_developer/account/user_self_management.md, CHANGELOG.md   (close.sh stamp trimmed of intervening unrelated commits)
- tests added: `tests/test_account/test_name_moderation.py` (legit names Matsushita/Harshita/Scunthorpe allowed + clean name still allowed + content_guard scoring unchanged); `tests/test_register/configurable_form.py::test_fresh_phone_is_unique`. `_fresh_phone` fixed in 3 copies (configurable_form, passwordless, forgot_password_phone).
- verification: full suite green across **5/5** consecutive runs (was ~1 random failure/run). The two pre-existing flaky tests are now deterministically stable.
- security review: passed (validate_name_fields was only a profanity gate, never an injection guard; downstream surfaces escape independently; log records identifier + reason codes, not the name value). docs: both tracks + content_guard.md updated.
