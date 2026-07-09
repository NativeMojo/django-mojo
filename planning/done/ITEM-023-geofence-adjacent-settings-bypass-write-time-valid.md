---
# id is assigned by /scope on pickup — leave it blank
id: ITEM-023
type: bug
title: Geofence-adjacent Settings bypass write-time validation; kind= coercion silently absorbs garbage
priority: P2
effort: M
owner: backend
opened: 2026-07-08
depends_on: []
related: [ITEM-017]
links: []
---

# Geofence-adjacent Settings bypass write-time validation; kind= coercion silently absorbs garbage

## What & Why

Found during mverify_api MVERIFY-API-014's post-build security review
(2026-07-08). ITEM-017 added write-time validation for DB-backed geofence
Settings, but `Setting.GEOFENCE_KEYS` covers only `GEOFENCE_SYSTEM_RULES`
and `GEOFENCE_ALLOWLIST` (`mojo/apps/account/models/setting.py:86,96-127`).
Two settings that now carry real enforcement stakes are writable as garbage
through the generic `POST /api/settings` (perms `manage_settings`/`groups`)
with no validation:

- `GEOFENCE_FAIL_CLOSED_SCOPES` — read `kind="list"`; a malformed value
  silently changes which scopes fail closed (money endpoints could quietly
  revert to fail-open).
- `GEOFENCE_ALLOW_PRIVATE_IPS` — read `kind="bool"`; an unrecognized string
  falls through to Python truthiness, so `bool("some-typo")` is `True`
  (allow) — the unsafe direction.

Compounding it, `settings.get(kind=...)` coercion is silently lenient
(`mojo/helpers/settings/helper.py:96-118`, `objict.from_json(...,
ignore_errors=True)`): a present-but-unparsable value coerces to the empty/
default shape with no log, so "someone wrote garbage" is indistinguishable
from "unset" at every read site. mverify_api fixed its own app-level key
(`PAYMENTS_GEOFENCE_RULES`) by reading the raw value and denying on
present-but-unparsable (`apps/mojopay/payments/services/geo_gate.py`); the
framework keys remain exposed for every deployment.

## Acceptance Criteria

- [ ] `GEOFENCE_FAIL_CLOSED_SCOPES` and `GEOFENCE_ALLOW_PRIVATE_IPS` (and
      any other geofence-posture key) get write-time validation — extend
      `Setting.GEOFENCE_KEYS` or generalize it into a per-key validator
      registry other apps can register into (mverify's
      `PAYMENTS_GEOFENCE_RULES` would use it).
- [ ] Coercion failure is observable: `settings.get(kind=...)` (or at least
      the geofence read paths) logs when a present value fails to coerce,
      instead of silently returning the default shape.
- [ ] `bool` coercion of unrecognized strings does not default to `True`
      for allow-flavored settings (decide: strict parse with log-and-default,
      or explicit truthy-string whitelist).
- [ ] Tests in `tests/test_geofence/` cover a garbage write to each key:
      rejected at write time, and read-path behavior pinned.

## Plan

**Approved 2026-07-09.** Shape: generalize Setting write-validation into a per-key
validator registry (enforced in BOTH the REST hook and `save()`), extend it to all
geofence posture keys (which also fixes a cache-invalidation gap), and make
`kind=` coercion failures loud with a default-aware strict-bool rule.

**Premise correction vs the item text above:** `GEOFENCE_STRICT_POSTURE` is
ALREADY validated — ITEM-021 made `GEOFENCE_KEYS` a 3-tuple. The genuinely
unvalidated posture keys are five: `GEOFENCE_ENABLED`, `GEOFENCE_FAIL_CLOSED`,
`GEOFENCE_FAIL_CLOSED_SCOPES`, `GEOFENCE_ALLOW_PRIVATE_IPS`, `GEOFENCE_CACHE_TTL`.

### Goal

