# edge templates — the rendered contracts

What every generation actually contains: the four vhost contracts, the
rendered http base, the include graph, the blocklist plane, and the
migration path off a hand-managed nginx.conf.

Companion to [README.md](README.md) (models, lifecycle, permissions) and
[webapps.md](webapps.md) (what a site vhost serves). The byte-exact truth is
`tests/test_edge/golden/` — this document explains it; the golden files pin
it.

## The four kinds

`Vhost.kind` is a **product shape**, not an nginx feature list:

| Kind | Serves | Destination | Knobs |
|---|---|---|---|
| `api` | Whole-host reverse proxy | `upstream` FK (required) | `body_size_mb`, `quiet_paths`, `serve_static` |
| `site` | Static files / SPA | web root = own pk | `spa` |
| `site_api` | Static site + proxied prefixes | one `VhostRoute` (path prefix → upstream FK) per prefix | `spa`, `body_size_mb`, `quiet_paths`, `serve_static` |
| `redirect` | `return 301 https://<host>$request_uri` | `redirect_to` — a validated FQDN, never a URL | `body_size_mb` |

Structural on **every** kind — rendered always, optional never:

- A **443 + http2 server block** with the fleet TLS policy inlined
  (`EDGE_TLS_PROTOCOLS` / `EDGE_TLS_CIPHERS`, re-asserted against a
  whitelist at substitution — never certbot include files).
- When file-only **`EDGE_HTTP_ENABLED` is true** (the compatibility default),
  a **port-80 server block per name**: the ACME webroot location
  (`EDGE_ACME_WEBROOT`) plus a 301 to https. DNSMAN-only fleets set it false;
  they render no public HTTP blocks because DNS-01 issuance needs no listener.
- The **security headers** (HSTS, nosniff, `X-Frame-Options DENY`,
  Referrer-Policy, Permissions-Policy), re-emitted inside any location that
  declares its own `add_header` — nginx header inheritance is
  replace-not-merge, so the no-cache posture on proxied locations and the
  immutable caching on assets would otherwise silently drop them.
- `client_max_body_size` rendered from `body_size_mb` (default 50, range
  1–4096) — nginx's 1m default is never silently in play.
- The **blocklist guards** (`if ($edge_block_ip)` / `if ($edge_block_ua)` →
  444), referencing maps the http base always defines.
- The health-check log exclusion (`$loggable` map) on the main access log.

The injection pin covers both postures: exactly one `server {` per vhost in
DNS-01-only mode and two when public HTTP is enabled, asserted in
`tests/test_edge/3_render_injection.py`.

### Knob semantics

- **MojoSec** — file-only `MOJOSEC_MODE` is `off` (default) or `observe`.
  Observe adds the shared protected raw-evidence JSON security log and an exact
  `/api/incident/mojosec/batch` proxy location (and its slash alias) capped at 512 KiB on API
  vhosts (and `site_api` only when a route covers it). File-only
  `MOJOSEC_TRUSTED_PROXY_CIDRS` is a comma-separated list of canonical CIDRs;
  only those networks may change the resolved client IP. Off emits neither
  the log nor receiver location, so upgrading Edge does not create noise.
  The MojoSec log is fixed at root:root mode-`0600`
  `/var/log/nginx/mojosec.json.log`; unlike ordinary Edge logs it may not live
  beneath the app-owned staging directory. Root sensor enrollment uses
  `nginx_plane=edge` and the same fixed protected collector path.
  The shared standard/Edge format includes request ID, scheme/protocol/TLS,
  user agent/referrer, client/direct-peer/server ports, request/response byte
  counts, and upstream status/timing/length/byte measurements. It deliberately
  excludes bodies, cookies, authorization and arbitrary headers. Observe-mode
  generation is exercised by the real `nginx -t` test when nginx is installed.

- **`quiet_paths`** (`api`, `site_api`) — exact-match locations whose hits
  stay out of the *main* access log. They are never blind: the location
  swaps the main log for the **watch log** (`edge_watch.log ... if=$edge_watch`),
  so a watch-listed client's health probes still surface. On `site_api`,
  every quiet path must sit under a declared route prefix (validated at
  save; a path orphaned later by a route delete renders nothing rather than
  freezing the pool).
