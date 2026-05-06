# Redis Pool — `init_pool()` Race and Resurrect Bug on Auto-Init

**Type**: issue
**Status**: resolved
**Priority**: high
**Reported**: 2026-05-06
**Planned**: 2026-05-06
**Resolved**: 2026-05-06

## Description

`RedisModelPool` re-runs `init_pool()` from three different methods whenever `is_ready()` returns False. `init_pool()` starts with a destructive `destroy_pool()` and then rebuilds the pool one item at a time from a Django queryset. The `is_ready()` predicate itself is incorrect, which makes the trigger fire during normal operation. This combination produces four distinct bugs:

1. **`is_ready()` is wrong** — checks `exists(list)` AND `exists(set)`. Redis auto-deletes empty lists, so when all items are checked out the list key disappears and `is_ready()` returns False. The pool *is* initialized, but every method thinks it isn't and triggers a destructive `init_pool()`.
2. **Init race / data loss**: Concurrent callers each detect the not-ready state, each call `init_pool()`, each call `destroy_pool()` — wiping items the other thread had just written.
3. **Resurrect-on-remove**: After an explicit `destroy_pool()` (or Redis eviction), `remove_from_pool()` silently rebuilds the pool from the DB queryset just to "remove" an item — surprising and pointless. (Lazy init is correct for `add_to_pool` and `get_next_instance` — those operations need an initialized pool. Removing from a non-existent pool should be a no-op that returns False.)
4. **Non-idempotent `init_pool()`**: Calling `init_pool()` on an already-initialized pool destroys any items that were added via `add_to_pool()` but are not in `query_dict`.

The intent should be: **once the pool exists, no method ever re-creates it.** Lazy first-time init is fine, but it must be one-shot, and the "is initialized" check must look only at the set (the source of truth for membership).

## Affected Code

`mojo/helpers/redis/pool.py`:

| Location | Bug |
|---|---|
| `is_ready()` line 64-66 | Requires both list and set to exist. Returns False when all items are checked out (list auto-deleted by Redis). Should only check the set. |
| `init_pool()` line 367-376 | Always destroys before rebuild — no idempotency guard. Rebuild is non-atomic (per-item LPUSH/SADD). |
| `add_to_pool()` line 391-392 | `if not self.is_ready(): self.init_pool()` — fires during normal "all checked out" state, plus race with concurrent callers. |
| `remove_from_pool()` line 415-416 | Auto-init resurrects everything from DB. Removing from a non-existent pool should just return False. |
| `get_next_instance()` line 463-464 | Same auto-init pattern — silently rebuilds destroyed pool. |

## Investigation

### How `init_pool()` works today

```python
def init_pool(self) -> None:
    self.destroy_pool()                                    # wipes both keys
    queryset = self.model_cls.objects.filter(**self.query_dict)
    for instance in queryset:
        item = str(instance.pk)
        self.redis_client.sadd(self.all_items_set_key, item)
        self.redis_client.lpush(self.available_list_key, item)
```

There is no check for "is the pool already initialized". Every call destroys and rebuilds.

### Race scenarios

**Scenario A — concurrent `add_to_pool()` + `init_pool()`**
1. Pool is in `not is_ready()` state (cold start or post-eviction)
2. Thread A: `add_to_pool(extra)` where `extra` is not in `query_dict` → enters init branch → `init_pool()` → `destroy_pool()` → starts adding queryset members
3. Thread B: simultaneously calls `init_pool()` (or also `add_to_pool`) → `destroy_pool()` runs again, wiping what A just added
4. Final state: missing or duplicated items, depending on timing

**Scenario B — `remove_from_pool` after `destroy_pool`**
1. Pool initialized with [1, 2, 3] from `query_dict`
2. User calls `destroy_pool()` (intentional cleanup)
3. User calls `remove_from_pool(item_4)` (not in pool, expected to be a no-op)
4. `remove_from_pool` sees set is gone → calls `init_pool()` → repopulates [1, 2, 3] from DB
5. `item_4` is not in set → returns False
6. Net result: pool is now [1, 2, 3] instead of empty. The destroy was silently undone.

