# Shared ACME hub cutover

The additive schema starts in **compatibility** mode. Existing owner-only
allocate/publish/withdraw and sweep behavior continues, including old ORM
lease inserts with no participant column. It creates no owner/trust rows and
performs no lazy binding. Capabilities keeps its v2 shape and returns
`shared_validation:false`, `can_consume:false`, `can_manage_trust:false`.
Shared lookup/join/controller operations refuse. Project setup returns the
existing `configure_hub` action, for the deployment operator to complete this
cutover. No new public status or action enum is introduced.

`dnsman.AcmeHubInstallation` is a migration-seeded singleton, never a Setting.
Its absence, malformed state or wrong epoch is unavailable, never compatibility.
Reads do not repair it. Migration0007 repairs the physical nullable lease FK
if the original unpublished0005 was applied locally. Existing participant rows
from that migration require qualification; ordinary compatibility mutations
refuse them. The migration does not remove them, synthesize trust, rewrite
historical values/timestamps, or enable sharing.

This document defines a bounded **operator-led** one-time cutover. Shipping the
source does not enroll a root helper, grant access, publish a package, stop a
host, or authorize downtime. An exact later live GO and actual qualified
operator facts are required. No SSH, SSM or general remote command facility is
provided. Missing evidence remains `evidence_unavailable`.

## Artifact and release qualification

Use a reviewed, published final wheel after the publication hold is released.
The build hook binds epoch1, full committed source SHA, critical-file manifest
and release inside the wheel and reports the actual outer wheel SHA-256.
Editable/unbound sources cannot qualify. The loaded identity is captured once
per process; inspecting newer files cannot prove an old worker reloaded them.
The tuple stays private and does not extend the existing public node-proof
REST shape. A first import of this metadata into a long-lived old process
after pip changes can describe new files while other old modules survive.
It is never proof that HTTP/worker processes restarted: actual stopped/replaced
PID/start identities and maintained operator holds remain mandatory.
Fresh-shell output is installed evidence. This source supplies no automatic
collector proving running HTTP/Jobs/scheduler imports. The operator must
qualify actual startup/import and PID/start/restart sequencing while the
reviewed installed artifact stays held; unsupported process observations
remain `evidence_unavailable`, never a private-helper-only proof.

The release policy accepts only canonical dotted numeric final releases with
two through four components. Epoch, pre/dev/local/post releases, unknown
identities, and every release at or below **1.37.4** refuse. New local bytes
still called1.37.3 or1.37.4 cannot become the first floor. The merged
pre-feature release source already reports1.37.4: qualification requires both
a numeric release above that known legacy ceiling and the actual published
feature epoch/source/manifest/wheel identity. This work chooses no next
release number and does not bump or publish one. The runtime uses
`packaging.Version`; the retained verifier uses the same smaller policy with
stdlib parsing, independent of the pip environment being recovered.

Before activation, deploy the compatible release through each application's
existing authenticated signed main-push release plane. A queued old deployment
must not be treated as current: publication, orchestration, node delivery and
the final spawn recheck its durable deployment UUID, exact SHA/framework,
frozen roster/cohort and intended runner. Full convergence and restarted loaded
process identities are prerequisites, not a claim made by a version endpoint.
Before publishing any feature wheel, separately pin deployments where an
unset resolver would otherwise select the newest release.

## Fixed local enrollment contract

An already-authorized effective-root operator must qualify and install these
reviewed retained files on **every writer/deployment node**, outside candidate
pip mutation. The source build never performs this installation:

| Fixed path | Owner/mode | Meaning |
|---|---|---|
| `/usr/local/libexec/mojo-acme-hub-upgrade` | root0755 | Reviewed `mojo/deploy/acme_hub_floor.py`; fixed `/usr/bin/python3 -I -S` startup, stdlib only |
| `/usr/local/libexec/mojo-acme-hub-update.sh` | root0755 | Reviewed packaged `update.sh`, retained for the enrolled entry |
| `/usr/local/libexec/mojo-acme-hub-enrollment.json` | root0644 | Nonsecret exact enrollment binding below |
| `/var/lib/django-mojo-deploy/update.lock` | root0600 | Existing deployment lock; never replace its inode while held |
| `/var/lib/django-mojo-deploy/acme-hub-floor.json` | root0600 | Atomic fsynced immutable floor written by prepare_hold |

Parents and files must be root-owned, have no group/other write, no symlinks,
and single-link regular files. Enrollment has exactly `schema:1`,
`helper_sha256`, `updater_sha256`, `app_user`, `project_root`, `artifact`.
The artifact is exactly epoch/source/manifest/release/wheel SHA as above.
The pinned app account must exist and have nonzero UID; project_root is a
canonical absolute path. The operator binds both retained byte hashes to the
reviewed release manifest and the serving project's actual account/root.
Enrollment is already a package/recovery floor before the activation stamp.

The default deployment argv first runs a data-only root selector with `-I -S`.
Enrollment **or** any partial floor selects the fixed helper `--launch` entry,
independent of database mode. Missing, unreadable, unsafe or mismatched enrolled
bytes refuse; they never fall back to a current-package locator. Enrolled
dispatch ignores a caller-configured alternate script. Only positively
unenrolled/no-floor nodes retain the legacy locator/custom behavior.