- **`serve_static`** (`api`, `site_api`) — a `/static/` alias onto
  `EDGE_DJANGO_STATIC_ROOT`, the fleet's own Django static root. A
  `get_static` path: no row can point it anywhere else. On `site_api` the
  alias uses nginx's `^~` prefix modifier so the site's asset-suffix regex
  cannot capture a Django static request and look for it in the release root.
- **`spa`** (`site`, `site_api`) — `try_files ... /index.html` history
  fallback instead of `=404` + `error_page`.
- **Routes** (`site_api`) — one `location ^~ <prefix>` per `VhostRoute`, so
  longest-prefix wins while the site's asset-suffix regex cannot capture an
  asset-shaped request below a declared API prefix. Every proxied location
  carries the canonical proxy body: upgrade headers (the `$connection_upgrade`
  map lives in the bootstrap — or in the generation base on a node whose
  bootstrap predates it, see below), X-Forwarded set, no-cache posture,
  buffering off,
  `proxy_read_timeout` from `EDGE_PROXY_READ_TIMEOUT`. `proxy_pass` targets
  are **named upstream blocks** (`edge_up_<pk>`), never inline addresses.

## The include graph

Per generation:

```
generations/<gen>/
  http.d/00_base.conf       mime include, log_format + $loggable + access_log,
                            sendfile/keepalive/server_tokens/gzip,
                            blocklist maps + watch log, flag-gated catch-alls
                            (NOT $connection_upgrade — bootstrap owns it,
                            EXCEPT under carry_upgrade_map, below)
  http.d/10_upstreams.conf  upstream edge_up_<pk> { server <target>; } blocks —
                            the ONLY place a literal host:port / socket appears
  conf.d/<vhost-pk>.conf    HTTPS server, plus HTTP when enabled
  staging/http.d/           listen-remapped copies of the two trees above —
  staging/conf.d/           identical bytes except active listeners become staged
                            ports; ONLY the pre-filter reads them
  certs/<cert-pk>/          fullchain.pem + privkey.pem, 0600
  www/<vhost-pk>/           web roots (release symlinks)
  nginx.conf                the staging harness (pre-filter nginx -t only;
                            includes staging/, never the real trees)
```

`/etc/nginx/nginx.conf` on an API node is the project's small bootstrap. The
packaged API activation (optionally entered through the project's permanent
`aws/post_deploy.sh` locator shim) reinstalls the repository copy on each
deployment and accepts it only after the real `nginx -t`:

```nginx
# /etc/nginx/nginx.conf — bootstrap. The app owns everything under current/.
user             www;
worker_processes auto;
error_log        /var/log/nginx/error.log info;
pid              /run/nginx.pid;

events { worker_connections 5024; }

http {
    default_type        application/octet-stream;
    types_hash_max_size 4096;

    map $http_upgrade $connection_upgrade {
        default upgrade;
        ''      close;
    }

    include /opt/api/var/edge/current/http.d/*.conf;
    include /opt/api/var/edge/current/conf.d/*.conf;
}
```

Notes that bite:

- **`default_type`, `types_hash_max_size` and the `$connection_upgrade` map
  live HERE, not in the rendered base.** Any of them at the same http level
  twice is a duplicate-directive `[emerg]`. The rendered base deliberately
  does not emit them.
- **The upgrade map is in that set for a second reason**: a node that also
  serves a classic `conf.d` vhost — one proxying to the app through
  `asgi.inc` — references `$connection_upgrade` *before* the first
  convergence, when `current/` does not exist and both globs below match
  nothing. Owned only by the rendered base, such a node could not start nginx
  until it had converged, and could not converge without serving. Owned here,
  day 0 works and a hybrid node is possible at all.
