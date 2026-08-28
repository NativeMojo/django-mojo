# Test isolation — one checkout, one database, one Redis index, one port

Every checkout gets its own test environment, derived from its path. Two
worktrees of this repo can run their suites at the same time and never see each
other.

## The rule

**Derive, never inherit.**

Isolation across the mojo repos used to be hand-set: someone picked
`mojo_maestro_test` / Redis index 15 / port 9109 once, and it worked because a
human made the values different.

A worktree defeats that completely. It is a **copy**, so it silently inherits
its parent's database name, Redis index and port. Nobody chose anything, so
nothing is distinct — and two suites then `TRUNCATE` every table and call
`flushdb()` on each other mid-run. The symptom is not an error; it is the other
run's data disappearing, which reads as flakiness.

So no isolation value may live in a file a worktree would clone. They are
derived from the checkout's absolute path and recorded **outside** any checkout,
in `~/.mojo/testenv.json`.

## What you get

`bin/create_testproject` prints it:

```
==> Creating testproject in /Users/you/Projects/django-mojo/testproject
    db=mojo_test_0f294679  redis=1  port=5600
```

| | Derived value | Written into |
|---|---|---|
| Postgres database | `mojo_test_<slug>` | `testproject/config/settings/local/db.py` |
| Redis DB index | `REDIS_DB_INDEX` | same file |
| Dev server port | `port=` | `testproject/config/dev_server.conf` |
| Django cache prefix | `mojot_<slug>` | same `db.py` |
| Pub/Sub channel prefix | `REDIS_PUBSUB_PREFIX = mojot_<slug>` | same `db.py` |

`<slug>` is `sha1(realpath(checkout))[:8]`. Stable across reboots and branch
switches; different for every worktree. `realpath` matters — `/var` and
`/private/var` are the same directory on macOS, and hashing both would hand one
tree two slots.

`bin/asgi_local` already reads `dev_server.conf`, and testit reads it through
`_read_dev_server_conf()`, so nothing else needs to know.

## Inspecting and reclaiming

```bash
uv run python testit/testenv.py list
uv run python testit/testenv.py prune            # drop slots for deleted checkouts
uv run python testit/testenv.py release <path>   # give one slot back
```

**Prune after deleting a worktree.** Redis indexes are the scarce resource and a
removed checkout keeps holding one.

`prune` reclaims the slot but deliberately does **not** drop the Postgres
database — removing one because a directory is currently missing would destroy
data whenever a volume is unmounted or a checkout is moved rather than deleted.
It prints the orphaned name instead:

```
pruned 1
  /Users/you/Projects/django-mojo-feature  (mojo_test)

These databases are now orphaned. Drop them when you are sure the checkout is
really gone:
  dropdb mojo_test_85e2ef50
```

`prune` is **machine-global** — the registry holds every mojo repo on the
machine, and a slot for any of them whose directory is gone gets reclaimed.
That is intended; it is the only thing that keeps the 15 Redis indexes from
leaking away. It names the project each reclaimed slot belonged to, and says so
explicitly when it took one from a repo other than this one:

```
Note: slots for other projects were reclaimed (mojo_maestro_test).
Their checkouts will get a different Redis index and port next run.
```

There is deliberately **no per-project filter**. Restricting `prune` to one repo
would leave every other repo's dead slots held forever, which is exactly the
leak above.

> Invoke it by **file path**, not `python -m testit.testenv`. Importing the
> package runs `testit/__init__.py` → `mojo.helpers.logit` → `paths`, which
> needs a configured project — and `create_testproject` runs before the project
> exists. Downstream repos that need the path without importing the package can
> use `importlib.util.find_spec("testit").origin`, which locates it without
> executing it.

## Messaging isolation — the Pub/Sub prefix