A garbage value can no longer be written to any geofence-consumed Setting through
ANY path (REST, `Setting.set`, shell), coercion failures at read time are logged
instead of silently absorbed, unrecognized bool strings no longer coerce to
`True`, and downstream apps (mverify's `PAYMENTS_GEOFENCE_RULES`) can register
their own key validators.

### Context — what exists (verified 2026-07-09 @ HEAD 9c8ba53)

**Setting model** (`mojo/apps/account/models/setting.py`):
- `GEOFENCE_KEYS = ("GEOFENCE_SYSTEM_RULES", "GEOFENCE_ALLOWLIST",
  "GEOFENCE_STRICT_POSTURE")` (:86-87). This ONE tuple wires two behaviors:
  - **Write validation** — `on_rest_pre_save` (:89-95) calls
    `_validate_geofence_value()` (:97-134): non-listed keys short-circuit
    (:101-102); group-scoped rows rejected `"{key} is a global-only setting…"`
    (:104-109); secret skip (:110-111); empty string = unset, early return
    (:113-115); `json.loads` else `ValueException("{key} must be valid JSON")`
    (:116-119); then a HARDCODED if/elif (:120-134): `GEOFENCE_SYSTEM_RULES` →
    lazy `dsl.validate_rule(parsed)`; `GEOFENCE_STRICT_POSTURE` →
    `isinstance(parsed, bool)` else ValueError (comment there already names this
    item's bug: *"kind='bool' coerces any unrecognized string truthy at read
    time, so garbage must be rejected here"*); else → lazy
    `engine.validate_allowlist(parsed)`; ValueError wrapped to
    `merrors.ValueException(str(exc))`.
  - **Decision-cache invalidation** — `_invalidate_geofence_decisions()`
    (:305-315), called from Django `save()` (:295-298) and `delete()` (:300-303);
    gate at :309: `if self.group_id is not None or self.key not in
    self.GEOFENCE_KEYS: return`, else `gf_cache.invalidate_all()`.
- **Validation is REST-hook-only.** `on_rest_pre_save` is invoked from the
  framework REST save (`mojo/models/rest.py:1268`). Django `save()` does NOT
  validate — so `Setting.set()` (:268-279 — plain `s.save()`),
  `Setting.objects.create(...)`, and shell writes all bypass validation today.
  (The `/api/geo/*` endpoints validate BEFORE calling `Setting.set` —
  `rest/geofence.py:234,246,317` — so that path is safe; the generic gap is
  programmatic/shell writes.) Cache invalidation, by contrast, IS in
  `save()`/`delete()` and covers all paths.
- **Storage**: `key` CharField(255, db_index) / `value` TextField (JSON is
  `json.dumps`'d by `set_value`, :58-71 — no kind column, not JSONField);
  `unique_together ("key","group")` (:33); `RestMeta VIEW_PERMS = SAVE_PERMS =
  ["manage_settings", "groups"]` (:36-48). REST endpoint:
  `mojo/apps/account/rest/setting.py` (9 lines, `uses_model_security(Setting)`
  passthrough).

**Cache-invalidation gap (bonus finding to fix here):** the five unvalidated
posture keys also don't invalidate the decision cache. Of them, only
`GEOFENCE_ALLOW_PRIVATE_IPS` matters: `private_ip` decisions ARE cached (engine
`_maybe_cache`; unlike never-cached `lookup_failed`), so flipping it True→False
via `/api/settings` leaves stale `private_ip: allowed` decisions live for up to
`GEOFENCE_CACHE_TTL`. (`ENABLED` is read pre-cache every request;
`FAIL_CLOSED`/`FAIL_CLOSED_SCOPES` affect only the never-cached `lookup_failed`
path; `CACHE_TTL` is forward-only.)

**Settings helper** (`mojo/helpers/settings/helper.py`):
- Module-scope imports are ONLY `importlib` + `objict` (:1-2); the singleton
  `settings = SettingsHelper()` is constructed at import (:201) and used very
  early in boot — keep it import-light. `logit` is NOT imported anywhere in the
  package today.
- `get()` (:122-143): dict-root sub-helper (:125-129) → DB overlay via
  `Setting.resolve` when `_django_ready()` (:131-137) → live django.conf
  fallback (:139-143). `_convert_value` applied at all three sites
  (:128, :135, :142). Freshness of DB values is the Redis hash maintained by
  the model's `push_to_cache`/`remove_from_cache` on save/delete (setting.py
  :154-171) — write-driven, no TTL.
- `_convert_value(self, value, kind, default=UNKNOWN)` (:69-120) — the name of
  the setting is NOT passed in (needed for logging; thread it through):
  - `int`/`float` (:73-83): except → default, silent.
  - `bool` (:85-94): real bool passes; string whitelists
    `["true","1","yes","on","y"]` / `["false","0","no","off","n",""]`; anything
    else falls to `return bool(value)` (:94) — **unrecognized non-empty string →
    `True`**. Confirmed bug.
  - `dict` (:96-105): string → `objict.from_json(value, ignore_errors=True)`;
    non-dict result silently falls to `default` or `{}`.
  - `list` (:107-118): `[`…`]` strings → `from_json(ignore_errors=True)`; if
    that does NOT yield a list it FALLS THROUGH to the comma-split at :115 — so
    a bracket-wrapped unparsable string becomes nonsense like
    `'["payments",]'` → `['["payments"', ']']`. Plain strings comma-split
    (legit CSV form). Non-str non-list → default, silent.
  - **No logging anywhere.**
- `get_static`/`__getattr__` (:145-163, :193-197) skip the DB overlay entirely
  (file-only) — untouched by this item.

**Read sites of the geofence keys** (all resolve through `settings.get`):
engine wrappers with per-request test-header overrides (`engine.py:86-110`):
`GEOFENCE_ENABLED` bool (engine.py:386-387, 456-457), `GEOFENCE_STRICT_POSTURE`
bool (:138-139), `GEOFENCE_CACHE_TTL` int (:411-412), `GEOFENCE_FAIL_CLOSED`
bool (:489-490), `GEOFENCE_FAIL_CLOSED_SCOPES` list (:492-494),
`GEOFENCE_ALLOW_PRIVATE_IPS` bool (:502-503), `GEOFENCE_SYSTEM_RULES` dict
(:119), `GEOFENCE_ALLOWLIST` list (:159). Plain reads: `rest/geofence.py`
posture block (:188-193), `on_geo_policy` (:112,:117,:119), allowlist reads
(:165,:280); `evidence.py:145` (scopes list).

**Bool blast radius**: 32 `kind="bool"` sites repo-wide. Security-relevant
default-False (garbage→True is unsafe today): `MOJO_TEST_MODE`
(`mojo/helpers/test_mode.py:43` — master gate for test-header overrides;
mitigated by loopback checks), `ALLOW_PHONE_LOGIN` (`rest/user.py:164`),
`LLM_ADMIN_ENABLED` (assistant). Default-True flags (must NOT flip off on
garbage): `LLM_ADMIN_MEMORY_ENABLED`, `LLM_ADMIN_SKILLS_ENABLED`,
`LLM_ADMIN_PROMPT_CACHE_ENABLED`, `GEOFENCE_ENABLED`,
`GEOFENCE_ALLOW_PRIVATE_IPS`, etc. → the fix must return the DECLARED default,
not a hardcoded False.

**logit conventions**: category-first convenience calls — geofence code uses
`logit.error("geofence", f"…")`; the analogous coercion warning is
`logit.warning("settings", f"…")` (`logit.warning`/`warn` both exist,
logit.py:75-80). Only `logit.py` may import stdlib logging. `logit` imports
only stdlib + `paths` — import-safe early; still, use a LAZY import inside the
coercion-failure branch to keep helper.py's module scope clean.

**Existing tests**:
- `tests/test_geofence/config_plane.py:170-204`
  `test_settings_rest_backdoor_validated` — the pattern to extend:
  `opts.client.post("/api/settings", {"key": SYSTEM_KEY, "value": '{"country":
  {"zap": []}}'})` → assert 400 + row absent; valid value → 200 + row; group-
  scoped row → 400 body contains "global-only". ITEM-021 added the same for
  `GEOFENCE_STRICT_POSTURE` in `tests/test_geofence/strict_posture.py`.
- `tests/test_helpers/secure_settings.py` — helper round-trips incl.
  `test_settings_helper_db_override` (:243-267, valid bools only). NO garbage-
  coercion tests exist.
- Hygiene (config_plane.py:1-13 + memory.md): these tests write REAL global
  Setting rows — rules must only bite `X-Mojo-Test-Geo`-headered requests; the
  DB allowlist must NEVER cover 127.0.0.1; every mutating test restores in
  `finally` (`_cleanup_settings` :24-29 = `Setting.remove(...)` +
  `gf_cache.invalidate_all()`); never persist a global strict=true row; prime
  cached denials only under group-scoped keys.

**Downstream consumer** (context; work happens in THAT repo later):
`/Users/ians/Projects/mojo/mojo-verify/mverify_api/apps/mojopay/payments/services/geo_gate.py:91-108`
`_payments_rules()` reads `PAYMENTS_GEOFENCE_RULES` RAW (no `kind=`), hand-parses
JSON, and fails CLOSED on present-but-unparsable — precisely because
`kind="dict"` silently coerces garbage to `{}` (= no restriction on a money
path). The registry lets mverify register that key for write-time 400s; a
strict-read API is deliberately NOT part of this item (see decisions).

### Changes — what to do

1. **`mojo/apps/account/models/setting.py`** — validator registry + save-time
   enforcement + full key family:
   - Class-level registry + registration API:
     ```python
     # key -> {"func": callable(key, parsed) raising ValueError, "global_only": bool}
     VALIDATORS = {}

     @classmethod
     def register_validator(cls, key, func, global_only=True):
         cls.VALIDATORS[key] = {"func": func, "global_only": global_only}
     ```
   - Generalize `_validate_geofence_value` → `_validate_value`: look up
     `self.key` in `VALIDATORS` (absent → return); keep the existing sequence
     verbatim — group-scoped rejection when `global_only` (same message),
     secret skip, empty-string early return, JSON parse →
     `ValueException(f"{self.key} must be valid JSON")` — then
     `entry["func"](self.key, parsed)` with `ValueError` →
     `merrors.ValueException(str(exc))`. Update `on_rest_pre_save` (:95) to call
     the renamed method.
   - **Also call `self._validate_value()` at the top of `save()`** (:295) —
     closes `Setting.set`/`objects.create`/shell writes. Double-validation on
     the REST path is idempotent and cheap.
   - Extend the family tuple:
     ```python
     GEOFENCE_KEYS = (
         "GEOFENCE_SYSTEM_RULES", "GEOFENCE_ALLOWLIST", "GEOFENCE_STRICT_POSTURE",
         "GEOFENCE_ENABLED", "GEOFENCE_FAIL_CLOSED", "GEOFENCE_FAIL_CLOSED_SCOPES",
         "GEOFENCE_ALLOW_PRIVATE_IPS", "GEOFENCE_CACHE_TTL")
     ```
     The invalidation gate (:309) is mechanically unchanged and now covers
     posture flips — fixing the `ALLOW_PRIVATE_IPS` staleness gap.
   - Module-bottom validators + registrations (lazy imports inside the two
     heavy ones, matching today's pattern):
     ```python
     def _validate_geofence_rule(key, parsed):
         from mojo.apps.account.services.geofence.dsl import validate_rule
         validate_rule(parsed)

     def _validate_geofence_allowlist(key, parsed):
         from mojo.apps.account.services.geofence.engine import validate_allowlist
         validate_allowlist(parsed)

     def _validate_json_bool(key, parsed):
         if not isinstance(parsed, bool):
             raise ValueError(f"{key} must be a JSON boolean (true/false)")

     def _validate_cache_ttl(key, parsed):
         if isinstance(parsed, bool) or not isinstance(parsed, int) or parsed < 0:
             raise ValueError(f"{key} must be a non-negative JSON integer")

     def _validate_scope_list(key, parsed):
         if not isinstance(parsed, list) or not all(
                 isinstance(s, str) and s.strip() for s in parsed):
             raise ValueError(f"{key} must be a JSON list of non-empty strings")

     Setting.register_validator("GEOFENCE_SYSTEM_RULES", _validate_geofence_rule)
     Setting.register_validator("GEOFENCE_ALLOWLIST", _validate_geofence_allowlist)
     Setting.register_validator("GEOFENCE_STRICT_POSTURE", _validate_json_bool)
     Setting.register_validator("GEOFENCE_ENABLED", _validate_json_bool)
     Setting.register_validator("GEOFENCE_FAIL_CLOSED", _validate_json_bool)
     Setting.register_validator("GEOFENCE_ALLOW_PRIVATE_IPS", _validate_json_bool)
     Setting.register_validator("GEOFENCE_CACHE_TTL", _validate_cache_ttl)
     Setting.register_validator("GEOFENCE_FAIL_CLOSED_SCOPES", _validate_scope_list)
     ```
     The `GEOFENCE_STRICT_POSTURE` error text stays identical to today's
     (ITEM-021's `strict_posture.py` backdoor test keeps passing). Update the
     :83-85 comment to describe the registry + family tuple.

2. **`mojo/helpers/settings/helper.py`** — observable coercion + strict bool:
   - Thread the name: `_convert_value(self, value, kind, default=UNKNOWN,
     name=None)`; pass `name=name` at the three call sites (:128, :135, :142).
   - Module-level warn helper with LAZY logit import:
     ```python
     def _warn_coercion(name, value, kind, default):
         from mojo.helpers import logit
         logit.warning("settings",
             f"setting {name!r}: cannot coerce {value!r} to {kind}; "
             f"using default {default!r}")
     ```
   - `int`/`float` except branches: call `_warn_coercion` before returning the
     default (behavior of the returned value unchanged).
   - `bool`: after the two string whitelists, replace the fallthrough for
     STRINGS — `if isinstance(value, str): _warn_coercion(...); return
     bool(default) if default is not UNKNOWN else False`. Non-string values
     keep `return bool(value)` (ints/None truthiness unchanged).
   - `dict`: when the value is present but doesn't yield a dict (unparsable
     string or wrong type), `_warn_coercion` then the existing default return.
   - `list`: when a `[`…`]`-wrapped string fails to parse as a list, warn and
     return the default — do NOT fall through to comma-split (that manufactures
     nonsense entries). Plain comma strings keep the :115 split; non-str
     non-list values warn + default.

3. **`tests/test_geofence/settings_validation.py`** — new module (see Tests).

4. **`tests/test_helpers/settings_coercion.py`** — new module (see Tests).

5. **Docs + `CHANGELOG.md`** — see Docs.

No model schema changes (registry is a class attr) → no `bin/create_testproject`
needed. No engine changes.

### Design decisions

1. **Registry lives on the `Setting` model** (class attr + classmethod), not in
   `mojo/helpers/settings` — write validation is a model-write concern; the
   helper stays read-side and import-light. Validator signature
   `func(key, parsed)` so shared validators (`_validate_json_bool`) produce
   key-specific messages without partials.
2. **Enforce in `save()` too, keep the REST hook.** REST keeps its readable 400
   before any side effects; `save()` closes `Setting.set`/`objects.create`/
   shell — the exact "unvalidated back door" class this item exists to close.
   Rejected: REST-only (user explicitly approved save-level).
3. **One 8-key family tuple keeps driving cache invalidation.**
   `invalidate_all()` on a rare admin write is cheap; ITEM-021's resolution
   explicitly praised the "add a key → validation + invalidation auto-wired"
   coupling. Rejected: separate validation/invalidation sets (drift risk — and
   the recon found exactly such a drift bug: `ALLOW_PRIVATE_IPS` staleness).
4. **Bool garbage-string → declared default + warning** (the item's sanctioned
   "strict parse with log-and-default"): garbage becomes *unset-but-loud*.
   Default-aware means default-True flags (LLM_*, GEOFENCE_ENABLED) do NOT flip
   off on garbage — no surprise outages — while default-False gates
   (`MOJO_TEST_MODE`, `ALLOW_PHONE_LOGIN`) stop failing open. Rejected:
   hardcoded False (flips default-True flags); truthy-whitelist-only-for-some-
   keys (leaves the other 30 bool sites exposed and adds a second semantic).
5. **Bracket-wrapped unparsable list strings → default + warn, never
   comma-split.** Comma-split of malformed JSON manufactures garbage entries
   (`'["payments",]'` → `['["payments"', ']']`) — for `FAIL_CLOSED_SCOPES`
   that silently changes which scopes fail closed. Plain CSV strings keep
   working.
6. **No `settings.get_strict` in this item.** The registry is the enabling
   piece for mverify (write-time 400s once it registers its key, in that repo);
   a raise-on-unparsable read API is a separable follow-up — file it there if
   geo_gate wants to delete its guard entirely.
7. **Lazy `logit` import in the failure branch only** — `helper.py`'s module
   scope stays `importlib`+`objict` (early-boot import profile preserved).

### Edge cases & risks

- `isinstance(True, int)` is `True` — the `CACHE_TTL` validator explicitly
  excludes bool before the int check.
- Empty-string value keeps its "unset" early-return semantics (REST clear-a-
  setting flows unaffected).
- Secret rows still skip validation (unchanged).
- Python-style `'True'` is not valid JSON → 400 `"must be valid JSON"` (readable;
  admins write `true`).
- `save()`-level validation + PRE-EXISTING garbage rows: only writes validate;
  reads now degrade to default + warning (pinned by the read-path test, which
  plants garbage via `queryset.update()` — bypasses `save()` — then refreshes
  the Redis settings cache before reading).
- Bool-coercion blast radius: behavior changes ONLY for present garbage strings
  (was `True`, becomes declared default + log). Full-suite baseline comparison
  at build time gates surprises. `MOJO_TEST_MODE` garbage now coerces False —
  strictly safer.
- Registry mutation in tests: register throwaway keys inside try/finally and
  pop them from `Setting.VALIDATORS` — never leave test validators behind.
- Test writes to real global keys: use DEFAULT-EQUAL values for the
  valid-write assertions (ENABLED `true`, FAIL_CLOSED `false`,
  ALLOW_PRIVATE_IPS `true`, CACHE_TTL `300`, SCOPES `["item023-scope"]` — the
  one non-default is scoped to a name no other module uses) and restore in
  `finally` via `Setting.remove(...)` + `gf_cache.invalidate_all()`
  (config_plane's `_cleanup_settings` pattern). Never touch 127.0.0.1
  allowlists; never persist strict=true.
- Import order: registrations run at `setting.py` module import (app registry);
  validators lazy-import dsl/engine only when a registered key is actually
  written — no import-time cost, no cycles.

### Tests

Both new modules use testit (`from testit import helpers as th`; run
`bin/run_tests --agent -t test_geofence.settings_validation` and
`-t test_helpers.settings_coercion`; read `testproject/var/test_failures.json`).
Every assert carries a failure message. Setup deletes/restores its own rows.

**`tests/test_geofence/settings_validation.py`** (server + in-process; mirror
`config_plane.py` setup: suffix-unique admin with `manage_settings` global
grant, `_login` helper that clears the login rate bucket):
1. REST garbage → 400 + row absent, for each of the five keys:
   `GEOFENCE_ENABLED` value `"garbage"`; `GEOFENCE_FAIL_CLOSED` value `"[1,2"`;
   `GEOFENCE_ALLOW_PRIVATE_IPS` value `'"yes"'` (JSON string, not boolean —
   exactly the truthy-coercion vector); `GEOFENCE_CACHE_TTL` values `'true'`
   (bool-as-int trap) and `'-5'`; `GEOFENCE_FAIL_CLOSED_SCOPES` values
   `'{"a":1}'` and `'["payments", ""]'` (empty entry).
2. REST valid, default-equal → 200 + row present, then `finally` restore:
   `true`/`false`/`true`/`300`/`["item023-scope"]` respectively.
3. Group-scoped row for a NEWLY-validated key (e.g. `GEOFENCE_ENABLED` with
   `group=<test group pk>`) → 400, body contains "global-only".
4. In-process shell-path enforcement: `Setting(key="GEOFENCE_ENABLED",
   value="garbage").save()` raises `ValueException`; same via
   `Setting.set("GEOFENCE_CACHE_TTL", -1)`. (This is the NEW `save()` hook.)
5. Registry round-trip in-process: `Setting.register_validator("ITEM023_TEST_KEY",
   func)` where func rejects anything but `{"ok": true}` → garbage save raises,
   valid save persists, and an UNREGISTERED key (`ITEM023_FREE_KEY`) still
   accepts arbitrary values; `finally` pops both test keys from
   `Setting.VALIDATORS` and removes rows.
6. Cache invalidation for a posture key (the bonus fix): in-process, write a
   dummy decision through `mojo.apps.account.services.geofence.cache` (builder:
   read that module's set/get API first), then `Setting.set(
   "GEOFENCE_ALLOW_PRIVATE_IPS", True)` → cached decision gone
   (`finally`: `Setting.remove` + `invalidate_all`).

**`tests/test_helpers/settings_coercion.py`** (in-process; unregistered
`ITEM023_COERCE_*` keys so validation never interferes; `finally`
`Setting.remove` each):
1. Bool: `Setting.set(key, "garbage")` → `settings.get(key, False, kind="bool")
   is False` AND `settings.get(key, True, kind="bool") is True` (declared
   default, both directions — the old behavior returned True regardless);
   valid strings still parse (`"yes"` → True, `"off"` → False); real booleans
   round-trip.
2. Dict: plant `"{not-json"` → `settings.get(key, {"d": 1}, kind="dict") ==
   {"d": 1}`; valid JSON dict round-trips.
3. List: plant `'["payments",]'` (bracket-wrapped, unparsable) →
   `settings.get(key, ["x"], kind="list") == ["x"]` — NOT
   `['["payments"', ']']`; plain `"a, b"` still comma-splits to `["a", "b"]`.
4. Int: plant `"12x"` → declared default returned.
5. Read-path pin for a geofence key with pre-existing garbage:
   `Setting.set("GEOFENCE_ALLOW_PRIVATE_IPS", True)` (valid write), then
   `Setting.objects.filter(key=..., group=None).update(value="garbage")`
   (bypasses `save()`), refresh the Redis settings cache (re-fetch row and
   `push_to_cache()` — builder verifies the method at setting.py:154-171),
   then `settings.get("GEOFENCE_ALLOW_PRIVATE_IPS", True, kind="bool") is
   True` (declared default; no crash). `finally` remove + `invalidate_all`.
   (Do NOT assert log output — logging is best-effort observability; the
   VALUE contract is what's pinned.)

### Docs

- `docs/django_developer/helpers/settings.md` — document the `kind=` coercion
  contract change (present-but-uncoercible → declared default + a
  `logit.warning("settings", …)`; unrecognized bool strings no longer truthy;
  bracket-wrapped unparsable lists no longer comma-split) and the new
  `Setting.register_validator(key, func, global_only=True)` API with the
  mverify `PAYMENTS_GEOFENCE_RULES` registration as the example.
- `docs/django_developer/account/geofence.md` — Settings Reference + Config
  Plane sections: all eight `GEOFENCE_*` keys are write-validated, global-only,
  and cache-invalidating (previously three).
- `docs/web_developer/account/admin_portal.md` — where it documents
  `/api/settings`: note that validated keys return readable 400s (builder
  checks fit; add only if it discusses setting writes).
- `CHANGELOG.md` — bug-fix entry (write validation for posture keys +
  invalidation fix + coercion logging + bool strictness).

### Open questions

None. (mverify-side registration of `PAYMENTS_GEOFENCE_RULES` and any
`get_strict` read API are follow-ups in that repo, unblocked by this item.)

## Notes

- **Build baseline (2026-07-09)**: GREEN — total 2361, passed 2305, failed 0,
  skipped 56 (default suite). No pre-existing failures: every post-change
  failure is attributable to this build.
- Origin: mverify_api `planning/done/MVERIFY-API-014-*.md` (security-review
  WARNING; app-side fix landed there same day, commit 7a825ee).
- Sequencing hint: the validator-registry shape would let downstream apps
  stop hand-rolling raw-read guards like geo_gate's `_payments_rules()`.
- **Post-build review results (2026-07-09)**: regression-first build — 9
  failing tests confirmed pre-fix (bonus finding: `ignore_errors=True` masked
  dict parse errors as an EMPTY objict, so garbage read as `{}` not even the
  declared default). test-runner GREEN (2372/2316/0 — baseline +11).
  security-review: no criticals; two WARNINGs FIXED in 57332b6 (`is_secret`
  bypassed validation on registered keys → now rejected outright + regression
  test; `_warn_coercion` logged raw values which can be decrypted secrets →
  now logs type only). INFO deferred: `json.loads` RecursionError on
  pathologically deep JSON escapes as 500 not 400 — pre-existing, generic,
  admin-perm-gated. docs-updater fixed one stale claim (strict-posture doc's
  "kind=bool would coerce garbage truthy" rationale). mverify follow-ups
  (register `PAYMENTS_GEOFENCE_RULES`, optional `get_strict`) live in that
  repo.

## Resolution
- closed: 2026-07-09
- branch: main
- files changed: CHANGELOG.md,docs/django_developer/account/README.md,docs/django_developer/account/geofence.md,docs/django_developer/account/geoip.md,docs/django_developer/core/permissions.md,docs/django_developer/helpers/settings.md,docs/django_developer/helpers/settings_reference.md,docs/django_developer/logging/incidents.md,docs/django_developer/security/README.md,docs/django_developer/testit/Overview.md,docs/web_developer/account/README.md,docs/web_developer/account/admin_portal.md,docs/web_developer/account/geofence.md,docs/web_developer/account/geoip.md,docs/web_developer/account/login_events.md,docs/web_developer/security/README.md,memory.md,mojo/__init__.py,mojo/apps/account/models/group.py,mojo/apps/account/models/setting.py,mojo/apps/account/rest/geofence.py,mojo/apps/account/services/geofence/engine.py,mojo/apps/account/services/geofence/evidence.py,mojo/apps/incident/asyncjobs.py,mojo/apps/incident/cronjobs.py,mojo/apps/incident/migrations/0031_alter_ipset_source.py,mojo/apps/incident/models/ipset.py,mojo/helpers/geoip/detection.py,mojo/helpers/geoip/threat_intel.py,mojo/helpers/settings/helper.py,mojo/rest/info.py,planning/.next_id,planning/done/ITEM-020-geofence-evidence-metrics-dual-write-group-scoped-.md,planning/done/ITEM-021-geofence-hardening-opt-in-strict-compliance-enforc.md,planning/done/ITEM-022-member-readable-geofence-policy-events-group-scope.md,planning/in_progress/ITEM-023-geofence-adjacent-settings-bypass-write-time-valid.md,planning/inbox/dispatcher-group-param-is-active-asymmetry.md,planning/inbox/geofence-hardening.md,planning/inbox/geofence-settings-write-validation-gap.md,pyproject.toml,tests/test_geofence/_helpers.py,tests/test_geofence/evidence_plane.py,tests/test_geofence/member_visibility.py,tests/test_geofence/settings_validation.py,tests/test_geofence/strict_posture.py,tests/test_geofence/threat_cache.py,tests/test_helpers/settings_coercion.py,uv.lock
- tests added: tests/test_geofence/settings_validation.py — test_backdoor_garbage_rejected (5 posture keys × garbage payloads → 400 + no row), test_backdoor_valid_accepted (default-equal values persist), test_backdoor_group_scoped_rejected (global-only), test_secret_flag_rejected_for_registered_keys (is_secret bypass closed, REST + Setting.set), test_shell_write_validated (save()-level hook), test_validator_registry (register/reject/accept + unregistered keys unaffected), test_posture_write_invalidates_decision_cache; tests/test_helpers/settings_coercion.py — test_bool_garbage_returns_declared_default (both default directions + valid strings), test_dict_garbage_returns_default, test_list_bracket_garbage_returns_default (no comma-split nonsense; CSV still works), test_int_garbage_returns_default, test_geofence_read_path_planted_garbage (pre-existing garbage degrades to declared default)
