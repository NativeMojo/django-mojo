# Packaged Portal and legacy Admin

The default configured Admin route (normally `/admin/`) remains legacy Admin.
Its **Open Portal** sidebar destination opens `/admin/v2/` in the same tab.
With `MOJO_ADMIN_PATH = "operations"`, these become `/operations/` and
`/operations/v2/`. The backend validates this configuration as one
letter/digit/underscore/hyphen segment. No credentials are included in the link.

Portal's UI source and build belong to **portal-mojo**. Django vendors the
complete canonical `dist/admin` artifact, not the ordinary Portal build.
The installed Python package requires no Node, npm or frontend build.
Compiled assets use a relative base; Portal API requests always use same-origin
`/api/...`, independent of the Admin mount and legacy bootstrap.

## Pinned identity and offline replacement

The current artifact is portal-mojo **0.2.3**, source revision
`ec2cf8037afbf3ebb57192a54a5786c149895cc5`, built clean with Node
**24.21.0** / npm **11.19.0** and lockfile SHA-256
`92cd7305e4929293a61def2bd87f32439c732a1bbb0b91de3d840cd412148bac`.
Its 116-file inventory includes the Vite manifest and lazy chunks.
The identity is the SHA-256 of the exact `admin-artifact.json` bytes:

`8012f664ecb2ead262a240549fc33e70ac45c75d1cb6bded1e8c17e217049efb`