The database index isolates **stored keys** only. [Redis Pub/Sub ignores
logical database numbers](https://redis.io/docs/latest/develop/pubsub/): a
message published on `realtime:broadcast` from db 3 is delivered to a
subscriber of `realtime:broadcast` on db 7 of the same server. Before this
existed, two checkouts' suites heard each other's realtime messages and
job-control broadcasts even with fully separated data.

The fix is the file-static Django setting **`REDIS_PUBSUB_PREFIX`**
(default `""`). When nonempty, every framework Pub/Sub channel — jobs
runner ctl, runners broadcast, replies, ping; realtime broadcast, topics,
per-connection messages — becomes `{REDIS_PUBSUB_PREFIX}:{legacy_name}` on
publish, subscribe, and unsubscribe. Empty means the legacy names,
byte-identical. Storage keys never carry it, and clients see no change —
the prefix exists only on the Redis wire.

`bin/create_testproject` writes `REDIS_PUBSUB_PREFIX = "mojot_<slug>"`
into the generated `db.py` — the same checkout identity as the cache
prefix, via `testenv.expected_pubsub_prefix(root)`. One settings file
feeds every participating process (test runner, `bin/asgi_local` server,
in-test job engines), so they always agree.

**The guard fails closed.** `bin/testit.py` compares the live setting to
`expected_pubsub_prefix(checkout root)` before flushing anything; a
missing or mismatched value — a testproject generated before this feature
existed, or one copied from another tree — aborts the run with the fix
(`bin/create_testproject`) instead of silently talking on the legacy
shared channels.

Scope honestly stated: this is cooperative segregation between test
processes that all apply the prefix. Two *empty-prefix* environments on
one Redis server are still not isolated from each other, and none of this
is a substitute for Redis ACLs against a hostile client. Leave the setting
unset in production. Do not override `REDIS_PUBSUB_PREFIX` through
`th.server_settings()` — the runner and the reloaded server would then
disagree and every delivery test fails.

## Two deliberate offsets

- **Redis indexes start at 1.** Index 0 is what any project that has *not*
  adopted this gets from `REDIS_DB_INDEX`'s default, so leaving it unallocated
  means an adopted checkout can never collide with an unadopted one.
- **Ports start at 5600.** Clear of every hand-set port in the fleet today
  (5555, 7575, 9009, 9109, 9999), so adoption is not a flag day.

Both only matter during the migration, and both cost nothing to keep.

## The Redis ceiling

Redis ships with **16 databases**, and index 0 is reserved above — so 15
concurrent checkouts, across every mojo repo on the machine. The allocator
**assumes** 16; it does not ask the server. When it runs out it **fails with an
actionable error** rather than quietly reusing an index.

To raise it you must change two things, and changing only the first does
nothing:

```
# 1. your local redis.conf
databases 64
```

```bash
# 2. tell the allocator, e.g. in your shell profile
export MOJO_TESTENV_REDIS_LIMIT=64
```

The allocator does not probe because probing meant importing
`mojo.helpers.redis`, which resolves its URL from a Django setting and caches a
process-global client before it ever touches the network. Called from
`create_testproject` (no project yet) that import simply fails; called from a
settings module that is still executing, it silently pins the whole process to
Redis index 0 for its lifetime. A number you can be told is not worth that.

A junk or unset `MOJO_TESTENV_REDIS_LIMIT` means 16. A value below 1 floors at
1, which leaves no allocatable index and fails closed on the next allocation —
correct, if blunt. It is **clamped at 64** (`REDIS_MAX_LIMIT`) at the top end:
allocation opens a connection per candidate index, so a mistyped `99999999`
would not fail, it would sit there connecting for hours. Clamped rather than
rejected, so the typo still allocates. If you genuinely run more than 64
databases, raise the constant.

## Adopting testenv elsewhere

`testit/testenv.py` is deliberately standalone. **`allocate()` imports nothing
from `mojo.*` and reads no Django setting**, so calling it from a test settings
module — while that module is still executing, before Django is configured — is
supported and is the intended use.

```python
# in your project's test settings, before anything reads DATABASES
import importlib.util, sys

spec = importlib.util.spec_from_file_location(
    "mojo_testenv", importlib.util.find_spec("testit").origin.replace(
        "__init__.py", "testenv.py"))
testenv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(testenv)

env = testenv.allocate(REPO_ROOT, "myproj_test")
```

If your caller already knows the Redis ceiling, hand it over directly and skip
the environment variable entirely:

```python
env = testenv.allocate(REPO_ROOT, "myproj_test", limit=64)
```

Load it by file path (or `find_spec`), never `import testit.testenv` —
importing the package runs `testit/__init__.py`, which needs a configured
project.

### Adopting messaging isolation

The Pub/Sub prefix rides the same allocation. In the generated (or test)
settings, alongside `REDIS_DB_INDEX`:

```python
REDIS_PUBSUB_PREFIX = testenv.expected_pubsub_prefix(REPO_ROOT)
```

That is the whole consumer contract: set the setting, and every framework
Pub/Sub path — and anything you publish through `realtime.publish_topic`
etc. — is namespaced per checkout. If your own code publishes raw Redis
channels, build the names through
`mojo.helpers.redis.channels.channel_name(name)`. To fail closed the way
django-mojo's own runner does, compare the live setting to
`testenv.expected_pubsub_prefix(root)` in your runner preflight and abort
on mismatch (see `bin/testit.py`).

### Guarding your own flush

Allocation alone does not protect you. If your runner calls `FLUSHDB`, wrap it:

```python
index = client.connection_pool.connection_kwargs.get("db", 0)

testenv.claim_redis_index(index, REPO_ROOT, client=client)   # raises AllocationError -> exit 1
client.flushdb()
testenv.stamp_redis_owner(index, REPO_ROOT, client=client)   # the flush destroyed the stamp
```

Five things that matter:

- **Pass the client you are about to flush.** `claim_redis_index`,
  `stamp_redis_owner`, `redis_owner` and `redis_index_is_free` all take
  `client=`, and with one supplied they read and write the stamp through it —
  the exact server and database `flushdb()` is about to empty, no second
  connection, and closing it stays your job. Without one they fall back to
  building a probe against `MOJO_TESTENV_REDIS_HOST`/`_PORT` (localhost by
  default), which is the wrong server whenever your Redis is elsewhere. The
  index you pass must agree with the client's own `db`; a disagreement raises
  `AllocationError` rather than picking one.
- **Derive the index from the live client, not from settings.**
  `mojo.helpers.redis` gives `REDIS_URL` precedence over `REDIS_DB_INDEX`, so a
  guard reading settings can protect index N while `flushdb()` empties index M —
  worse than no guard at all. The same argument applies to the host, which is
  what `client=` above is for.
- **Claim before, stamp after.** The flush destroys the stamp along with
  everything else.
- **On a resume path (`--continue`), claim but do NOT stamp.** A `SET` with no
  preceding flush has no claim behind it and would quietly take over another
  checkout's index — making the resume path the one way to bypass the guard.
- **The key name is shared across repos on purpose.** Two repos with different
  key names are blind to each other's stamps, which is the collision the whole
  mechanism exists to prevent. Use `testenv.REDIS_OWNER_KEY`.

An adopter that allocates but never stamps gets today's behaviour: nothing owns
its index, so nothing is protected and nothing is broken. Stamping is what buys
the protection.

## The Redis ownership stamp

The registry is one JSON file in a home directory, and every way it can be lost
— deleted, truncated by a killed writer, rebuilt after a bad parse — reads as
"nothing is allocated". A port survives that, because `port_is_free` **binds**
before handing one out. A Redis index had no such check, so a lost registry
meant the next `flushdb()` silently emptied a live checkout's database mid-run.

So each allocated index carries a stamp:

| | |
|---|---|
| Key | `testenv:owner` (in that index) |
| Value | the owning checkout's absolute realpath |
| Written by | `bin/testit.py`, right after its `FLUSHDB` |

The stamp is read and written with a bare `redis-py` client — never
`mojo.helpers.redis`, for the same reason `redis_limit()` doesn't call it (see
above).