- **Day-0 is safe**: before the first converge, `current/` does not exist
  and both globs match nothing — nginx still starts. If the load balancer
  deliberately exposes HTTP and needs an answer before the first converge,
  add a minimal probe server to
  the bootstrap (`server { listen 80 default_server; return 204; }`) and
  **remove it before Edge takes over port 80** — two `default_server`s on one
  port is an `[emerg]`. A DNS-01-only fleet needs no port-80 day-zero probe.
- **A bootstrap without these includes fails silently-but-converged.** The
  installer stages files, `nginx -t` passes (the real config simply never
  reads the generation), the node reloads and reports the generation
  installed — and serves none of it. Nothing errors. If a vhost "installed
  everywhere" but does not serve, check the node's bootstrap first.
- **A bootstrap that predates the map is healed, not required.** Exactly one
  `$connection_upgrade` declaration may exist per graph, and two mechanisms
  keep that true on nodes provisioned before this contract (their bootstrap
  declares no map, so a map-less generation can never activate). The Edge
  installer probes with the real `nginx -t`: an
  `unknown "connection_upgrade" variable` verdict re-stages the generation
  with `carry_upgrade_map` — the base declares the map, the harness yields
  it, and the variant lands in its **own** generation directory
  (`render.local_generation_id`) recorded in `installed.json`; a later
  duplicate verdict flips it back. Do not "fix" a carrying node by adding
  the map to its bootstrap without expecting one failed converge first —
  the duplicate verdict is how the generation learns to stop carrying it.
- **A generation id moves whenever the renderer moves.** The framework
  version participates in `generation_id`, so a django-mojo upgrade
  re-stages into a fresh directory and takes the full pre-filter → swap →
  real-test gate. The installer additionally refuses to rewrite a LIVE
  generation's rendered files with changed bytes — an in-place rewrite of
  the active include graph has no swap to revert, which is exactly how the
  wedge happened.

The staging harness (`nginx.conf` inside the generation) mirrors the
bootstrap: main context + the same two directives — but its two includes read
the **`staging/`** copies, not the real trees. The staged `http.d/00_base.conf`
is the single owner of the five generation-local scratch directives; the
harness deliberately carries no duplicate. The staged
`nginx -t` runs unprivileged, and nginx attempts `bind()` on every `listen`
during `-t` (only `EADDRINUSE` is tolerated in test mode; the `EACCES` an
unprivileged process gets for 443/80 on Linux is fatal) — so the staged
copies remap every active listen port to `EDGE_STAGED_HTTP_PORT` /
`EDGE_STAGED_HTTPS_PORT` and change nothing else: `ssl` survives, so the
staged certificates are still opened and validated. The authoritative check
is still the real config, real ports included, after the swap — see README's
install sequence. In HTTPS-only mode the HTTP staged port is not resolved or
validated, so an obsolete value cannot block a DNS-01-only generation.

Those five names come from `mojo.deploy.nginx_runtime.TEMP_PATHS`. The
installer creates every scratch leaf before the unprivileged staged check.
Never copy the directive list into an Edge template: a missing leaf recreates
staging-only validation drift, while a duplicate at HTTP context makes nginx
refuse the generation. Production nginx scratch paths remain the project's or
nginx's own configuration; deploy rendering no longer creates a privileged
runtime fragment.

## Node prerequisites (1.6.0)

What a node must provide before the edge plane can converge — everything
below is provision-time (skeleton/`aws/` contract) work, not something this
app writes:

- **nginx ≥1.25.1.** The rendered 443 blocks carry `http2 on;`, which does
  not exist below 1.25.1 — an older nginx fails every check at parse time
  with `unknown directive "http2"`.
- **Kernel IPv6 enabled.** Every vhost renders `[::]` listens. A node booted
  with `ipv6.disable=1` fails both the staged and the root check with a
  socket-family `[emerg]` that looks nothing like a config problem.
- **The bootstrap includes.** `/etc/nginx/nginx.conf` must include
  `current/http.d/*.conf` then `current/conf.d/*.conf` (the ~12-line form
  above). Without them the node converges silently and serves nothing — the
  failure mode described under "Notes that bite". **Keep both globs exactly
  this shape — non-recursive.** `staging/` lives inside the directory
  `current` points at; a broadened glob (`current/*/*.conf`) would make the
  root nginx serve a full duplicate of every vhost on the staged ports, and
  nothing would flag it — a different addr:port raises no
  `conflicting server name` warning.