This pin comes from the successful [portal-mojo 0.2.3 release build](https://github.com/NativeMojo/portal-mojo/actions/runs/34546230351),
artifact `portal-mojo-admin-0.2.3-ec2cf8037afbf3ebb57192a54a5786c149895cc5`
(GitHub artifact ID `10179244830`). The complete artifact includes hidden
`.vite` content; verify the manifest digest before vendoring it.

Stop processes serving/importing the checkout before replacing the artifact:

```bash
uv run python scripts/vendor_admin_portal.py \
  --source /absolute/path/to/verified/dist/admin \
  --expected-manifest-sha256 8012f664ecb2ead262a240549fc33e70ac45c75d1cb6bded1e8c17e217049efb
uv run python scripts/vendor_admin_portal.py --check
```

The tool is offline, rejects overlapping paths and symlinked ancestry, takes an
exclusive lock, verifies the source, stages only declared files plus metadata,
reverifies, and replaces only `mojo/apps/account/admin_portal_v2/`.
Identical bytes are a no-op. A failed promotion restores the previous tree.
A crash between the two renames can briefly leave no live tree: this is
recoverable offline replacement, not uninterrupted live deployment.

The deterministic checkout-root `.admin-portal-v2.backup/` is retained if
restoration fails. Re-run the vendor command to recover and install the pinned
source. If both the destination and backup exist but the destination fails
validation, stop and preserve both for diagnosis; restore that exact backup
with processes stopped before retrying. Lock, stage and backup paths are
gitignored and forbidden in archives. Interrupted orphan stages contain no
authoritative copy and may be removed after confirming no vendor process runs.

`services/admin_artifact.py` uses only the Python standard library. Repair
and package commands load it directly, so they work even when an invalid v2
tree prevents Django startup. The runtime loader proves the pinned bytes at
startup and delivers only its validated allowlist. Provenance and
`.vite/manifest.json` ship in the package but return HTTP 404.

## Included Portal behavior

Portal 0.2.3 adds **Phone Hub → SMS → Send SMS** for a recipient and custom
message. The composer uses the existing `POST /api/phonehub/sms/send` endpoint
with only `to_number` and `body`, so sending uses the effective system
configuration and default sender. SMS visibility and the separate global
`sys.send_sms` or `sys.comms` grant are both required. It preserves drafts
after transport or malformed-response failures, displays the returned
SMS record's `data.status`, and refreshes the audit list after every attempted
send. A successful envelope can contain a `failed` or `undelivered` record;
only `delivered` confirms delivery. An uncertain result must be checked in the
audit before retrying. See the [Phone Hub REST contract](../phonehub/rest.md)
for the request and response formats.

The bundle also includes responsive detail dialogs and contextual lifecycle
actions, improved Phone Hub configuration, a direct Dashboard destination,
and expanded incident/event evidence. These are frontend updates against the
existing APIs; no database migration is needed.

## Private source sessions

Interactive global Admin admission remains required. API keys, group tokens,
ordinary authenticated users and anonymous clients cannot mint a source
session. A valid source cookie admits static source only; every REST endpoint
retains its own user/group/security permission checks. Legacy Security
bootstrap capabilities do not turn `admin` into a Security wildcard.

`POST /api/account/admin/session` returns:

```json
{"status":true,"data":{"path":"/admin/","source_session_expires_in":300,"source_session_expires_at":2000000300}}
```

The two integer fields derive from one issuance deadline, bounded by
`MOJO_ADMIN_SESSION_TTL` and access JWT expiry. The cache stores and explicitly
checks that deadline; cache timeout and cookie Max-Age cannot exceed it.
`issue(request)` retains its session-id-or-None Python return contract;
`issue_with_metadata(request)` is the internal richer API. The source-session
identifier never appears in the REST response.

The HttpOnly, SameSite=Strict cookie remains scoped to the configured Admin
root. Source delivery is no-store; anonymous documents contain only the public
auth gate, and anonymous private assets return 404. Cache failure, expired
grants, inactive users and changed authentication keys fail closed.

Portal, legacy Admin and the gate all hold the origin Web Lock
`mojo:admin-source-session:v1` across every issue/revoke response, including
body completion. The BroadcastChannel uses that same name. The authoritative
localStorage key `mojo:admin-source-generation:v1` stores only
`{version:1,generation:<UUID>,state:"active"|"revoked"}`; sessionStorage binds
each tab to its accepted generation. Messages contain only
`{type:"generation",version,generation,state}` and prompt a reread.

Logout writes/broadcasts a fresh tombstone and clears credentials before waiting
for the lock, then awaits `DELETE /<admin>/_session`. It never overwrites a
newer explicit-login generation. Refresh and resumed tabs cannot reactivate a
tombstone. Hosted auth activates a generation only after explicit credential
completion, and only for a validated same-origin Admin return destination.
The coordination-only public adapter is served no-store. Missing Web Locks,
BroadcastChannel, UUIDs or usable storage yields visible recovery without an
uncoordinated fallback.

## Preview, CSP and release proof

`bin/admin_preview` preserves legacy fixtures and the live proxy. Start at
`/admin/` and use Open Portal; `/v2/` redirects to the canonical
`/admin/v2/` fixture mount. Preview is a deterministic fixture, not evidence
of production authorization. Protected-browser acceptance uses a separate real
Django process and the committed artifact.

The packaged CSP retains same-origin scripts, styles, connections and resources,
data images, no base URI and no framing. The protected Chrome rider demonstrated
that blob image and audio previews require `blob:` in `img-src` and
`media-src`; only those two directives are extended, for v2 only.
Inline scripts, eval and foreign connections remain denied. Same-origin
WebSocket, sandboxed email-frame and credential-free upload fixtures need no
policy extension; these controlled fixtures do not prove external provider
compatibility. External origins still require exact deployment allowlists.
The rider explicitly disables CDP's unsafe-eval bypass and writes probe results
before asserting. Browser evidence is written under
`testproject/var/admin-browser-4060/` (identity, screenshots, CSP and race
records). Do not describe unexecuted fixtures as measured compatibility.

```bash
uv build
uv run python scripts/verify_admin_portal_package.py --dist dist --build-smoke
MOJO_ADMIN_CHROME=/absolute/path/to/chrome \
  bin/run_tests --agent --extra slow -t test_account
```

Package verification rejects duplicate, traversing and nonregular archive
members, validates exact bytes in both wheel and sdist, builds a wheel from the
sdist, installs it into a clean environment and performs a dependency-free
installed-asset smoke check. `publish.py` validates the committed tree before
building and requires archive/build-smoke proof before any push. Its dry-run
continues to print the intended commands without building or publishing.