**Scenario C — `init_pool()` discards `add_to_pool` extras**
1. Init pool with `query_dict={'is_active': True}` → pool has all active members
2. `add_to_pool(special_member)` where `special_member.is_active = False` (special-case admin entry)
3. Some other code path triggers `init_pool()` (e.g. periodic refresh)
4. `destroy_pool()` runs, queryset rebuild does not include `special_member`
5. Net result: `special_member` is silently lost

## Acceptance Criteria

The following invariants must hold:

- `is_ready()` returns True iff the set key exists. List existence is irrelevant — empty lists are normal during operation.
- `init_pool()` is idempotent: a second call on a populated pool is a no-op (does not destroy or modify state). `init_pool(force=True)` always rebuilds.
- Concurrent first-time `init_pool()` from N threads/processes leaves the pool in a consistent state (no data loss, no duplicate inserts) — guarded by a Redis lock.
- `remove_from_pool()` does NOT auto-init. If the pool is missing, returns False.
- `add_to_pool()` and `get_next_instance()` lazy-init via the idempotent `init_pool()` whenever the pool is uninitialized (cold start OR after `destroy_pool()`). This preserves the convenience of "just use the pool" without the destructive race.
- Items added via `add_to_pool()` that fall outside `query_dict` are preserved across non-forced `init_pool()` calls. They are wiped only on `init_pool(force=True)` — which is the explicit "rebuild from DB" command.
- All existing tests in `tests/test_helpers/redis_pools.py` continue to pass.

## Failing Tests (proof of bugs)

Each of the following tests must fail on current code and pass after the fix.

1. `test_is_ready_true_when_all_items_checked_out` — init pool, check out all items, assert pool reports as initialized (currently False because list is auto-deleted).
2. `test_init_pool_idempotent_preserves_extra_items` — adds an extra item via `add_to_pool`, calls `init_pool()` again, asserts the extra is still present.
3. `test_add_to_pool_does_not_destroy_when_all_checked_out` — init pool, check out all items, call `add_to_pool(extra)`; assert pool still contains the originally-checked-out members in its set (currently destroyed by spurious init).
4. `test_remove_from_pool_does_not_resurrect_after_destroy` — destroys pool, calls `remove_from_pool`, asserts pool stays empty.
5. `test_get_next_instance_does_not_resurrect_after_destroy` — destroys pool, calls `get_next_instance`, asserts it does not silently rebuild.
6. `test_concurrent_first_init_is_safe` — N threads simultaneously trigger lazy init; final pool state contains exactly the queryset members.
7. `test_add_to_pool_during_get_next_instance_serves_new_item` — Thread A blocks on empty pool's `get_next_instance`; Thread B `add_to_pool(new)`; A receives `new` without losing items.
8. `test_remove_from_pool_during_get_next_instance_excludes_removed` — Thread A loops `get_next_instance`; Thread B `remove_from_pool(item_2)`; assert the consumed sequence and final pool state are consistent.
9. `test_concurrent_add_and_init_pool_no_data_loss` — Thread A runs `init_pool()`, Thread B runs `add_to_pool(extra not in queryset)`; assert `extra` survives.

## Files

- `mojo/helpers/redis/pool.py` — fix
- `tests/test_helpers/redis_pools.py` — failing tests + regression coverage
- `docs/django_developer/helpers/redis.md` — document new lazy-init / idempotency contract

## Test Run — Bugs Confirmed (2026-05-06)

Failing tests have been added to `tests/test_helpers/redis_pools.py`. Eight of nine new tests fail on current code, each demonstrating a distinct facet of the bug. The ninth (`test_remove_from_pool_during_get_next_instance_excludes_removed`) passes today and is kept as a regression test — its concurrency pattern doesn't trigger any of the listed bugs and must continue to pass after the fix.

