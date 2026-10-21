# Guarded shared-validation operator service

The retained operator service uses a dedicated independently root-pinned HTTPS
hostname. An application-relative maintenance path does not isolate the browser
from application scripts or an existing application service worker. The retained
origin serves only root-owned allowlisted UI/API bytes, with no application proxy,
worker registration, opener, CORS or token relay. Its existing WebAuthn credential
must be valid for its approved RP, and every signed clientData origin must equal
that guardian origin. An approved profile is expected configuration until actual
runtime checks establish its observed state.

The service sources are inert until an independently authorized root deployment
installs the exact qualified retained artifact, profile and separately locked
runtime principals. A normal Django import, framework update, app JWT, Jobs
message or successful package deployment never installs or enrolls root authority.
Missing real hostname/TLS/DNS/profile/credentials/account/coordinator/transport
facts remain unavailable. Source and disposable fixtures do not qualify a live
estate or authorize publication, installation, DNS or certificate operations.

## Pure protocol and runtime boundary

`mojo/deploy/acme_hub_operator_protocol.py` defines bounded duplicate-rejecting
JSON frames, fixed action names, strict request schemas and the retained HTTP
result envelope. Responses have exactly `version`, `ok`, `data`, `error`,
`request_id`, `revision`, `observed_at`; a refusal has null data and a fixed safe
error code. Sensitive requests contain the server-owned operation/revision,
replay reference and one-use guardian action authorization. They never accept a
PID, collector, helper receipt, arbitrary executable/path/unit/SSH endpoint,
provider body or proof Boolean.

The retained entry is `/_mojo/acme-upgrade/` on the approved guardian hostname;
its browser API is below `api/`. Session options and verification carry only
bounded WebAuthn challenge/assertion JSON. The browser uses the guardian's own
`__Host-mojo_acme_operator` Secure HttpOnly SameSite=Strict Path=/ cookie (no
Domain) and `X-Mojo-Acme-CSRF`. Host, exact Origin and same-origin Fetch Metadata
are checked independently; a sibling origin can be same-site. Wrong or missing
trusted account/pin/runtime state cannot become an authenticated session.

The UI consumes explicit server-enabled actions, safe source-labelled facts and
current root clocks. It must not infer eligibility from green text or local
checkboxes. `inspect` and `verify` are bounded observations, never an alternate
route to establish holds, enroll, restore or mutate a provider. An uncertain POST
is followed by an explicit status read rather than automatic mutation replay.
Actual server-owned existing-account/public-credential/approved-profile choices
and operation review references must be integrated before candidate/action
controls are enabled; no person pastes UUIDs, credential IDs or digests.

`acme_hub_operator_profile.py` reads one fixed root-owned data profile. The broad
operator profile binds approved origin/RP, actual nodes/host keys, runtime digests,
independent enrollment policy and separately approved producer/witness artifacts.
Its digest is distinct from ProducerCoverageV1.profile_sha256, which binds the
shared ProducerProfileV1 document. Declared node or source facts are not observed
process identity and do not constitute coverage.

`acme_hub_operator_runtime.py` validates a complete root-owned immutable inventory
without importing candidate code. Paths, sizes, hashes, modes, interpreter and
entry are pinned; unlisted files, links and import hooks are refused. Child
launch drops supplementary groups/GID/UID and enables no-new-privileges and
non-dumpable before framework or verifier imports. Production root entries load
standalone sibling modules from retained verified bytes; importing the normal
`mojo` package would enter framework helpers and is outside that root path.

`acme_hub_operator_bridge.py` binds Linux spawned peers using pidfd, boot/start,
UID/GID, executable, argv digest, cgroup and the actual inherited AF_UNIX
SOCK_SEQPACKET channel. Every frame checks SCM_CREDENTIALS and a strict operation,
root revision/generation, nonce, session and sequence. A same-UID process, copied
receipt, dead/reused PID, replaced channel or durable mirror is not live authority.
Unsupported kernels refuse; macOS unit checks are not Linux qualification.

The fixed coordinator/account-status/provider/writer qualifier runtimes, DB
schema/privileges/CAS, shared producer and coverage readers, current floor helpers,
retained deployment hooks and critical manifests are integrated only against the
actual reviewed foundation and adapter source. Pure protocol fixtures cannot
implement a competing ledger/profile or fabricate successful finality.

## Qualification and ordinary compatibility

Actual qualification needs the complete current Native and application producer
roster, future-launch exclusions, durable holds, same-owner floors, real primary
ledger cutover and conclusive all-prior outcomes. The producer uses one frozen
credential snapshot for fixed STS identity and the actual one-attempt Route53
writer. The separately read-only witness verifies its own identity but attributes
history to that captured mutation producer/session/key/request. Neither root nor
the DB coordinator receives AWS credentials or project settings.

An ordinary compatibility acknowledgement may independently permit a separately
current ordinary lifecycle under the backend's immutable policy. It never
qualifies coverage, settles unknown prior requests, enables strict manual cleanup,
sets `can_clear_local`, clears a strict-finality marker or downgrades a qualified
operation. Missing old history may remain unavailable indefinitely.

Final acceptance requires the combined framework and exact affected serial union,
real disposable Linux root/systemd/SSH/process/pipe/flock/PG and browser evidence,
including a hostile application root-scope service worker and a fresh guardian
sign-in with target HTTP/Jobs stopped. Source-only tests and unavailable fixtures
are reported separately from that required qualification.

## Unprivileged source fixture

`UnavailableSourceFixture(asset_root)` in `acme_hub_operator.py` binds only an
ephemeral127.0.0.1 TCP port and refuses root execution. It uses the actual HTTP
framing and request safeguards but hardwires unavailable authority and labels
facts fixture. It cannot enable enrollment, authentication, an action or provider
proof. The caller owns its process and shuts it down. This fixture supports
development UI/refusal checks; it supplies no production TLS/profile or browser
isolation qualification. Production binds only the fixed retained Unix socket
behind the independently approved HTTPS guardian vhost.

Inherited seqpacket I/O is nonblocking. Select/monotonic deadlines bound both
send and receive, each message preserves atomic packet and sequence semantics,
and the channel retains an absolute600s ceiling plus30s received-peer heartbeat.
A blocked send never resets deadlines or advances its sequence. Peer exit and
failed registration close owned descriptors; a mirrored receipt cannot reconnect
the same session. The Linux fixture's `--bridge-only` regression exercises actual
backpressure, receive timeout, pidfd peer loss and failed-constructor cleanup.
It is separate from full deployment/Linux/browser qualification.

The shared nonroot producer constructor lives at
`mojo.deploy.acme_dns_producer.frozen_producer` with an explicit existing writer
session. The normal AWS helper is a convenience wrapper only; the isolated
qualifier must not import its eager settings/logit package. Integration consumes
the one reviewed common lower seam, not a second SDK/profile implementation.
