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
schema/version/permanent-name proof, and invokes neither the mutation
dispatcher nor its host lock. Mutation operations still require JobEngine
context and independently recheck readiness immediately before broker work.

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

## Deployment and rollback

Deployment retains the stdlib-only `firewall_deploy.py` alongside its previous
activation body. Candidate and previous activation reconverge enrollment
after activation. The retained N-1 wrapper also reconverges after its saved
body returns, so legacy MojoSec-off cleanup cannot leave an enrolled host's
broker removed. This preserves firewall authority without starting MojoSec.

`python3 -m mojo.deploy.check_node --sections firewall` reports independent
enrollment/assets and an actual status call as ec2-user. The jobs section
reports a missing firewall consumption channel.