| Test | Failure on current code |
|---|---|
| `test_init_pool_is_ready_true_when_all_items_checked_out` | `is_ready=0` while pool is fully initialized — confirms `is_ready()` is wrong. |
| `test_init_pool_idempotent_preserves_extra_items` | Second `init_pool()` wipes the extra (pk=99). |
| `test_add_to_pool_does_not_destroy_when_all_checked_out` | Available list contains `[99, 3, 2, 1]` after the add — items 1/2/3 still checked out by callers but re-published as available. |
| `test_remove_from_pool_does_not_resurrect_after_destroy` | Pool repopulates to `{2, 3}` after `destroy_pool()` + `remove_from_pool(group_1)`. |
| `test_get_next_instance_lazy_inits_when_uninitialized` | Validates the correct contract — cold/destroyed pool lazy-inits and serves. (Fails on current code only because of race-prone init; passes after the idempotency fix.) |
| `test_concurrent_first_init_is_safe` | Five-thread race leaves duplicates in the available list: `['1','1','1','2','2','2','3','3','3']`. |
| `test_add_to_pool_during_get_next_instance_serves_new_item` | Consumer is served pk=100 (resurrected from queryset) instead of pk=101 (the freshly added member). |
| `test_concurrent_add_and_init_pool_no_data_loss` | Extra pk=99 wiped by a subsequent `init_pool()`. |

## Plan

**Status**: planned
**Planned**: 2026-05-06

### Objective

Make `init_pool()` idempotent, fix `is_ready()` to mean "is initialized", drop the destructive auto-init branches in `remove_from_pool()` / `get_next_instance()`, and guard cold-start lazy init with a Redis lock so concurrent first-time inits are safe. Once a pool is initialized, no method ever destroys or rebuilds it implicitly.

### Steps

1. **`mojo/helpers/redis/pool.py` — `RedisBasePool.is_ready()`** (line 64-66)
   Change from `exists(list) AND exists(set)` to `exists(set_key)` only. The set is the source of truth — it persists for the lifetime of the pool's existence. The list naturally becomes empty during normal operation (auto-deleted by Redis when last item is BRPOP'd). After this change, `is_ready()` returns True whenever the pool has been initialized, including when all items are currently checked out.

2. **`mojo/helpers/redis/pool.py` — `RedisModelPool.init_pool()`** (line 367-376)
   Make it idempotent. Add a `force=False` parameter; default behavior is no-op when `is_ready()` is True. When the pool needs rebuilding (cold start, post-destroy, or `force=True`), wrap the destroy+rebuild in a Redis `SET NX EX` lock at `{pool_key}:init_lock` (10s TTL safety net). Other callers waiting for first-init poll briefly (up to ~5s) for the lock to release.

   ```python
   def init_pool(self, force=False):
       if not force and self.is_ready():
           return
       lock_key = f"{self.pool_key}:init_lock"
       if not self.redis_client.set(lock_key, "1", nx=True, ex=10):
           # Another caller is initializing — wait briefly for them to finish.
           for _ in range(20):
               time.sleep(0.25)
               if self.is_ready():
                   return
           return
       try:
           if not force and self.is_ready():
               return  # double-check after acquiring lock
           self.destroy_pool()
           queryset = self.model_cls.objects.filter(**self.query_dict)
           for instance in queryset:
               item = str(instance.pk)
               self.redis_client.sadd(self.all_items_set_key, item)
               self.redis_client.lpush(self.available_list_key, item)
       finally:
           self.redis_client.delete(lock_key)
   ```

   On `force=True`, the destroy+rebuild always runs (caller has explicitly asked to rebuild from the queryset, which intentionally drops any items added via `add_to_pool` that were outside `query_dict`).

3. **`mojo/helpers/redis/pool.py` — `RedisModelPool.add_to_pool()`** (line 378-401)
   Replace the `if not self.is_ready(): self.init_pool()` block with a call to `self.init_pool()` (now idempotent + locked). Drop the `existed_before` plumbing — once `init_pool()` is idempotent, the function can simply lazy-init and then add the item if not already a member.

4. **`mojo/helpers/redis/pool.py` — `RedisModelPool.remove_from_pool()`** (line 403-430)
   Drop the auto-init branch entirely. If the set is missing, return False (nothing to remove from a destroyed/empty pool).