The launch entry accepts only exact SHA/framework/deployment/node-type pins
and optional migrate, then uses a sanitized fixed environment. It offers no
manual/latest-version route, arbitrary command, path or environment override.
The retained shell enters its existing bounded transient unit, acquires the
fixed update.lock and checks sealed data-only admission/recovery before any
current-package root import. The app account comes from enrollment as data.
The enrolled lane requires a local Linux filesystem with exclusive `flock`
supported on a read-only fd. It validates the existing root0600 single-link
regular lock before a no-create/no-truncate open, then verifies the actual
descriptor/inode and held lock. Missing/unsafe/unsupported lock state refuses;
enrollment must not replace the inode while held.
Unsafe ACTIVE recovery remains untouched. Saved metadata is checked before
restore, and actual installed bytes are checked again before retained
lifecycle. A qualifying ordinary rollback remains supported. Never edit an
ACTIVE journal or lower/delete a floor to recover.

## Operator quiescence and activation

1. Establish the exact converged app commit, reviewed wheel identity and full
   node roster, including HTTP, issuer/sweep/scheduler workers, cron/supervisor
   restart sources, transient root launchers and unmanaged writers. Enroll the
   retained entry while sharing is disabled. Eliminate surviving pre-admitted
   deployer threads/root launchers. A new setting cannot stop old processes.
2. Hold every request/worker/scheduler and persistent cron/supervisor restart
   source. Exclude replacement/ASG/bootstrap launches and stale deployment
   dispatch. Drain in-flight provider calls and establish their actual final
   Route53 outcome. A lost DB session alone does not cancel an admitted remote
   write; an unknown final outcome blocks activation. No provider mutation is
   performed by the coordinator.
3. Use local `manage.py acme_hub_upgrade status` and `plan` to inspect the
   singleton and bind the actual artifact, sorted authoritative roster, exact
   converged deployment UUID and full app SHA. Plan/status are read-only.
   Save the nonsecret plan under effective-root ownership0600 for activation.
4. Start local `activate` with that plan and a root-owned0600 attestation file.
   It takes the exclusive PostgreSQL admission barrier, generates a fresh
   nonce and waits for operator evidence/confirmation. Under that **same held
   session**, start one fixed helper process on each node. Send newline JSON
   `prepare_hold` with the exact floor and current nonce. The helper owns the
   real update.lock across frames. It checks idle transaction state and actual
   installed identity before durably stamping. Partial preparation never
   enables the database.
5. Keep those **same helper PIDs/sessions** alive; send `confirm_hold` frames
   with their returned helper_session and current nonce. Collect Linux boot ID,
   PID/start identity and fresh receipts. Heartbeat expires at30s and absolute
   session lifetime at600s; blocked input/output and EOF release the actual
   lock but never erase a stamp. One-shot repeated invocations are not proof
   of a held lock. No automated transport is implied.
6. The effective-root operator records the exact complete attestation shape
   accepted by `services/acme_hub_upgrade.py`, honestly classified
   `operator_attested`: exact artifact/deploy/roster/nonce, every node's same
   held helper and fresh confirmation, actual installed/updater/lifecycle
   identities, stopped loaded process identities, all quiescence/restart/
   launch holds, unmanaged-writer statement and known provider outcomes.
   The assertion is bounded/fresh and digest-confirmed locally. A JSON file
   alone is **not machine proof** of those operational facts. Unsupported or
   unestablished observations cannot be asserted true to pass a check.
7. With all holds maintained, activation validates the artifact and converged
   journal, binds existing owners/null leases without changing history, and
   commits enabled state atomically. Any failure rolls the DB transaction back.
   Retry a partial stamp with the **same activation UUID, artifact and roster**
   via the existing floor receipt, a fresh coordinator nonce and new same-PID
   helper sessions. Conflicting identity refuses; there is no reset/downshift.
8. Read back the enabled singleton and exact tuple before releasing helper
   sessions and restarting only qualified processes. Retain permanent floors
   and enrolled entry for subsequent deployment and interrupted recovery.
   Provider/process/root-session failures require operator investigation;
   source tests do not certify a live cutover.

Future launch coverage remains an actual prerequisite. Existing AMIs, user
data, launch templates and ASG replacements must not introduce old unfenced
writers. This change creates no new bootstrap enrollment distribution lane.
An enabled hub refuses framework capacity expansion with
`launch_coverage_unavailable`; an operator must qualify a separate supported
launch path before enabling it. Existing nonhub/compatibility capacity
convergence retains its existing durable-operation checks.

## Source verification

The serial rider roster is fixed in `tests/acme_hub_upgrade_tests.json`.
Run one framework tier and one all-tier serial union using that config; do not
run shared-database sessions concurrently. Local temporary-file/pipe and real
PostgreSQL harnesses verify failure boundaries. They do not enroll a host,
qualify a published wheel, prove maintenance holds or lift publication/live
holds. Direct-provider Domains, keys, certificates, CNAMEs and vhost references
remain outside this source change's operational authority.