- **The app user owns `EDGE_ROOT` and `EDGE_LOG_DIR`**, and everything the
  installer writes lives under them: generations (including `staging/`),
  certificate material, `installed.json`, the access/watch logs. Release
  bundles under `EDGE_WWW_BASE` are app-written too.
- **What the app user does NOT need** — and must not be given: write access
  to `/var/log/nginx`, and the ability to bind 443/80. The staged check
  binds only the staged ports for listeners actually rendered; the harness
  keeps every scratch path
  inside the generation.
- **Keep the staged ports unreachable from outside the node.** 61080/61443
  are validation scratch, not a serving contract: the staged `-t` briefly
  listens on them (never accepting a connection, gone in milliseconds).
  No security-group or firewall rule should expose them.
- **The two sudoers commands**, exactly and only: the argument-free root
  `nginx -t` and `systemctl reload nginx` — the exact text and the reasoning
  (including why the staged check must never get a sudo rule) are in
  [README's privilege boundary](README.md#the-privilege-boundary--state-it-plainly).
- **The nginx worker user can read what it serves.** Workers run as the
  bootstrap's `user`; web roots resolve through
  `EDGE_ROOT → generations/<gen>/www/<pk> → EDGE_WWW_BASE` releases, so that
  chain must be traversable (`o+x`) and the release files readable by that
  user. The fetcher sets both explicitly for exactly this reason: every
  release file lands `0644` and every release directory it creates is
  `0755`, rather than inheriting the runner's umask — under a hardened
  `UMask=0077` an inherited mode is `0700`, and nginx cannot traverse into
  the release it is meant to serve. Certificate material does not need this:
  the root master process reads keys at load time.

## Blocklists — data, log-first

`BlocklistEntry` rows (`kind` `ip`|`ua`, `mode` `allow`|`off`|`log`|`enforce`)
render into every generation's base as:

```
geo $edge_block_ip  { default 0; <allow nets> 0; <enforce nets> <row-id>; }
geo $edge_watch_ip  { default 0; <allow nets> 0; <log nets> <row-id>; }
map $http_user_agent $edge_block_ua { default 0; "~*<allow>" 0; "~*<enforce>" <row-id>; }
map $http_user_agent $edge_watch_ua { default 0; "~*<allow>" 0; "~*<log>" <row-id>; }
map "$edge_watch_ip:$edge_watch_ua" $edge_watch { default 1; "0:0" 0; }
```

plus `log_format edge_watch` and
`access_log <EDGE_LOG_DIR>/edge_watch.log edge_watch if=$edge_watch;`.
Every 443 server block carries the two guards (`return 444`).

Semantics:

- **`log` is the default and the posture**: a new rule observes in the watch
  log until a human flips it to `enforce`. The watch log line carries the
  matching **row id**, so "what rule fired?" is a grep, not a guess.
- **`allow` rows render FIRST in every map they join.** nginx `map` regexes
  match in order of appearance, so an early `0` beats any later pattern that
  also matches. The canonical case ships in the seed: Lynx user agents
  contain `libwww-FM`, which the unanchored `libwww` token matches — the
  `^Lynx` allow row is what keeps real Lynx clients unblocked. In `geo`
  blocks order is cosmetic (most-specific network wins), but the convention
  is kept.
- **`off` renders nowhere** and drops out of the hashed payload — a parked
  rule with its history intact.
- Values are whitelist-validated at save AND re-asserted at render; `ip`
  values are stored normalized (`10.1.2.3/8` → `10.0.0.0/8`) because two
  spellings of one network in a `geo` block is an nginx `[emerg]` — which,
  under this architecture, would freeze fleet convergence. The renderer also
  de-duplicates defensively.

The 0004 migration seeds the skeleton's `sec.d` content as rows in `log`
mode (the 2,635-character bad-bot alternation split per token, `^Lynx` as
`allow`, zero IP rows). Fleet-scoped: managed with **global**
`manage_security` — the model is deliberately group-less and a `?group=`
param opens nothing.

**During migration, file-managed `sec.d` and the rendered maps coexist**
safely: the variable names differ (`$bad_bot`/`$is_blocked_ip` vs
`$edge_block_*`), so a bootstrap still carrying
`include /etc/nginx/sec.d/*.conf;` conflicts with nothing. The old files
only *enforce* if the old server blocks still reference their variables;
retire the include with the conf.d glob at the last step.

## Colliding with a hand-written server block

Any name a `Domain`'s owner holds may be served through the edge — there is no
reserved list. What the edge cannot see is nginx configuration it did not
generate: a server block written by hand in the bootstrap or `conf.d` claims a
name without any row existing for it.

**So the cutover is sequenced, and the conflicting-server-name scan is the
reason.** While the file-managed server block for a name still exists,
enabling an edge vhost for the same name makes every converge fail: the
real `nginx -t` reports `conflicting server name`, the installer treats
that as fatal (nginx would silently drop one block otherwise), reverts
`current`, and raises an incident — on every node, every sweep, until the
file block is gone. So: **retire the file-managed server block and enable
the edge row in the same maintenance window.** Order inside the window:
enable the row (converges now fail loudly — expected), delete the
file-managed block, reload once by hand or let the next sweep converge.

## Migrating a node off the hand-managed config

One step at a time, each independently verifiable:

1. **Update the bootstrap** to the ~12-line form above (keep the old
   `conf.d`/`sec.d` includes for now if the node still serves file-managed
   vhosts; the edge includes match nothing until a generation lands).
2. **Converge** (`install_generation` broadcast or the 10-minute sweep) and
   verify `installed.json` and that `current/` is populated.
3. **Per domain**: delete the file-managed server block and create/enable the
   edge vhost row — same window, per the sequence above.
4. **Flip `EDGE_HTTP_DEFAULT_SERVER`** once no file-managed default server
   remains (and remove any day-0 probe server) — the rendered catch-alls
   take over unmatched names: 443 rejects the TLS handshake without a
   certificate; 80 answers 444 only when `EDGE_HTTP_ENABLED=True`.
5. **Retire the old includes** (`conf.d` glob, `sec.d` glob) from the
   bootstrap when nothing file-managed remains.

For a DNS-01-only cutover, first publish `EDGE_HTTP_ENABLED=False` through the
deployment's canonical static configuration, then wait for every node's Edge
generation to converge and verify `nginx -T` contains no `listen 80` or ACME
challenge location. Only after that proof should the infrastructure owner
remove the load-balancer listener and port-80 security-group ingress. Reversing
that order can strand a fleet that still expects HTTP redirects or HTTP-01.

**Logs move.** The rendered base writes the main access log and the watch
log under **`EDGE_LOG_DIR`** (default `<EDGE_ROOT>/log`), app-owned so the
unprivileged staged `nginx -t` can open them — NOT `/var/log/nginx`.
The MojoSec raw-evidence log is the deliberate exception: staged variants
disable access logs, while the authoritative root check opens its fixed
root-only `/var/log/nginx/mojosec.json.log` descriptor.
Repoint logrotate and OSSEC/file-integrity watches when a node migrates, or
the fleet goes quiet in the places dashboards watch. nginx's own
`error_log` stays wherever the bootstrap points it.

## Settings this document adds

See README's settings table for the full list; the template-plane knobs are
`EDGE_HTTP_ENABLED`, `EDGE_ACME_WEBROOT`, `EDGE_LOG_DIR`, `EDGE_MIME_TYPES`,
`EDGE_DJANGO_STATIC_ROOT`, `EDGE_PROXY_READ_TIMEOUT`,
`EDGE_HTTP_KEEPALIVE_TIMEOUT`, `EDGE_HTTP_DEFAULT_SERVER`,
`EDGE_STAGED_HTTP_PORT`, `EDGE_STAGED_HTTPS_PORT`.
