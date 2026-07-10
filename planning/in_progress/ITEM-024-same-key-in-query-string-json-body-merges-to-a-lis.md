---
# id is assigned by /scope on pickup — leave it blank
id: ITEM-024
type: bug
title: Same key in query string + JSON body merges to a list — dispatcher int() TypeError → bare Django 500
priority: P2
effort: S
owner: backend
opened: 2026-07-09
depends_on: []
related: [planning/inbox/dispatcher-group-param-is-active-asymmetry.md]
links: []
---

# Same key in query string + JSON body merges to a list — dispatcher int() TypeError → bare Django 500

## What & Why
Any REST request that sends the same key in BOTH the query string and the JSON
body gets a bare Django 500 (HTML error page — not mojo's JSON error envelope),
with no traceback reaching mojo's app logs. Reproduced on django-mojo 1.1.20
from the maestro project (`/Users/ians/Projects/mojo/maestro/api`) against
multiple RestMeta endpoints:

- `POST /api/maestro/thread/<pk>?group=518` with body `{"priority": "high", "group": 518}` → 500
- `POST /api/maestro/agent/task?group=518` with body `{..., "group": 518}` → 500

Sending the key in only one place (query OR body) returns 200. This is a
real-traffic pattern: web-mojo clients commonly send an active-group query
param while posting JSON forms that also carry `group`. Beyond the user-facing
failure, the 500 is invisible to operators — Django logs it only to the
`django.request` logger, which is not wired into logit's file handlers.

## Acceptance Criteria
- [ ] Duplicate keys across query string and JSON body resolve deterministically: JSON body wins over query params (last-writer-wins given merge order query→JSON). Documented in both doc tracks.
- [ ] The maestro repro pattern (`?group=518` + body `{"group": 518}`) returns a normal success response, not a 500.
- [ ] If a `group` value is genuinely unusable (e.g. still a list, or non-numeric garbage), the dispatcher returns mojo's JSON 400 (`{"error": "Invalid group ID", "code": 400}`) — never a bare 500 (catch `TypeError` alongside `ValueError`).
- [ ] Intra-source multi-value semantics are preserved: `?tag=a&tag=b` and `tags[]=a&tags[]=b` still produce lists; a JSON list value still arrives as a list.
- [ ] Regression test sends the same key through both channels (query string + JSON body) end-to-end and asserts a clean JSON response; a parser-level unit test pins the cross-source precedence.

## Repro — bugs only
1. Start any django-mojo server with a RestMeta model that has a `group` FK (e.g. testproject).
2. `POST /api/<app>/<resource>/<pk>?group=<id>` with header `Content-Type: application/json` and JSON body that also contains `"group": <id>` (testit: `opts.client.post(url + "?group=518", json={"group": 518, ...})`).
- Expected: 200 (duplicate key resolved deterministically), or at worst a JSON 400.
- Actual: bare Django 500 HTML page; no JSON envelope; nothing in mojo's error.log.

## Investigation
**Root cause — confidence: confirmed** (deterministic Python/Django semantics, traced by code reading).

The chain:
1. `request.DATA` is built by `mojo/middleware/mojo.py:56` → `parse_request_data` → `RequestDataParser` (`mojo/helpers/request_parser.py`). Merge order: query params first (`parse`, request_parser.py:32-40), JSON body second.
2. Query pass (`_process_query_params`, request_parser.py:47-50) sets `DATA['group'] = '518'` (str, via `getlist()[0]` for single values).
3. JSON pass (`_merge_dict_data`, request_parser.py:129-132) hits the collision branch in `_set_nested_value` (request_parser.py:174-188): existing scalar + new scalar → `current[final_key] = [existing, final_value]` → **`DATA.group = ['518', 518]`** (mixed list).
4. The dispatcher's group resolution (`mojo/decorators/http.py:74-91`) runs `int(request.DATA.group)` (http.py:76) → `int()` on a list raises **`TypeError`**.
5. The surrounding `except ValueError` (http.py:82) does not catch `TypeError`, and this block runs BEFORE `dispatch_error_handler` wraps the view (http.py:112-116) — so mojo's JSON error envelope and its `logger.exception` (http.py:196-211) never run.
6. Django's `convert_exception_to_response` converts it to the stock 500 HTML page and logs only to the `django.request` logger, which isn't in logit's handlers — hence no traceback in app logs. `LoggerMiddleware`'s try/except (`mojo/middleware/logging.py:87-95`) never fires because Django already converted the exception to a response.

**Latent siblings found (same defect class, decide in scope whether in-scope):**
- `mojo/decorators/auth.py:42-43` and `auth.py:84-85` — same `int(request.DATA.group)` pattern; runs inside the wrapped view so it yields a JSON 500 (not bare), but same list fragility.
- `mojo/models/rest.py:1370-1434` — `on_rest_save_related_field` handles dict/int/str/None; a list FK value silently no-ops (field never set, no error).

**Do not break (intentional feature):** multi-value query params (`?tag=a&tag=b`) and array notation (`tags[]=…`) intentionally produce lists (request_parser.py:47-50, `_normalize_key` 109-127; documented at docs/django_developer/core/middleware.md:152-179). Fix must target cross-source scalar collisions only.

**Related planning item:** `planning/inbox/dispatcher-group-param-is-active-asymmetry.md` (unscoped, P3) — the numeric `group=` branch of the same dispatcher block (http.py:74-91) skips the `is_active=True` filter that the `group_uuid` branch applies. Not the 500 cause, but it edits the exact same lines; reconcile when scoping.

**Regression-test feasibility:** high. testit's `RestClient` forwards kwargs to `requests` (`testit/client.py:93-115, 167-193`), so `opts.client.post(path + "?group=518", json={"group": 518})` (or `params=` + `json=`) sends both channels in one call. No existing test covers same-key-both-sources; no unit tests exist for `RequestDataParser`. Docs currently claim the three sources are "merged and treated identically" with no collision caveat (docs/web_developer/core/request_response.md:5-11).

## Plan

### Goal
A key sent in both the query string and the JSON body resolves deterministically (later source wins — JSON body over query params) instead of merging into a list that crashes the dispatcher into a bare Django 500; a genuinely unusable `group` value yields mojo's JSON 400, never a bare 500.

### Context — what exists
- **`request.DATA` construction:** `mojo/middleware/mojo.py:56` → `request.DATA = rhelper.parse_request_data(request)` → `RequestDataParser.parse()` (`mojo/helpers/request_parser.py:22-40`). Processing order (the comment at line 34 already SAYS "later sources can override earlier ones" — the collision branch contradicts it): `_process_query_params` (GET), `_process_form_data` (skipped for JSON content-type), `_process_json_data`, `_process_files`.
- **Value shapes entering `_set_nested_value(target, key, values)`:** query/form pass `values = QueryDict.getlist(key)` (so `?group=518` → `['518']` → scalar `'518'`; `?tag=a&tag=b` → `['a','b']` → stays a list — single call, list built by `len(values)` logic at request_parser.py:166-172, NOT by the collision branch). JSON passes `[value]` per top-level key (`_merge_dict_data`, request_parser.py:129-132), so a JSON list value arrives intact as `values[0]`.
- **The bug — the collision branch** (`request_parser.py:174-188`):
  ```python
  if final_key in current:
      existing = current[final_key]
      if isinstance(existing, list):
          if isinstance(final_value, list):
              existing.extend(final_value)
          else:
              existing.append(final_value)
      else:
          if isinstance(final_value, list):
              current[final_key] = [existing] + final_value
          else:
              current[final_key] = [existing, final_value]   # '518' + 518 → ['518', 518]
  else:
      current[final_key] = final_value
  ```
- **The crash site — dispatcher group resolution** (`mojo/decorators/http.py:74-91`), which runs BEFORE `dispatch_error_handler` wraps the view (http.py:112-116), so nothing converts the exception to JSON or logs it via logit:
  ```python
  if "group" in request.DATA and request.DATA.group:
      try:
          request.group = modules.get_model_instance("account", "Group", int(request.DATA.group))
          ...
      except ValueError:   # int(list) raises TypeError — NOT caught → bare Django 500
          ...
          return JsonResponse({"error": "Invalid group ID", "code": 400}, status=400)
  ```
  Django's `convert_exception_to_response` turns the escaped `TypeError` into the stock 500 HTML page and logs only to the `django.request` logger (not wired into logit) — hence "no traceback in app logs". `LoggerMiddleware`'s except (`mojo/middleware/logging.py:87-95`) never fires because Django already converted the exception to a response.
- **Same coercion pattern in auth decorators** (inside the error wrapper, so JSON 500 not bare): `mojo/decorators/auth.py:42-43` (`requires_perms`) and `auth.py:84-85` (`requires_group_perms`): `int(request.DATA.group)` unguarded. Post-parser-fix these are reachable when the dispatcher skipped resolution (falsy `group`: `''`, `0`, `[]`, `null`).
- **No consumer relies on cross-source list-merge:** grep found no `isinstance(request.DATA.x, list)` handling or `.getlist` on DATA anywhere in `mojo/`.
- **Test patterns to copy:** `tests/test_middleware/auth_malformed_header.py` (ITEM-012) — in-process fake requests (plain object/objict with `method`/`content_type`/`GET`/`POST`/`FILES`/`body`) + one HTTP test via `opts.client`. `request_parser.py:229-265` has a `MockRequest` showing the exact attrs the parser touches. Authenticated POST pattern: `tests/test_account/test_user_actions.py:27-66` — `@th.django_unit_setup()` creates user (delete-before-create), `opts.client.login(USERNAME, PASSWORD)`, `opts.client.post(f"/api/user/{opts.user_id}", {...})` (positional dict = JSON body); owner-saving-self returns 200. testit `RestClient` forwards kwargs to `requests`, so query string can ride in the path: `opts.client.post(f"/api/user/{pk}?group={gpk}", {...})`.
- **Docs today:** `docs/web_developer/core/request_response.md:5-11` claims the three sources are "merged and treated identically" (no collision rule); `docs/django_developer/core/middleware.md:152-180` documents the merge table + array notation, silent on duplicates.

### Changes — what to do
1. `mojo/helpers/request_parser.py` — in `_set_nested_value`, delete the collision merge branch (lines 174-188) and assign unconditionally: `current[final_key] = final_value` (keep a short comment: later sources override earlier — query < form < JSON; a key in two sources must not become a list). Update the class/`parse` docstring to state the precedence. This preserves intra-source multi-values (`?tag=a&tag=b`, `tags[]=…`) because those are built from `len(values) > 1` in a single call, and preserves JSON lists (arrive as `values[0]`).
2. `mojo/decorators/http.py:82` — broaden `except ValueError:` → `except (TypeError, ValueError):` so a still-unusable `group` (client deliberately sends a JSON list/dict) returns the existing JSON 400 `{"error": "Invalid group ID"}` instead of escaping as a bare 500. No other dispatcher changes (do NOT touch the `is_active`/`touch()` semantics — that's the separate inbox item).
3. `mojo/decorators/auth.py:42-43` and `auth.py:84-85` — guard the coercion fail-closed: wrap `request.group = modules.get_model_instance("account", "Group", int(request.DATA.group))` in `try/except (TypeError, ValueError): request.group = None`, so an unusable group param falls through to the existing `if not request.group … raise PermissionDeniedException()` (clean 403) instead of a JSON 500.
4. `tests/test_middleware/request_data_merge.py` — new test file (unit + e2e; see Tests).
5. Docs + `CHANGELOG.md` — see Docs.

### Design decisions
- **Last-writer-wins for ALL repeat writes to the same key in `_set_nested_value`** (not a per-source merge flag) — `parse()`'s comment already declares later-source override as the intent; the only behavior sacrificed is undocumented mixed-notation merging within one source (`?tags[]=a&tags=b` previously `['a','b']`, now `'b'` by key order) — pathological, undocumented, and now deterministic. Rejected: source-aware merge flag (more machinery, no consumer needs it); keeping list-merge and hardening every consumer (leaves every `request.DATA` reader exposed to surprise lists forever).
- **JSON body wins over query string** — falls out of the existing processing order (query → form → JSON); the JSON body is the deliberate payload while query params are ambient context (web-mojo appends the active-group param to everything). Confirmed by the requester.
- **Dispatcher catches `TypeError` too rather than pre-validating shape** — smallest change; the 400 path (incident event + JSON error) already exists and is correct for "unusable group value".
- **auth.py fails closed to 403 (not 400)** — in the permission-fallback context an unusable group simply means "no group context" → permission denied; consistent with fail-closed security rule. The f-string incident detail in http.py already stringifies non-scalars safely.
- **Out of scope:** `on_rest_save_related_field` silently ignoring a list FK value (`mojo/models/rest.py:1370-1434`) — post-fix only reachable when a client deliberately sends a JSON list for an FK; separate behavior question (noted in Notes). The `dispatcher-group-param-is-active-asymmetry` inbox item — security semantics change, needs its own ruling; this fix only widens the `except` and doesn't touch that branch logic.

### Edge cases & risks
- `?group=518` alone → `'518'` (str), `int()` fine — unchanged. JSON-only `"group": 518` → int — unchanged.
- `?tag=a&tag=b` / `tags[]=a&tags[]=b` → still lists (single `_set_nested_value` call). JSON `{"ids": [1,2]}` → still a list. `?ids=5` + JSON `{"ids": [1,2]}` → `[1,2]` (replace).
- Nested collision: `?user.name=John` + JSON `{"user": {"name": "Jane"}}` → JSON's whole dict replaces at key `user` → `user.name == "Jane"` (deterministic whole-value replace, not deep-merge — document it).
- `?group=1&group=2` (repeated in query only) → list → dispatcher `int(list)` → TypeError → now JSON 400 (was bare 500). Correctly rejects ambiguity.
- JSON `"group": null` / `""` / `[]` → falsy → dispatcher skips; auth.py fallback hits `int(None/''/[])` → guarded → fail-closed 403 (was JSON 500).
- `_json_parse_error`, `files` handling, dot/bracket normalization — untouched.
- Risk — something depended on cross-source list-merge: grep says nothing in `mojo/` does; full default suite (baseline-compared per build rule) is the backstop.

### Tests
All in new `tests/test_middleware/request_data_merge.py` (testit; module runs with `bin/run_tests --agent -t test_middleware.request_data_merge`). In-process parser tests build fake requests (objict/MockRequest pattern with `method`, `content_type`, `GET`/`POST` as `QueryDict`, `FILES`, `body`) and call `mojo.helpers.request_parser.parse_request_data` directly (import inside the test function per testit convention):
1. Cross-source scalar precedence (THE regression): GET `QueryDict('group=518')` + JSON body `b'{"group": 518}'` → `data.group == 518` (int — JSON won, NOT a list).
2. Form over query: content-type urlencoded, `?status=a` + POST `status=b` → `'b'`.
3. Intra-source multi-value preserved: `?tag=a&tag=b` → `['a','b']`; `tags[]=x&tags[]=y` → `['x','y']`.
4. JSON list preserved / replaces query scalar: `?ids=5` + `{"ids": [1, 2]}` → `[1, 2]`.
5. Nested whole-value replace: `?user.name=John` + `{"user": {"name": "Jane"}}` → `data.user.name == "Jane"`.
6. Single-source unchanged: query-only `?group=518` → `'518'`; JSON-only → `518`.

E2e over HTTP (setup: delete-before-create a user + a `Group` per `test_user_actions.py` pattern; login):
7. **Repro e2e:** `opts.client.post(f"/api/user/{opts.user_id}?group={opts.group_id}", {"group": opts.group_id, "display_name": ...})` → `status_code == 200` with JSON envelope (was bare 500). Sanity control in same test: same POST without the query param → 200.
8. **Unusable group → clean 400:** `opts.client.post(f"/api/user/{opts.user_id}?group={opts.group_id}", {"group": {"bad": true}})` → 400, JSON body contains `"Invalid group ID"` (was bare 500; dict replaces str, `int(dict)` → TypeError → new except arm).
9. **auth.py fail-closed (in-process):** call a `@requires_perms("some_perm")`-decorated dummy through its wrapper with a fake request (`user`: objict `is_authenticated=True`, `has_permission=lambda p: False`, `username`; `DATA=objict(group='')`, `group=None`) → expect `PermissionDeniedException` raised (not `ValueError`/`TypeError`).

Bug regression discipline: written first, MUST fail on unfixed code (tests 1, 7, 8 fail pre-fix), pass post-fix.

### Docs
- `docs/web_developer/core/request_response.md` (Sending Data, lines 5-11) — replace "merged and treated identically" with the precedence rule: sources merge query string → form body → JSON body; **the same key in more than one source takes the later value (JSON body wins over query string)**; repeated query keys (`?tag=a&tag=b`) still produce arrays.
- `docs/django_developer/core/middleware.md` (request.DATA Reference, lines 152-180) — add a **Duplicate keys / precedence** note under the merge table: later source wins, whole-value replace (no deep-merge of nested dicts), intra-source multi-value lists preserved.
- `CHANGELOG.md` (Unreleased) — **api/bugfix** entry: duplicate key in query + JSON body no longer 500s; documented precedence; unusable `group` now JSON 400 (dispatcher) / fail-closed 403 (perm fallback).

### Open questions
- none

## Notes
- Baseline (2026-07-09, before first edit): `bin/run_tests --agent` → **GREEN** — total 2373, passed 2317, failed 0, skipped 56 (`var/test_failures.json` status "passed", failures []). No pre-existing failures; anything red after the change is this item's.
- Follow-up candidates (NOT this item): `on_rest_save_related_field` list-FK silent no-op (`mojo/models/rest.py:1370-1434`); `dispatcher-group-param-is-active-asymmetry` inbox item (same dispatcher block, `is_active` filter asymmetry).
- Maestro repro context: django-mojo 1.1.20, `POST /api/maestro/thread/<pk>?group=518` + body `{"priority":"high","group":518}` → 500; single-source → 200.

## Resolution
- closed: YYYY-MM-DD
- branch:
- files changed:
- tests added:
