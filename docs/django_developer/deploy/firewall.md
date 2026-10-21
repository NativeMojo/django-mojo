# Firewall enrollment and fleet truth

Firewall brokering is installation infrastructure. It works with MojoSec off
or absent; enabling a host sensor is never a firewall precondition. No lifecycle
command edits SSH rules or flushes kernel firewall state.

## Enroll a host

Run as root using the system Python that holds the installed framework:

```bash
/usr/bin/python3 -E -P -m mojo.deploy.firewall_deploy enroll --permanent-set mojo_blocked
/usr/bin/python3 -E -P -m mojo.deploy.firewall_deploy check
/usr/bin/python3 -E -P -m mojo.deploy.firewall_deploy converge
/usr/bin/python3 -E -P -m mojo.deploy.firewall_deploy off
```

`enroll` records participation in root-owned, mode-0600
`/etc/mojo-firewall.json`. `/etc/mojo-firewall-broker.json` is the only root
authority for the permanent set name. Re-enrollment refuses a different name;
changing that namespace requires a separate operator migration. The app's
file-only `FIREWALL_BLOCKED_IPSET_NAME` must match it.

Enrollment and convergence install the root-owned broker wrapper (0755) and
the exact empty-argument ec2-user sudo grant (0440), validating candidate
sudoers before activation. A nonblocking lifecycle lock serializes changes;
failed writes restore the previous files. Paths reject symlinks, unsafe
ownership/modes, and oversized files. `converge` repairs an enrolled host and
does not enroll a new one. `off` removes the grant and wrapper and records
disabled enrollment; permanent set identity and existing kernel rules remain.
The wrapper is a direct `/usr/bin/python3 -EP` script rather than a shell
trampoline. That preserves safe-path and environment isolation while producing
one canonical broker exec generation for MojoSec receipt correlation.

## Declare the complete fleet

In the project's settings file on every participating node:

```python
FIREWALL_EXPECTED_HOSTS = ["api-1", "api-2", "sites-1", "sites-2"]
FIREWALL_BLOCKED_IPSET_NAME = "mojo_blocked"
```

Use exact lowercase heartbeat hostnames. The list must contain 1–128 unique
valid hostnames. Missing, empty, malformed, or duplicate membership makes fleet
truth unavailable. A live heartbeat never defines or shrinks the expected
fleet. Changing membership is an explicit deployment configuration change.

The normal API runner consumes `firewall` through `DEFAULT_CHANNELS`. Projects
that replace `JOBS_CHANNELS` must add `firewall` and retain
`JOBS_HOSTNAME_CHANNEL=True`. A participating Sites host also needs a live
JobEngine: its dedicated deployment engine must consume `firewall` and its own
box-direct channel, load the incident capability provider, and run as ec2-user.
Enrollment alone does not start that runner. Enrolled API
candidates validate these channel and membership contracts before activation.

## Readiness and reconciliation

The only context-free broker operation is stdin
`{"operation":"broker.status"}` sent to exactly
`sudo -n -- /usr/local/sbin/mojo-firewall-broker` with no broker arguments.
It validates the sudo caller and protected enrollment/assets, returns a bounded
schema/version/permanent-name proof, and emits a root-authored begin/result
receipt pair for its own exact PID generation. Both receipts have no firewall
children; the operation invokes neither the mutation dispatcher nor its host
lock. MojoSec suppresses the corresponding sudo observation only when the pair
and PID generation agree with exact cron JobEngine → sudo → broker lineage and
the pinned engine origin. A failed status result or any incomplete proof remains
an ordinary sudo Event. Mutation operations still require JobEngine context and
independently recheck readiness immediately before broker work.
On AL2023, Audit reports the resolved `/usr/sbin/xtables-nft-multi` or
`/usr/sbin/ipset` executable behind the broker's fixed `/sbin/*` command. Proof
accepts only those fixed pairs and still requires the exact recorded argv digest.

The incident provider verifies the process's effective UID, the exact sudo
operation, timeout, response shape, and permanent-name agreement. A background
runner thread refreshes this proof; heartbeats only read its expiring cache.
Only ready engines consuming firewall plus their direct channel advertise
`firewall_reconcile: 1`. `execute_checked` retains its generic jobs meaning.

Firewall host selection joins capable heartbeats to the configured expected
list and preserves expected, selected, and unavailable hosts separately. A
healthy subset can repair itself while an unavailable expected host prevents
aggregate truth from finalizing. Startup/recovered-readiness work is idempotent
per runner incarnation; hourly repair selects one capable runner per host.
Aggregate jobs coalesce by desired generation; their marker stays held through
retryable attempts and is released on success, terminal failure, or exhausted
retries (its expiry bounds recovery after a lost worker). Aggregation reads
fleet evidence and updates database truth, so any firewall-channel consumer
can run it without local broker authority. It still requires the complete
ready expected roster, and capability loss during publication invalidates the
published success before retrying. Local kernel mutations retain their broker
readiness checks. Structural failures such as a
wrong account or malformed/missing broker fail terminally; transient timeouts
and host contention use durable retry. A later readiness recovery queues a
fresh repair.

Public firewall truth APIs accept only `channel="firewall"`; passing another
channel, including the former `"default"`, explicitly fails with
`invalid_firewall_channel` before dispatch. Omit the argument for the default.

Disabled historical IPSet rows produce an absence tombstone after validating
their safe name; their old IPv6 or malformed member lists are not loaded.
Present sets still require canonical IPv4 data. Model/save validation is
unchanged.

## Target safety

No framework path may put a DROP for an unsafe address into the kernel. One
predicate, `mojo/apps/incident/services/block_safety.py`, decides what is
unsafe, and it runs at three boundaries:

| Boundary | Where | What happens to an unsafe target |
|---|---|---|
| Desired-state write | `GeoLocatedIP._block_checked_locked`, reached by `block`, `block_checked`, the REST `block` action, threat escalation, the `block://` handler, the LLM and assistant tools and MojoSec execution | Refused with its code: block state unchanged (`is_blocked`, `blocked_until`, generation, `block_count`, firewall fields), no fence, no push-back from the block, no dispatch |
| Desired-state build | `firewall_truth.permanent_snapshot`, `geolocated_snapshot_from_row`, `ipset_snapshot_from_row` | A blocked row is planned absent and flagged `firewall_sync_error="unsafe_target: <code>"`, keeping `is_blocked` so an operator can see and unblock it. A verified sync keeps that marker (only `firewall_pending` clears), so it stays visible until the row is unblocked or the target is safe again. A set keeps only its safe networks |
| Dispatch | every present-direction call in `mojo/apps/incident/firewall.py`, the only caller of the root broker | Refused with `{"ok": false, "error": {"code": ...}}` and no broker request. It never repairs. Removals are never refused: unblocks, set removals and an empty network list always dispatch |

Unsafe, with the refusal code, checked in this order:

| Code | Meaning |
|---|---|
| `invalid_network` | Not an address |
| `unsupported_family` | IPv6 (unchanged) |
| `non_global_address` | In the pinned table `NON_GLOBAL_V4`: 0/8, 10/8, 100.64/10, 127/8, 169.254/16, 172.16/12, 192.0.0/24, 192.0.2/24, 192.168/16, 198.18/15, 198.51.100/24, 203.0.113/24, 224/4 and 240/4. It is pinned because `ipaddress.is_global` differs between Python versions |
| `protected_config_invalid` | `MOJOSEC_PROTECTED_CIDRS` or `MOJOSEC_TRUSTED_PROXY_CIDRS` is malformed |
| `mojosec_protected_cidrs` | Inside `MOJOSEC_PROTECTED_CIDRS` |
| `mojosec_trusted_proxy_cidrs` | Inside `MOJOSEC_TRUSTED_PROXY_CIDRS` |
| `whitelisted` | An active whitelist |

Both CIDR settings are file-only and take a list or a comma-separated string of
at most 64 IPv4 CIDRs; unset or empty means none. Anything else refuses every
new block, logged with the setting's name. While it is malformed:

- every present request refuses with `protected_config_invalid`: `block`,
  `ipset_add`, `ipset_load`, `normalize_ip` and `normalize_ipset` with members,
  and a geolocated request whose temporary source is present;
- removals still dispatch, so an unblock never stalls on a config error;
- the permanent aggregate's list is narrowed, not refused: it is checked
  against `NON_GLOBAL_V4` and active whitelists only, as the desired-state
  builder does. New blocks are already refused at the write, so the list holds
  only rows written while the setting was valid, or legacy kernel state;
- sync does not tear existing kernel state down because of the setting: what is
  enforced stays as it is until the setting is fixed.

**Sets are carved, not refused.** Operator, country and feed sets lose the
unsafe ranges and every actively whitelisted address before they reach the
kernel; the rest of the set still blocks. The stored `IPSet.data` keeps the
operator's list, and the planned count and digest describe what is kept, so
verification stays truthful. `sync_firewall` reports each carved set once per
sync: one job-log line, one `mojo.log` line and one `firewall:ipset_carved`
metric. One that would leave more than 250,000 networks refuses with
`network_limit` rather than truncating.

**Whitelists are repaired eventually, not atomically.** A whitelist visible when
a block is written, or when dispatch re-reads desired state, prevents the
block. An address whitelisted after that read can stay in a kernel set until
the sync `whitelist()` publishes, or the hourly sync, succeeds. `whitelist()`
and `unwhitelist()` publish that sync so the carve-out follows promptly;
whitelist expiry is picked up by the hourly sync, and the stale window errs
toward not blocking.

Upgrading changes enforcement in two ways:

- Blocked rows for non-global, protected or proxy addresses stop being enforced
  at the next sync and are flagged `unsafe_target`.
- Sets stop matching protected ranges and actively whitelisted addresses. For
  example, a whitelisted partner address in a blocked country is no longer
  dropped by the country set.

## Deployment and rollback

Deployment retains the stdlib-only `firewall_deploy.py` alongside its previous
activation body. Candidate and previous activation reconverge enrollment
after activation. The retained N-1 wrapper also reconverges after its saved
body returns, so legacy MojoSec-off cleanup cannot leave an enrolled host's
broker removed. This preserves firewall authority without starting MojoSec.

`python3 -m mojo.deploy.check_node --sections firewall` reports independent
enrollment/assets and an actual status call as ec2-user. The jobs section
reports a missing firewall consumption channel.
