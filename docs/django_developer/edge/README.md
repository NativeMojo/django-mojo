# edge — nginx vhosts, and installing them to the fleet

`mojo.apps.edge` owns **how a domain is served**: the nginx `server` block, the
certificate material on disk, and the node-side lifecycle that installs both as
one validated, revertible generation.

It sits on top of `dnsman`, which owns the domain and issues the certificate.
The dependency is one-way — `edge` imports `dnsman`, never the reverse — which
is why this is a separate app rather than more of dnsman: dnsman is built on
"this system never touches a node", and everything below `services/installer.py`
does exactly that.

REST reference for API consumers:
[web_developer/edge](../../web_developer/edge/README.md).

Site releases (what is being *served*, as opposed to how):
[webapps.md](webapps.md).

Fleet code deploys (webhook trigger, canary node, locked migrations):
[deploy.md](deploy.md).

## The constraint everything else follows from

**A vhost is never a text field an admin types nginx directives into.**

An nginx `server` block is a program, not a setting. Anyone who can write one
can `proxy_pass` to the instance metadata service, `root /opt/api/var;` and
serve `django.conf` over HTTP, claim a `server_name` that shadows the API, or
weaken TLS. A free-text field moves privilege escalation from "who can write
the filesystem" to "who holds a portal permission" — and portal permissions are
usually *broader* than box access, not narrower.

So every value that reaches a config file is one of exactly two things:

| | Example | Why it is safe |
|---|---|---|
| **Derived from a foreign key** | `server_name` from `Vhost.domain`, the web root from `Vhost.pk`, the proxy target from `Vhost.upstream` | Ownership decides it. You cannot name a zone you do not hold, or a directory that is not yours. |
| **Matched against a whitelist regex** | `Vhost.label`, `Upstream.host`, `Vhost.pool` | It cannot carry a metacharacter at all. |

There is deliberately **no escaping helper**. An escape function is how a new
field eventually routes around validation.

## Layout

```
mojo/apps/edge/
  models/
    upstream.py     Upstream — the allowlist of places traffic may go
    vhost.py        Vhost — one server block, as structured data
  validators.py     Every value that can reach a file passes through here
  services/
    render.py       Vhost rows -> nginx text; the generation id
    installer.py    Node-side: stage, validate, swap, reload, revert
  rest/
    upstream.py     Read + the platform-admin `declare` action
    vhost.py        CRUD, with the house-vhost guard
    node.py         desired_state + material — machine endpoints
  asyncjobs.py      install_generation (broadcast), converge (cron)
  cronjobs.py       The 10-minute convergence sweep
```

## Models

### `Upstream` — the reason the foreign key is a constraint

`Vhost.upstream` being an FK only means something if a tenant cannot create
rows on the other end of it. So **declaring an upstream is a platform-admin
act** (`POST edge/upstream/declare` → `require_platform_admin`), and every
destination field is in `NO_SAVE_FIELDS` so an existing row cannot be
repointed either.

