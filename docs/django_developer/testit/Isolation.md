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

> Invoke it by **file path**, not `python -m testit.testenv`. Importing the
> package runs `testit/__init__.py` → `mojo.helpers.logit` → `paths`, which
> needs a configured project — and `create_testproject` runs before the project
> exists. Downstream repos that need the path without importing the package can
> use `importlib.util.find_spec("testit").origin`, which locates it without
> executing it.

## Two deliberate offsets

- **Redis indexes start at 1.** Index 0 is what any project that has *not*
  adopted this gets from `REDIS_DB_INDEX`'s default, so leaving it unallocated
  means an adopted checkout can never collide with an unadopted one.
- **Ports start at 5600.** Clear of every hand-set port in the fleet today
  (5555, 7575, 9009, 9109, 9999), so adoption is not a flag day.

Both only matter during the migration, and both cost nothing to keep.

## The Redis ceiling

Redis ships with **16 databases**, and index 0 is reserved above — so 15
concurrent checkouts, across every mojo repo on the machine. The allocator asks
the server (`CONFIG GET databases`) rather than assuming, and **fails with an
actionable error** when it runs out rather than quietly reusing an index.

To raise it, in your local `redis.conf`:

```
databases 64
```

## Fail-closed behaviour

Two places refuse rather than guess, both because the failure mode is
destroying another run:

- **`create_testproject` aborts if allocation fails.** Falling back to a shared
  default name is exactly how one checkout `DROP`s the database another
  checkout is mid-run against.
- **It refuses to drop a database with active connections**, checked against
  `pg_stat_activity`. The name is derived per checkout so this should never
  fire; it exists because if derivation ever breaks, the consequence is another
  session's data, and that deserves a guard rather than trust.

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