**Which server gets asked depends on the check**, and the difference is the
point:

- **The flush guard uses the runner's own live client**, handed to
  `claim_redis_index(..., client=...)` / `stamp_redis_owner(..., client=...)`.
  That client is the one about to be flushed, so the guard reads and re-stamps
  the exact server and database `flushdb()` empties. Nothing about
  `MOJO_TESTENV_REDIS_HOST`/`_PORT` enters into it.
- **Allocation builds a probe**, because there is no live client yet — it runs
  before the project exists, deriving the values the settings will go on to
  hold. The probe connects to `127.0.0.1:6379` by default; if your Redis isn't
  there, set `MOJO_TESTENV_REDIS_HOST` and/or `MOJO_TESTENV_REDIS_PORT` (each
  falls back to its own default independently). These are read at call time and
  are independent of any Django `REDIS_HOST`/`REDIS_PORT`/`REDIS_URL` setting.

**The honest limitation:** an adopter whose test Redis is remote and who does
not set those two variables gets a probe that asks the local machine, so
**allocation-time protection does not apply to them** — the probe reads the
wrong server, which surfaces as `RedisUnreachable` rather than as a config
mismatch. They still get the flush-time guard, and that is the check standing
between a wrong index and destroyed data.

It is checked in two places, and they fail in **opposite directions on purpose**:

- **Allocation fails closed.** `allocate()` skips any candidate index a
  *different, still-existing* checkout has stamped, and raises `AllocationError`
  if it cannot read the stamps at all. The record it writes is used for months,
  so one flaky probe must never latch a collision in permanently. A refused
  connection stops the search immediately; a timeout does not, because a timeout
  means Redis is up and busy — precisely when guessing "free" is destructive.
- **The flush fails open.** `bin/testit.py` calls `claim_redis_index()` before
  `FLUSHDB` — passing the live client, so the question is asked of the server
  being flushed — and aborts the run if the index belongs to someone else, but
  allows it when Redis cannot be reached: a flush that cannot connect cannot
  destroy anything either.

A stamp naming a directory that **no longer exists** is stale: the checkout is
gone, so its leftover data is nobody's and the index is takeable. Otherwise a
deleted worktree would poison a slot forever.

Index 0 is never allocated, never stamped, and `claim_redis_index()` refuses it
outright — it is the developer's own cache, and flushing it is never ours to do.

A stamp that will not resolve to a path at all — one containing a NUL byte, say
— is **unusable**, not "not mine": both checks refuse with `AllocationError`
rather than treating an unknown owner as no owner. Every other hostile value
(`../../..`, `~/`, empty, whitespace) resolves fine, reads as "not me", and
falls through to the stale-directory check, which is the right answer for all
of them.

**The escape hatch** is `MOJO_TESTENV_NO_REDIS=1`, for an image with no Redis at
all. It turns off the **allocation-time probe** — that is its entire scope, and
it is the only supported way to allocate without the ownership check.

It does **not** disable the pre-`FLUSHDB` guard, and cannot. That path runs only
when the runner is holding a live client it is about to flush, so there is no
probe there to switch off and nothing for "this image has no Redis" to excuse.
Before this was fixed, `export MOJO_TESTENV_NO_REDIS=1` silently turned the
flush guard into a no-op — and left the index unstamped afterwards, so the
weakening outlived the process and the next allocation handed the index to a
third tree.

> **Anything that calls `FLUSHDB` outside the runner drops the stamp** until the
> next run re-writes it. `MojoRedisCache.clear()` (`mojo/cache/redis.py`) is one
> such call. It is unreachable in the generated testproject today — the
> serializer cache backend defaults to `memory` — so it is documented rather
> than defended against. If you point a Django cache at Redis and call
> `cache.clear()`, expect the guard to be blind until the next `run_tests`.

## Fail-closed behaviour

Three places refuse rather than guess, all because the failure mode is
destroying another run:

- **`create_testproject` aborts if allocation fails.** Falling back to a shared
  default name is exactly how one checkout `DROP`s the database another
  checkout is mid-run against.
- **It refuses to drop a database with active connections**, checked against
  `pg_stat_activity`. The name is derived per checkout so this should never
  fire; it exists because if derivation ever breaks, the consequence is another
  session's data, and that deserves a guard rather than trust.
- **`run_tests` aborts rather than `FLUSHDB` an index another live checkout has
  stamped** — see above. The fix is the allocation, not the guard: `prune`, then
  `bin/create_testproject`.

## Running two worktrees

```bash
git worktree add ../django-mojo-feature -b feature
cd ../django-mojo-feature
uv sync
./bin/create_testproject      # allocates its own db / index / port
./bin/run_tests --agent -t test_edge
```

Each tree needs its own `.venv` and its own `testproject/` — both gitignored,
both regenerated. That is the cost: a `uv sync` and a `create_testproject` per
tree.

**What worktrees do not isolate:** migration numbering. django-mojo ships its
own migrations, so two trees adding a model to the same app both generate
`0002_*.py`. They do not clash on disk — they clash at merge, and Django needs
a manual merge migration. Worth knowing before doing model work in two trees at
once.