Loopback and RFC1918 addresses **are** valid upstreams: the real upstream on
every node is `127.0.0.1:<asgi port>`, and internal services legitimately live
on private addresses. What keeps a tenant away from them is that they cannot
declare one, not an address filter. Link-local (`169.254.0.0/16` — every
cloud's metadata service) is refused for everybody, platform admins included.

A group-less `Upstream` is a house row offered to every tenant.
An active-group REST list therefore returns that group's upstreams plus these
shared house rows; it never includes another tenant's upstreams.

### `Vhost` — one server block

`server_name` is **derived**: `label + "." + domain.name`, or the apex when
`label` is empty. The web root is **derived from the row's own pk**.

> **There is no `root_slug`.** An earlier design had one. A tenant-writable
> slug becomes a cross-tenant read the moment webapp releases land: point it at
> another tenant's slug and serve their private build output — source maps,
> embedded config, admin bundle — from your own domain. An integer primary key
> cannot traverse, collide, or name someone else's content.

`headers` is not a field in v1. The base config sets the security headers, and
every structured field is another renderer input to test.

Constraints worth knowing about:

- **`UniqueConstraint(["domain", "label"], condition=is_enabled)`** is the
  server_name collision defence. **Not `nginx -t`** — nginx treats a duplicate
  `server_name` as a *warning*, silently drops one block, and exits 0. A
  disabled row may sit alongside an enabled one so a replacement can be staged.
- **`CheckConstraint`** on the kind/upstream pairing, enforced in the DB as
  well as in `save()`, so a bulk write cannot produce a hybrid row the renderer
  has no branch for.

Validation lives in `Model.save()`, not a REST hook, so a service-layer or
shell write is exactly as safe as a REST one.

### The house guard

A vhost whose `domain.group_id` is null is platform property. `Vhost` scopes
through `GROUP_FIELD = "domain__group"`, and for a house domain that resolves
to `None` — at which point the permission check falls through to the caller's
**global** permissions, so any global `manage_dns` holder would pass. The
detail guard in `rest/vhost.py` is load-bearing, and it runs **after** the model
check on purpose: leading with it would answer 403 for a house vhost and 401
for a tenant one, classifying the platform's inventory for a caller who can
read neither. Same shape and same reasoning as dnsman's house-certificate
guard. The list path separately excludes house vhosts for every non-superuser,
including a caller with global `manage_dns`; literal platform superusers retain
the complete house inventory.

## The generation lifecycle

A **generation** is the complete set of files a node should be serving,
identified by a sha256 over the desired-state *inputs* (never the rendered
text — the text embeds the generation directory).

```
EDGE_ROOT (default /opt/api/var/edge)
  generations/<generation>/
      nginx.conf                     harness, pre-filter `nginx -t` only
      conf.d/<vhost-id>.conf
      certs/<cert-id>/{fullchain.pem,privkey.pem}   0600, app-user owned
      www/<vhost-id>/                the web root (a release symlink later)
  current -> generations/<generation>
  installed.json                     {generation, excluded[]}
```

`/etc/nginx/conf.d/mojo.conf` is a one-line
`include /opt/api/var/edge/current/conf.d/*.conf;`, written once at
provisioning time and never by this code.

Rendered configs name certificates by **absolute generation path**, not through
`current`. That is what lets `nginx -t` validate the *new* material rather than
the installed material, and it makes the swap a single `os.replace` of one
symlink — with rollback being the same operation pointed at a retained
generation whose own files are still on disk.

### The install sequence

```
fetch desired state (from the DB, via render.desired_state)
if generation == installed.json.generation: return          idempotent no-op
render generations/<new>/, write certificate material 0600
    material unfetchable:  house vhost  -> abort
                           tenant vhost -> exclude it, report an incident
nginx -t -c generations/<new>/nginx.conf                    cheap pre-filter
os.replace(current -> generations/<new>)                    nothing has reloaded
nginx -t                                                    against the REAL config
    fail, or "conflicting server name" on stderr -> revert current, incident, raise
    ok -> systemctl reload nginx, write installed.json, prune
```

Three properties this buys, each with its own test:

- **Validation against the real config.** A harness only approximates the
  deployment's own `nginx.conf` — a `map`, `upstream` or `limit_req_zone`
  defined there and referenced by generated output passes or fails differently
  under it. Doing the authoritative check *after* the swap is safe because
  nginx serves the running configuration until something reloads it, so a bad
  `current` is reverted before it can ever be loaded.
- **A collision is a failure even though nginx exits 0.** The installer scans
  stderr for `conflicting server name`.
- **One tenant cannot freeze the fleet.** Certificate material can be
  unreadable for reasons unrelated to the row (`KSMSecrets` returns an empty
  mapping when KMS is down). A single abort path would stop every node in the
  pool from converging — including on an urgent renewal of the platform's own
  certificate — and would fail silent-but-serving until something expired.

**Stale config serves; missing config does not.** No code path deletes or
repoints `current` on a failure, so a node whose database is unreachable at
boot comes up on the retained generation.

### Pools

`Vhost.pool` (default `"default"`) is the per-node dimension. Nodes poll for
their own pool and the generation hash covers only that pool's rows, so a
staging pool or an isolated tenant node costs one filter clause rather than a
second convergence mechanism.

### Triggers

| Trigger | What it is for |
|---|---|
| `edge.install_generation` broadcast on the `edge` channel | Something changed and we were told |
| `converge_edge` cron, every 10 minutes | A node that missed a broadcast, booted from an AMI, or had its runner stopped |
| dnsman's `certificate_updated` | A renewal landed |

The sweep is what makes convergence a property rather than a hope: every
broadcast is best-effort, and nothing maintains a node inventory.

`EDGE_CONVERGE_ENABLED = False` (settings-file-only, read with `get_static`)
switches the sweep off for deployments that install this app **only** for the
fleet-deploy plane ([deploy.md](deploy.md)) and manage nginx some other way —
without it they would broadcast convergence onto an `edge` channel none of
their runners consume, every 10 minutes, forever. Default True; existing edge
deployments are unaffected.

## Machine endpoints and the permission that gates them

`GET /api/edge/desired_state` and `GET /api/edge/material/<pk>` are gated with
**`requires_global_perms("edge_node", allow_api_keys=True)`** — never
`requires_perms`.

That distinction is not stylistic. `requires_perms` falls back to
`request.group.user_has_permission(...)` using a **client-supplied `?group=`
param** (`REQUIRES_PERMS_IS_GROUP` defaults to True), and unlike the ApiKey
floor the GroupMember side has no framework protection map —
`_member_perms_protection()` returns `{}` by default, so an unlisted permission
falls through to "is this caller a group admin?", which every tenant admin is
inside their own group. Under `requires_perms` that composes into: grant
yourself `edge_node` as a member of your own group, read every vhost in the
pool, then read every referenced certificate's private key, house certificates
included.

`edge_node` is also on `APIKEY_PERMS_PROTECTION_DEFAULTS`, so the ApiKey half
is closed too: granting it requires the granter's global `sys.edge_node`.

**Why a second material endpoint exists.** dnsman's
`certificate/material/<pk>` runs `require_platform_admin`, which refuses
key-backed sessions outright — and every platform vhost sits on a house domain,
so a node's ApiKey is refused on exactly the certificates it exists to install.
Weakening that gate was not an option. `edge/material` is **strictly tighter**
for this use: it serves only certificates an enabled `Vhost` actually
references, so a node key reads what it installs and nothing else, and an
unknown certificate is indistinguishable from an unreferenced one.

## The privilege boundary — state it plainly

The installer runs as the **app user** in the job runner. Everything goes
through one function (`installer._run`) with a **constant argv list** built
from file settings — never `settings.get`, which would resolve a DB-backed
`Setting` row and make the argv row data.

### The sudoers rule, and the one that would be a root escalation

Exactly two commands need root, and **neither takes an argument**:

```
# /etc/sudoers.d/mojo-edge
mojo ALL=(root) NOPASSWD: /usr/sbin/nginx -t
mojo ALL=(root) NOPASSWD: /usr/bin/systemctl reload nginx
```

> **Do not add a rule for the staged check.** It is tempting to also permit
> `nginx -t -c /opt/api/var/edge/generations/*/nginx.conf` — that is the
> command the pre-filter runs, and the generation hash forces a wildcard. It
> would be a root escalation, not a config check: **`nginx -t` processes
> `load_module` while parsing and `dlopen()`s the named object as whatever user
> it runs as.** The app user writes that config file, so one
> `load_module /tmp/evil.so;` line in it is arbitrary root code execution — and
> because the hash is in the path, the wildcard cannot be narrowed away.
>
> The staged check therefore runs **unprivileged**, which it can, because every
> file it reads is app-owned and `-t` binds no ports. That is why
> `render_nginx_harness` puts `pid`, `error_log` and every `*_temp_path` inside
> the generation directory: an app user cannot write nginx's packaged prefix,
> so the defaults would fail the check for permissions rather than for config.

Absolute binary paths matter — a sudoers rule naming a bare `nginx` is
satisfied by anything first on `PATH`.

> The structured-model constraint at the top of this document defends against a
> malicious **admin**. It does nothing about a compromised **API process**,
> which now has a path to nginx configuration. What bounds that is the sudoers
> narrowness plus the app user owning only `EDGE_ROOT` — not the renderer.

Writing that sudoers rule and the `/etc/nginx/conf.d/mojo.conf` include is
**django-mojo-skeleton work** and is a deployment dependency of this app. Until
it exists, the installer can be unit-tested but cannot be exercised on a node.

## Settings

| Setting | Default | Purpose |
|---|---|---|
| `EDGE_ROOT` | `/opt/api/var/edge` | Generations, `current`, `installed.json` |
| `EDGE_WWW_BASE` | `/opt/www` | Where installed releases live |
| `EDGE_SOCKET_BASE` | `/run/mojo` | Unix upstream sockets must resolve under this |
| `EDGE_RESERVED_SERVER_NAMES` | — | Names no vhost may claim. **See below.** |
| `EDGE_TLS_PROTOCOLS` | `TLSv1.2 TLSv1.3` | The TLS floor |
| `EDGE_TLS_CIPHERS` | modern suite | The TLS floor |
| `EDGE_KEEP_GENERATIONS` | `5` | Retained generations (rollback depth) |
| `EDGE_POOLS` | `["default"]` | Pools the convergence sweep covers |
| `EDGE_NGINX_TEST_CMD` | `["sudo","-n","nginx","-t"]` | Root check, no arguments |
| `EDGE_NGINX_STAGED_TEST_CMD` | `["nginx","-t","-c"]` | Staged check, **unprivileged** |
| `EDGE_NGINX_RELOAD_CMD` | `["sudo","-n","systemctl","reload","nginx"]` | Constant argv |
| `EDGE_COMMAND_TIMEOUT` | `60` | Seconds |

**`EDGE_RESERVED_SERVER_NAMES` fails closed.** The reserved set is Django's
`ALLOWED_HOSTS` (concrete entries only) plus this setting. If `ALLOWED_HOSTS`
is `["*"]` or empty *and* this setting is unset, **no vhost can be enabled** —
a deployment that cannot name its own hostname cannot protect it, and silently
allowing every name is the shadowing attack. Declare it.

## Testing

`tests/test_edge/` — validators, models and constraints, REST permissions,
render injection, golden files, desired state, and the installer.

`7_nginx_real.py` feeds the generated configuration to a **real** `nginx -t`
under `--extra extended` (or `--full`). It **skips** when nginx is absent.

That skip is not optional. django-mojo's suite runs inside every project that
uses the framework, so a test that fails on a missing binary turns all of them
red on the next release — and nginx is not a dependency of this package. Any
test here needing a binary we don't ship must skip, never fail.

The renderer is still covered without it: the golden files
(`4_render_golden.py`) pin the exact bytes emitted, and `3_render_injection.py`
holds the injection and containment assertions. What only a real nginx can
answer is whether it *accepts* those bytes — run the extended tier on a host
with nginx, or rely on the installer's own `nginx -t` gate, which runs against
the production harness before any reload and keeps a failed generation out of
service.

## What this replaces

`certbot_sync.py` in django-mojo-skeleton, and with it the gatekeeper role, the
S3 certificate bucket, `PRIMARY_BALANCER_HOST`, the primary/replica direction,
and the operator-only `vhosts/` S3 prefix. A node asks "what should I be
serving?" and installs the answer.

Removing them is skeleton-side work and is sequenced: install this app on every
node → verify `installed.json` fleet-wide → then delete the old path.