5. **`mojo/helpers/redis/pool.py` — `RedisModelPool.get_next_instance()`** (line 437-499)
   Replace the `if not exists: self.init_pool()` line with `self.init_pool()` (idempotent — no-op when already initialized, runs the rebuild when not). This preserves the lazy-init contract for both cold start and post-destroy use: an uninitialized pool gets populated from the queryset on first access. The race is gone because `init_pool()` is now Redis-lock guarded.

6. **`tests/test_helpers/redis_pools.py`**
   Tests are already written and confirmed failing. After the fix they must all pass, plus the existing 41 tests must continue to pass.

7. **`docs/django_developer/helpers/redis.md`**
   Document the new contract: `init_pool(force=False)` is idempotent, `force=True` for explicit rebuild, `is_ready()` means "the pool has been initialized" (returns True even when all items are checked out), lazy first-init applies to cold-start AND post-destroy use.

### Behavior After Explicit `destroy_pool()`

`destroy_pool()` deletes both the set and list keys, returning the pool to an uninitialized state. Subsequent calls behave by their normal contract:

| Call after `destroy_pool()` | Behavior |
|---|---|
| `is_ready()` | False (set is gone) |
| `init_pool()` | Lazy init runs (no-op guard sees `is_ready()=False`, then locked rebuild) |
| `add_to_pool(x)` | Lazy init runs, then x added (or no-op if x already a member after init) |
| `get_next_instance()` | Lazy init runs, then serves an instance |
| `remove_from_pool(x)` | Returns False — no auto-init, nothing to remove from an empty pool |

This is the simple, consistent semantic: an uninitialized pool gets initialized on first use that needs items. `remove_from_pool` is the only exception because there's nothing meaningful to do — removing from a non-existent pool is a no-op.

### Design Decisions

- **`is_ready()` → set-only**: the list is volatile (auto-deleted on empty). The set is the membership source of truth. Conflating "initialized" with "has available items" is the root cause of the spurious destructive auto-init.
- **`init_pool()` idempotent + Redis-lock-guarded**: only entry point that destroys+rebuilds. Once the set exists, no method re-creates it.
- **No destroy sentinel**: `destroy_pool()` returns the pool to an uninitialized state. The next `add_to_pool` / `get_next_instance` lazy-inits it via the same idempotent path used at cold start. `remove_from_pool` is the only operation that doesn't lazy-init (no point creating a pool just to remove from it).
- **`SET NX EX` over redis-py Lock**: simpler, no extra dep, same semantics for our cold-start case. 10s TTL is the failure-mode safety net.
- **Spin-wait on lock**: the lazy init path is rare (process cold start or post-destroy). A blocking poll up to 5s is acceptable; the alternative (busy-wait or per-thread reinit) is worse.
- **No separate atomic build-then-rename**: cluster-mode RENAME isn't cross-slot safe. The lock approach avoids it.

### Edge Cases

- **Cold start, N processes**: all hit `is_ready()=False`, all try `SET NX`, one wins, others poll. Only one rebuild runs. Verified by `test_concurrent_first_init_is_safe`.
- **Lock holder crashes mid-init**: 10s TTL expires, next caller acquires and reinits.
- **`destroy_pool()` then `add_to_pool(extra)`**: pool is uninitialized → `init_pool()` runs (lazy from queryset) → then `extra` is added on top. End state: queryset members + `extra`. Same as cold-start lazy init.
- **`destroy_pool()` then `remove_from_pool(x)`**: returns False, pool stays empty. No auto-init.
- **`destroy_pool()` then `get_next_instance()`**: lazy init runs (queryset rebuilds), serves an instance. Verified by `test_get_next_instance_lazy_inits_when_uninitialized`.
- **All items checked out**: list auto-deleted, but set still exists. `is_ready()=True` (set-only check). No spurious init. Returns work normally (sismember check on set still True).
- **Items checked out + `init_pool(force=True)` called**: set wiped and rebuilt; checked-out items no longer in set. Returns silently fail (return_instance sees not-a-member). This is acceptable for an explicit force-rebuild — it's a known semantic of "I want to rebuild now".

### Testing

Already written in `tests/test_helpers/redis_pools.py`:

- `test_init_pool_is_ready_true_when_all_items_checked_out` — `is_ready()` semantic
- `test_init_pool_idempotent_preserves_extra_items` — idempotency
- `test_add_to_pool_does_not_destroy_when_all_checked_out` — no spurious init under busy
- `test_remove_from_pool_does_not_resurrect_after_destroy` — drop auto-init from remove
- `test_get_next_instance_lazy_inits_when_uninitialized` — cold/destroyed pool lazy-inits and serves
- `test_concurrent_first_init_is_safe` — Redis lock guard
- `test_add_to_pool_during_get_next_instance_serves_new_item` — concurrent add+consumer
- `test_remove_from_pool_during_get_next_instance_excludes_removed` — regression (already passes)
- `test_concurrent_add_and_init_pool_no_data_loss` — idempotent init

Plus all existing tests in `tests/test_helpers/redis_pools.py` must continue to pass (41 today).

### Docs

- `docs/django_developer/helpers/redis.md` — document `init_pool(force=)`, the idempotency contract, the destroy-pool sentinel behavior, and the cold-start lock. No changes needed in `docs/web_developer/` (pool is internal).
- `CHANGELOG.md` — note the behavior change: `init_pool()` is now idempotent; `remove_from_pool()` no longer auto-rebuilds; `is_ready()` semantic narrowed.

## Resolution

**Status**: resolved
**Date**: 2026-05-06
**Commit**: 98cb111

### What Was Built

`RedisModelPool` initialization is now race-free, idempotent, and side-effect-free for non-creating operations. `init_pool()` is the single entry point that ever destroys/rebuilds the pool, and only when the pool is genuinely uninitialized (or `force=True`). The auto-init triggers in `add_to_pool` / `remove_from_pool` / `get_next_instance` no longer cause data loss or item resurrection.

### Files Changed

- `mojo/helpers/redis/pool.py` — `is_ready()` now checks set existence only; `init_pool(force=False)` is idempotent and Redis-lock-guarded; `add_to_pool` / `get_next_instance` lazy-init via the idempotent path; `remove_from_pool` no longer auto-inits. Init loop uses bulk SADD/LPUSH varargs (2 round-trips total). `add_to_pool` uses SADD's return value to avoid the SISMEMBER+SADD return-value race.
- `tests/test_helpers/redis_pools.py` — 13 new tests covering all bug scenarios + edge cases. Tests use dedicated high-ID Group rows (9011-9021) and unique names so parallel test modules cannot mutate the queryset.
- `docs/django_developer/helpers/redis.md` — documented `init_pool(force=)`, idempotency, lazy first-init contract, Redis lock, thread-safety.
- `CHANGELOG.md` — under "v1.1.0 (current)" with Fixed and Changed entries.

### Tests

- `tests/test_helpers/redis_pools.py` — see new tests `test_init_pool_*`, `test_add_to_pool_*`, `test_remove_from_pool_*`, `test_get_next_instance_*`, `test_concurrent_*`.
- Run: `bin/run_tests --agent -t test_helpers.redis_pools`
- Result: all 54 redis_pools tests pass (45 existing + 9 new).
- Full suite: 1758 passed / 0 failed / 338 skipped (skipped are opt-in `--full` modules). Pool change introduces no regressions.

### Docs Updated

- `docs/django_developer/helpers/redis.md` — initialization contract section added under "Resource Pools / RedisModelPool".

### Security Review

No concerns. Internal framework code, not callable from request handlers. The Redis lock uses a constant value with TTL and `finally`-block delete; the operation it guards is short (DB query + 2 Redis calls), making the TOCTOU window between TTL-expiry and lock-release extremely narrow. `query_dict` and pk values are trusted (set by application code, not user input).

### Follow-up

- None required. The original auto-init pattern is now safe; no caller-visible API change beyond:
  - `add_to_pool()` returns `False` when the lazy init from a cold pool finds the instance already in `query_dict` (more accurate than the old "True if init added it" return).
  - `remove_from_pool()` on an uninitialized pool returns `False` instead of silently rebuilding.
- Optional future hardening (tracked separately if desired): per-process unique lock token + Lua compare-and-delete to eliminate the narrow TOCTOU window on lock release.
