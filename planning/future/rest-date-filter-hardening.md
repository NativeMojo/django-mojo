# REST Date-Filter Hardening (security review follow-ups)

**Type**: request
**Status**: open
**Date**: 2026-05-04
**Priority**: low

## Description

Three follow-up items from the security review of commit `6d0b058` ("rest: add date-component lookups + partial-date shorthand for list filters"). None are actively exploitable, but the medium one yields a 500 where it should yield a 400.

### 1. [Medium] `pytz.UnknownTimeZoneError` not caught → HTTP 500

`partial_date_to_range` in [mojo/helpers/dates.py](mojo/helpers/dates.py) calls `pytz.timezone(timezone)` with a raw user-supplied string from `request.DATA.get("timezone")`. `pytz.timezone()` raises `pytz.exceptions.UnknownTimeZoneError` (a subclass of `KeyError`, not `ValueError`) on an unrecognized zone name.

The three call sites in [mojo/models/rest.py](mojo/models/rest.py) all wrap the call in `except ValueError:` only:

- `on_rest_list_filter` partial-date shorthand branch
- `on_rest_list_date_range_filter` `dr_start` branch
- `on_rest_list_date_range_filter` `dr_end` branch

Result: `?timezone=Bogus/Zone&created=2026-04` produces a 500. Should be a 400.

**Fix**: catch `pytz.exceptions.UnknownTimeZoneError` inside `partial_date_to_range` and re-raise as `ValueError(...)`, so callers' existing handlers cover it. One-line change.

### 2. [Low] Unbounded `__in` value-list split

`value.split(",")` in `on_rest_list_filter` (relation branch and non-relation branch, both `__in` and `__not_in`) has no length cap. A request like `?field__year__in=1,2,3,...` with thousands of entries builds a large Python list and emits a large `IN (...)` SQL clause.

This is a pre-existing pattern that the new date-component path replicates; it's not introduced by 6d0b058. Worth fixing once across all four split sites for consistency, ideally as a separate hardening pass.

**Fix**: introduce a module-level constant (e.g. `_MAX_IN_LIST = 500`) and reject lists longer than that with `me.ValueException(code=400)`.

### 3. [Info] Unicode digits in `_PARTIAL_DATE_RE`

`re.compile(r"^(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?$")` in [mojo/helpers/dates.py](mojo/helpers/dates.py) — Python's `\d` matches Unicode digit characters (e.g. `٢٠٢٦` Eastern Arabic numerals), which `int()` then accepts. Not exploitable; just unexpected.

**Fix**: add `re.ASCII` flag to the compile. One-character change.

## Acceptance Criteria

- `?timezone=Bogus/Zone` on any list endpoint returns HTTP 400, not 500.
- A test exercises the bad-timezone path and asserts the 400.
- `__in` lists exceeding the configured cap return 400 with a clear message.
- `_PARTIAL_DATE_RE` rejects non-ASCII digits.
- All three changes covered by tests in `tests/test_models/date_filtering.py` (extend the existing module).

## Out of Scope

- Reworking the `__in` split into a true streaming filter (premature; the cap is enough).
- Adding a TZ-aware variant of `__year` / `__month` lookups — separate concern.
