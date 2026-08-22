# Provisioning an AWS environment (`mojo.deploy.provision`)

One command takes an empty AWS account to a running django-mojo environment.
An operator answers eight questions once, commits the answers, and runs `apply`
until it stops finding work.

```bash
python3 -m mojo.deploy.provision init                # eight questions → one file
python3 -m mojo.deploy.provision apply --dry-run     # what it would build, priced
python3 -m mojo.deploy.provision apply               # build it
python3 -m mojo.deploy.provision configure           # config, nodes, HTTPS
python3 -m mojo.deploy.provision admin               # first superuser + login link
python3 -m mojo.deploy.provision status              # what is there now
```

`apply` builds the infrastructure; `configure` turns running instances into a
site that answers over HTTPS; `admin` gets you a login. `apply` is the one you
run more than once.

Everything is `python3 -m`, with no `[project.scripts]` console entry point and
no Django settings anywhere on the path — this runs from a laptop against an
account that has no django-mojo installed in it yet.

## Exact-resource brownfield fleets

The normal commands above own a greenfield topology. They are intentionally
unchanged. An existing application with a live database, cache, buckets,
network, DNS, certificates and public addresses uses a second input language
and two separate commands:

```bash
python3 -m mojo.deploy.provision fleet-status --fleet shadow
python3 -m mojo.deploy.provision fleet-status --fleet shadow --json
python3 -m mojo.deploy.provision fleet-apply --fleet shadow --dry-run
python3 -m mojo.deploy.provision fleet-apply --fleet shadow
```

The declaration is `aws/fleets/<fleet>.json`. It is strict, secret-free and
exact-reference only: unknown keys, unversioned boot objects, cross-account
ARNs, ambiguous node roles and malformed AWS identifiers fail before an AWS
client is built. It is not an environment file with extra switches and
`brownfield` is not a managed size preset.

Abridged only by replacing account-specific values with examples, the schema
is:

```json
{
  "schema_version": 1,
  "manage_dns": false,
  "account_id": "123456789012",
  "region": "us-west-2",
  "project": "maestro",
  "environment": "prod",
  "fleet": "shadow",
  "network": {
    "vpc_id": "vpc-0123456789abcdef0",
    "node_security_group_id": "sg-0123456789abcdef0",
    "public_subnets": [
      {"id": "subnet-0123456789abcdef0", "availability_zone": "us-west-2a", "network_border_group": "us-west-2"},
      {"id": "subnet-1123456789abcdef0", "availability_zone": "us-west-2b", "network_border_group": "us-west-2"}
    ]
  },
  "database": {
    "cluster_arn": "arn:aws:rds:us-west-2:123456789012:cluster:orchestra",
    "identifier": "orchestra",
    "writer_endpoint": "orchestra.cluster.example.rds.amazonaws.com",
    "reader_endpoint": "orchestra.cluster-ro.example.rds.amazonaws.com",
    "port": 5432,
    "database_name": "orchestra",
    "master_user": "postgres",
    "application_user": "maestro_app_next",
    "subnet_group_name": "orchestra-db",
    "security_group_ids": ["sg-1123456789abcdef0"],
    "credential": {
      "provider": "s3",
      "metadata_key": "application-user",
      "object": {"bucket": "maestro-prod-config", "key": "secrets/db.json", "version_id": "db-version", "sha256": "<64 lowercase hex>"}
    }
  },
  "cache": {
    "replication_group_arn": "arn:aws:elasticache:us-west-2:123456789012:replicationgroup:orchestra-cache",
    "identifier": "orchestra-cache",
    "endpoint": "orchestra-cache.example.cache.amazonaws.com",
    "port": 6379,
    "transit_encryption": true,
    "auth_enabled": false,
    "subnet_group_name": "orchestra-cache",
    "security_group_ids": ["sg-2123456789abcdef0"]
  },
  "storage": {
    "config": {"bucket": "maestro-prod-config", "prefix": "config/live"},
    "releases": {"bucket": "maestro-prod-releases", "prefix": "releases"},
    "sites": {"bucket": "maestro-prod-sites", "prefix": "sites"},
    "revisions": {"bucket": "maestro-prod-sites", "prefix": "revisions"},
    "fleet_config": {"bucket": "maestro-prod-config", "prefix": "fleets/shadow"}
  },
  "bootstrap": {
    "stage1": {"bucket": "maestro-prod-config", "key": "bootstrap/stage1.sh", "version_id": "stage1-version", "sha256": "<64 lowercase hex>"},
    "live_config": {"bucket": "maestro-prod-config", "key": "config/live/django.conf", "version_id": "config-version", "sha256": "<64 lowercase hex>"},
    "role_document": {"bucket": "maestro-prod-config", "key": "bootstrap/node-role.json", "version_id": "role-version", "sha256": "<64 lowercase hex>"}
  },
  "nodes": {
    "instance_type": "t3.medium",
    "volume_gb": 40,
    "ami_id": "ami-0123456789abcdef0",
    "key_pair_name": "maestro-prod",
    "session_manager": true,
    "items": [
      {"name": "maestro-api-1", "role": "api", "serving_target": true, "subnet_id": "subnet-0123456789abcdef0", "availability_zone": "us-west-2a", "instance_profile_arn": "arn:aws:iam::123456789012:instance-profile/maestro-api-fleet"},
      {"name": "maestro-worker-1", "role": "worker", "serving_target": false, "subnet_id": "subnet-1123456789abcdef0", "availability_zone": "us-west-2b", "instance_profile_arn": "arn:aws:iam::123456789012:instance-profile/maestro-worker-fleet"}
    ],
    "profiles": {
      "api": {"profile_arn": "arn:aws:iam::123456789012:instance-profile/maestro-api-fleet", "role_arn": "arn:aws:iam::123456789012:role/maestro-api-fleet"},
      "worker": {"managed": {"profile_name": "maestro-shadow-worker", "role_name": "maestro-shadow-worker"}}
    }
  },
  "load_balancer": {
    "name": "maestro-shadow-nlb",
    "api_target_group": "maestro-shadow-api",
    "certbot_target_group": "maestro-shadow-http",
    "subnet_ids": ["subnet-0123456789abcdef0", "subnet-1123456789abcdef0"]
  },
  "kms_key_arn": "arn:aws:kms:us-west-2:123456789012:key/01234567-89ab-cdef-0123-456789abcdef",
  "alarm_topic_arn": "arn:aws:sns:us-west-2:123456789012:maestro-alarms",
  "compatibility_instance_ids": ["i-0123456789abcdef0"]
}
```

`credential` is also required for cache when `auth_enabled` is true. A role
profile is either two exact existing ARNs or one migration-owned `managed`
name pair, never both. `compatibility_instance_ids` are explicit existing
servers that may temporarily join the shadow target groups; they are not
adopted as managed nodes.

For the database credential, `metadata_key` names the S3 user-metadata field
whose non-secret value must equal `database.application_user`; discovery
refuses a missing or different value without reading the object body. For an
authenticated cache credential the declared metadata key must likewise exist
with a non-empty proof value. This proves the declared credential identity,
not connectivity: the node-side `SELECT 1` and `PING` evidence remains a hard
pre-cutover gate.

### The brownfield safety boundary

`fleet-status` reads and validates the exact account, region, VPC, subnets and
routes, security groups, Aurora/Valkey shape and endpoints, S3 prefixes and
versioned object metadata, KMS key, SNS topic, IAM references, AMI, key pair,
existing declared nodes and migration telemetry. It hashes the redacted result
as the dependency digest and separately hashes the canonical complete action
set `(step, verb, target, detail)`. `fleet-apply` performs that preview, asks
for a typed confirmation, re-observes everything, and refuses before mutation
if either digest changed. A newly needed but otherwise allowed mutation is
therefore not smuggled in after confirmation.

The mutation boundary is a positive allowlist:

| Step | May create or converge | Never does |
|---|---|---|
| identity | only declared migration-owned roles/profiles and their exact runtime policy | modify an exact reused profile or adopt a colliding name |
| nodes | only the exact declared fleet node names, with fleet and application-role tags | stop, replace or modify a live/compatibility instance |
| balancer | the named shadow NLB, its two target groups/listeners, new NLB addresses, and target registration | deregister a target, attach a preserved address, or modify the live edge |
| telemetry | exact fleet log groups, retention and two target-health alarms | adopt or overwrite an untagged same-name group/alarm |

Every mutable resource must either be created in this run or re-observed with
the exact `managed-by`, project, environment, fleet and resource-role tags.
Same-name collisions fail closed. The SDK client independently rejects every
mutation method outside the allowlist, and the reviewed preview independently
checks an exact step/verb/resource-name matrix.

An existing owned node is eligible for target registration only when its
declared instance type, exact AMI, VPC, subnet, AZ, instance profile, sole
security group, running state, root-volume size and root-volume encryption all
match. Hardware or storage drift is blocking; a matching Name tag never makes
the node usable by itself.

Two preview rows deliberately cover subordinate calls as one logical resource
convergence: creating an instance profile includes attaching its newly created
owned role, and creating a log group includes setting its 90-day retention.
Those subordinate SDK methods have their own positive client allowlist entries
and run only after the parent create succeeded. They are not hidden mutations
on a second resource. Tests instrument apply and prove the complete logical
preview, including NLB attributes, listeners, address mappings and target
registration, covers every action apply can reach.

The following are forced false: DNS publication and Route53 changes; ACM or
certificate work; EIP transfer/association of preserved addresses; secrets
rotation; S3 data-plane publication/copy; database, cache, VPC or security
group creation/modification; and all teardown. The normal managed DAG and
commands are not called by fleet mode.

### Node boot and role behavior

Stage 0 downloads `stage1`, the existing live `django.conf`, and the opaque
node-role document by exact S3 version and verifies each declared SHA-256
before execution or installation. The live config is installed locally at
`/opt/api/var/django.conf`; it is never copied or republished in S3. Future
config-sync writes use the separate migration-owned `storage.fleet_config`
prefix. The role document is root-owned `0600`, and `MOJO_NODE_ROLE` plus
`mojo:application-role` carry the opaque application role through boot and
inventory. Only `serving_target` nodes, plus explicit compatibility instance
ids, enter the API target group; workers do not.

The managed runtime policy grants unversioned `GetObject` only below declared
storage prefixes and `GetObjectVersion` only on the exact bootstrap and
database/cache credential object keys. It has no credential values and no
permission to republish the existing data plane.

### DNS, TLS and preserved public IP continuity

Fleet preparation is deliberately a shadow operation. It does not change a
single Route53 record, bring-your-own-DNS record, ACM certificate, dnsman
object or existing public IP association. Existing sites continue resolving
to the existing servers throughout preparation. The shadow NLB gets new
addresses; transferring an existing EIP to it is a later, explicit cutover
procedure after backup, rehearsal, node readiness and canary evidence.

An existing single preserved EIP can maintain one public-IP continuity path;
it cannot by itself provide multi-AZ ingress redundancy. Do not represent one
preserved address as a redundant edge. DNS providers that can point at the NLB
name may move independently later; providers pinned to an IP need a rehearsed
EIP handoff. The handoff is a separate command and credential boundary; it
never changes DNS. `manage_dns` is required in every brownfield manifest and
must be the literal `false`. Missing is not treated as false.

### Preserved-EIP handoff and exact rollback

Add these fields only after the ordinary shadow fleet is two-AZ, deployed,
healthy, and serving every Host/SNI canary through its temporary addresses:

```json
{
  "manage_dns": false,
  "nlb_eip_allocations": {
    "us-west-2a": "eipalloc-0123456789abcdef0"
  },
  "eip_handoff_role_arn": "arn:aws:iam::123456789012:role/mojo-eip-handoff",
  "eip_handoff_canaries": [
    {
      "name": "maestro-api-version",
      "target": "nlb",
      "protocol": "https",
      "port": 443,
      "tls_sni": "maestromojo.com",
      "host": "maestromojo.com",
      "path": "/api/version",
      "expected_status": 200,
      "expected_marker": "version",
      "timeout": 5
    }
  ]
}
```

One or both selected AZs may be mapped. Allocation IDs are unique and every AZ
must be one of the exact two NLB subnets. At least one `target: nlb` canary is
mandatory; node-local canaries may be added but cannot replace proof through
the temporary public edge. One preserved allocation keeps that legacy fixed IP
but does **not** create two customer-known ingress addresses. Fixed-IP clients
remain effectively single-ingress/single-AZ until a later edge expansion.

Canary definitions stay in the validated in-memory fleet topology. Preview and
write-ahead journal documents bind their canonical SHA-256 digest and retain
only result summaries; they never serialize a raw canary request. Raw requests
containing authorization, cookies, bearer/token, password, or secret material
are rejected. Use a public probe or an out-of-band secret resolver instead.

With those fields, `fleet-apply` creates the shadow NLB in the normal two
subnets using AWS temporary public addresses. It does not allocate, tag,
attach, or adopt the preserved EIP. Ordinary managed and brownfield clients
reject `DisassociateAddress`, `ReleaseAddress`, and `SetSubnets`. Brownfield's
ordinary positive allowlist also rejects `AssociateAddress`; managed
greenfield stable-node EIPs keep their existing association behavior.

#### IAM uses two principals

Keep this explicit deny on the ordinary provisioning role (scope resources
more tightly where AWS supports it):

```json
{
  "Effect": "Deny",
  "Action": [
    "ec2:AssociateAddress",
    "ec2:DisassociateAddress",
    "ec2:ReleaseAddress",
    "elasticloadbalancing:SetSubnets"
  ],
  "Resource": "*"
}
```

The dedicated role trust policy admits only the operator/release role used for
cutover. Its policy allows provider reads, `SetSubnets` on the exact shadow
NLB and declared subnets, `DisassociateAddress`/`AssociateAddress` on the exact
elastic-IP and original network-interface ARNs, and `s3:GetObject`/
`s3:PutObject` plus bucket-versioning/encryption reads for the exact
`storage.fleet_config` handoff prefix. The EC2 statements also bind the
`ec2:AllocationId`, `ec2:NetworkInterfaceID`, and `ec2:Region` condition keys.
The command independently binds the association ID, private IP, and complete
request shape because those values are not all expressible as IAM conditions.
The Python guard is defense in depth; it is not a substitute for this IAM
boundary. Do **not** grant
`ec2:ReleaseAddress`, Route 53, ACM, ELB deletion, EC2 termination, tagging,
address allocation, S3 delete, or general provisioning permissions.

Representative dedicated-role policy shape (replace the placeholders with the
manifest's exact values):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Sid": "ReadExactHandoffEvidence", "Effect": "Allow", "Action": ["sts:GetCallerIdentity", "ec2:DescribeAddresses", "ec2:DescribeInstances", "ec2:DescribeNetworkInterfaces", "ec2:DescribeSubnets", "elasticloadbalancing:DescribeLoadBalancers", "elasticloadbalancing:DescribeLoadBalancerAttributes", "elasticloadbalancing:DescribeListeners", "elasticloadbalancing:DescribeTags", "elasticloadbalancing:DescribeTargetGroups", "elasticloadbalancing:DescribeTargetHealth"], "Resource": "*"},
    {"Sid": "ReadJournalBucketControls", "Effect": "Allow", "Action": ["s3:GetBucketVersioning", "s3:GetEncryptionConfiguration"], "Resource": "arn:aws:s3:::<fleet-config-bucket>"},
    {"Sid": "CASExactHandoffJournalPrefix", "Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"], "Resource": "arn:aws:s3:::<fleet-config-bucket>/<exact-handoff-prefix>/*"},
    {"Sid": "SetExactShadowNLBSubnets", "Effect": "Allow", "Action": "elasticloadbalancing:SetSubnets", "Resource": "<exact-nlb-arn>", "Condition": {"ForAllValues:StringEqualsIgnoreCase": {"elasticloadbalancing:Subnet": ["<exact-subnet-a>", "<exact-subnet-b>"]}}},
    {"Sid": "DisassociateExactSourceA", "Effect": "Allow", "Action": "ec2:DisassociateAddress", "Resource": ["arn:aws:ec2:<region>:<account>:elastic-ip/<exact-allocation-a>", "arn:aws:ec2:<region>:<account>:network-interface/<exact-source-eni-a>"], "Condition": {"StringEquals": {"ec2:AllocationId": "<exact-allocation-a>", "ec2:NetworkInterfaceID": "<exact-source-eni-a>", "ec2:Region": "<region>"}}},
    {"Sid": "RestoreExactSourceA", "Effect": "Allow", "Action": "ec2:AssociateAddress", "Resource": ["arn:aws:ec2:<region>:<account>:elastic-ip/<exact-allocation-a>", "arn:aws:ec2:<region>:<account>:network-interface/<exact-source-eni-a>"], "Condition": {"StringEquals": {"ec2:AllocationId": "<exact-allocation-a>", "ec2:NetworkInterfaceID": "<exact-source-eni-a>", "ec2:Region": "<region>"}}}
  ]
}
```

Repeat the two EC2 statements for each declared source EIP/ENI pair. Current
AWS IAM supports both resource types and those condition keys for both address
actions, but has no condition for `AssociationId` or `PrivateIpAddress`;
therefore the runtime exact-request guard remains mandatory. The
`ForAllValues` subnet condition rejects any subnet outside the declared pair
while still allowing the intentional one-subnet removal transition. Generate
the concrete policy from the immutable preview plan with
`handoff.cutover_role_policy(topology, plan)` and review it before creating the
role; this command does not create or modify IAM.

The command assumes that exact role a second time even when `--profile` or
`--role-arn` selects an ordinary source credential. Its refreshable session is
checked with STS for account, region and role before every live/recovery path.
The cutover client cannot construct a Route 53 client.

#### Preview, rehearsal, handoff, resume and rollback

```bash
python3 -m mojo.deploy.provision eip-handoff --fleet shadow --mode preview

python3 -m mojo.deploy.provision eip-handoff --fleet shadow --mode rehearse \
  --plan-digest <digest> --confirm '<printed REHEARSE phrase>'

python3 -m mojo.deploy.provision eip-handoff --fleet shadow --mode apply \
  --plan-digest <digest> --confirm '<printed HANDOFF phrase>'

python3 -m mojo.deploy.provision eip-handoff --fleet shadow --mode resume \
  --operation-id <uuid> --plan-digest <digest> \
  --confirm 'RESUME <uuid> <digest>'

python3 -m mojo.deploy.provision eip-rollback --fleet shadow --mode preview \
  --operation-id <uuid> --plan-digest <digest>

python3 -m mojo.deploy.provision eip-rollback --fleet shadow --mode apply \
  --operation-id <uuid> --plan-digest <digest> \
  --confirm 'ROLLBACK <uuid> <digest>'
```

`--dry-run` forces preview. Handoff/rollback have no `--yes`, `--nlb`, or
ordinary apply override. The 0600 plan binds account, region, role, manifest,
allocation/public IP/tags, original instance/ENI/private IP/subnet/AZ/VPC,
target NLB ARN, complete map and explicit map digests, listeners, target health, canary summaries,
exact NLB/target-group ownership-tag summaries, journal coordinates,
disruption and exact inverse. Every non-preview command
requires its exact digest and a distinct exact phrase.

A live handoff is authorized only by a terminal `rehearsed` lock for the exact
same plan digest. A failed rehearsal never authorizes handoff. Rehearsal may be
retried under a new operation ID and a freshly generated plan; only after that
retry reaches `rehearsed` may the same digest enter live handoff. This also
terminalizes caught local/S3 rehearsal failures as `rehearsal_failed`, so a
fixed rehearsal can retry without leaving the fleet locked.

The remote fleet-wide lock is acquired before any local write, so a losing
operator cannot overwrite active write-ahead intent. Each operation has its
own mode-`0600` local journal under `var/provision/eip-handoffs/`. Before a
provider mutation, every transition is flushed/fsynced locally and then
conditionally written under the versioned/encrypted fleet S3 prefix. The one
stable project/environment/fleet lock uses `If-None-Match: *`; it deliberately
does not depend on the allocation set, so overlapping `{A}` and `{A,B}` runs
cannot race. A terminal lock is reassigned only with `If-Match`, a new operation
ID, and proof that the new journal key is absent. The active lock contains the
full recovery seed, so a crash after lock creation but before local/remote
journal creation on a live handoff remains resumable. Active locks are never
silently stolen, including an active rehearsal after a hard process death; that
case requires explicit audited operator recovery. No lock or journal object is deleted;
terminal state is another conditional version.

Every `SetSubnets` call re-reads and compares the complete mapping. It retains
the untouched AZ, removes only the selected AZ, waits for removal, re-checks
the source association, disassociates it, waits until free, and adds the same
subnet with the preserved allocation. The transferred-IP canary must pass
before another allocation begins. Resume accepts only the two journaled sides
of a write-ahead transition and the handoff-direction lock. Unknown association
or map drift stops without guessing.

Rollback works from the same active partial operation or its completed lock.
It CAS-switches the lock direction, removes the preserved target mapping,
waits for the allocation to become free, associates the journaled ENI/private
IP with `AllowReassociation=False`, then adds the same NLB subnet without an
allocation ID so AWS supplies a temporary address. A failed replacement target
or canary never vetoes rollback; its gates are identity, lock/journal direction,
exact map classification and original-source restorability.

Removing an NLB subnet terminates that AZ's active connections and AWS says it
can take up to three minutes. Clients using the transferred IP can briefly fail
between source disassociation and NLB association. Every partial failure prints
both journal coordinates. Never manually remap or release the EIP while an
operation is active.

### Failure and recovery

- A manifest, dependency, ownership, preview-action or digest failure mutates
  nothing. Correct the declaration or restore the exact dependency and rerun
  `fleet-status`.
- A partial preparation run is resumable. Re-observation recognizes only
  exactly tagged resources, and the converge is idempotent.
- A node that boots without its pinned config/artifact digest matching never
  reaches stage 1. Inspect `/var/log/mojo-stage0.log` and replace the bad pin;
  do not bless different bytes under the old declaration.
- An unhealthy target is `MANUAL` and blocks cutover evidence; the command
  never deregisters or replaces it.
- There is no rollback-by-delete. Before public cutover, recovery is simply to
  leave production DNS/EIPs on the existing servers. After a separately run
  handoff, its runbook must be able to reattach the address or restore DNS to
  the recorded baseline.

Before any external cutover, retain the JSON `fleet-status`, manifest and
dependency digests, instance/AZ/profile inventory, cloud-init/stage logs,
redacted database `SELECT 1` and cache `PING` results from every role, target
health over the full canary window, DNS answers and TTLs, certificate/SAN
evidence, current EIP allocation/association and network-border-group details,
and a tested reverse procedure. Fleet preparation alone is not cutover
authorization.

## It takes about three `apply` runs, and that is the design

Aurora and ElastiCache take five to fifteen minutes to become usable, and this
package holds **no boto3 waiters** — a waiter would either hang the terminal or
need every poll stubbed in tests. So a fresh account converges in passes:

| Run | What happens |
|---|---|
| 1 | network, security groups, config bucket, secrets, key pair, node role, the Aurora cluster and the cache. Stops there: the database has no endpoint yet. |
| 2 | writes the stage-1 payload the booting node reads, and launches the EC2 instances. |
| 3 | attaches the balancer and registers the targets. |

`PENDING` and `SKIPPED` are **progress, not failure**. The exit code stays `0`,
and the summary names which steps are still coming up and tells you to run
`apply` again in a few minutes. There is no state file: resume is
re-observation, every run is safe to interrupt, and running against a converged
account creates nothing at all.

An instance is one of those slow things too, on a smaller scale. Attaching a
node's elastic IP seconds after `run_instances` gets `InvalidInstanceID` — "the
pending instance ... is not in a valid state for this operation" — and that is
reported as `PENDING` (`address.instance_not_ready`), not as a failure: the
address is allocated and reserved, and the next `apply` attaches it. Any other
association error is still `BLIND` and still fails the step.

## The eight questions

`init` needs no AWS credential — it is eight questions and a file.

| # | Question | Notes |
|---|---|---|
| 1 | **AWS profile or role ARN** | Blank uses the ambient chain. A profile name is the path for MFA and long runs. A value starting `arn:` is recorded as `role_arn`. |
| 2 | **Region** | Everything lands here. Moving regions later is a rebuild, not a setting. |
| 3 | **Project slug + environment slug** | Every AWS name is derived from these two. Short, lowercase, permanent — renaming builds a *second* environment beside the first. |
| 4 | **Apex domain** | What the certificate is issued for and what the A records go under, e.g. `example.com`. No scheme, no trailing dot. |
| 5 | **Operator email** | Two jobs: the ACME contact for certificate expiry, **and** the first superuser of the portal — the account you log in with. |
| 6 | **Size preset** | `micro` / `small` / `medium` / `large`, rendered live from `spec.PRESETS`. |
| 7 | **GitHub repository** | `owner/name`. The node clones it; nothing here needs a token. |
| 8 | **Emergency admin CIDRs** | Who may reach port 22. Defaults to *your* current egress address as a `/32`. |

Then five optional questions, every one **off by default**: backup retention
(7 days, or 35), an extra Aurora reader, an extra cache replica, creating the
Route53 hosted zone, and recording a staging environment.

Reader and replica are **additive on top of the preset** — `reader: true` means
"at least one", never "exactly one". Declining the opt-in never removes a reader
the preset asked for.

### The sizes, and which of them finish HTTPS for you

| Preset | Nodes | Database | Cache | Balancer |
|---|---|---|---|---|
| `micro` | 1 x t3.small | db.t4g.medium | cache.t4g.micro | none — HTTPS terminates on the single node |
| `small` | 2 x t3.medium | db.t4g.medium + 1 reader | cache.t4g.micro + 1 replica | NLB |
| `medium` | 4 x m6i.large | db.r6g.large + 1 reader | cache.t4g.medium + 1 replica | NLB |
| `large` | 6 x m6i.large | db.r6g.xlarge + 2 readers | cache.r7g.large + 2 replicas | NLB |

A preset with a balancer gets the `:80` certbot target group and both listeners
built for it, so the ACME HTTP-01 challenge lands on one predictable node and the
certificate finishes without anyone touching a box. `micro` answers `:80` and
`:443` on the node's own elastic IP instead.

`--nlb` forces a balancer onto a preset that would not build one. It is allowed
on `micro` and it is priced.

Growing later is a re-run of `apply` at the bigger preset — `micro` is not a dead
end.

### `admin_cidrs`, and the one thing it refuses quietly

Blank opens SSH to **nobody**, which is a working configuration (Session Manager
still reaches the box) and a far better accident than a world-open one. Every
entry must carry a prefix length: a bare `203.0.113.4` is rejected rather than
assumed to be a `/32`, because a rule that decides who reaches port 22 is not a
place to guess.

`0.0.0.0/0` is **allowed but never quiet**. It requires a second, separate typed
confirmation after being told exactly what it does. It is still permitted,
because refusing outright just moves the rule into the console where nobody
reviews it — but it is the single finding `mojo.deploy.check_setup` grades FAIL
on the accounts this tool builds.

## The environment file

`aws/environments/<env>.json`, committed to git, reviewed like code. It is the
declaration of what the environment **is**, and it is what makes "why does prod
look like this?" a question with an answer that does not depend on whose laptop
you ask.

```json
{
  "admin_cidrs": ["203.0.113.9/32"],
  "apex_domain": "example.com",
  "backups_days": 7,
  "env": "prod",
  "github_repo": "acme/demo",
  "operator_email": "ops@example.com",
  "preset": "small",
  "project": "demo",
  "reader": false,
  "region": "us-west-2",
  "replica": false,
  "route53_zone": false,
  "schema_version": 1,
  "staging": false
}
```

| Key | Meaning |
|---|---|
| `project`, `env` | The two slugs every AWS name is derived from |
| `region` | Where it all lands |
| `apex_domain` | Certificate subject and DNS parent |
| `operator_email` | ACME contact **and** first superuser |
| `preset` | `micro` / `small` / `medium` / `large` |
| `github_repo` | `owner/name` |
| `aws_profile` **or** `role_arn` | Which credential this environment is usually built with. Keep one; a CLI flag overrides it. |
| `admin_cidrs` | List of CIDR blocks allowed to reach `:22` |
| `backups_days` | `7` or `35` |
| `reader`, `replica` | Additive opt-ins on top of the preset |
| `nlb` | Force a balancer the preset would not build |
| `route53_zone` | Create the hosted zone if absent |
| `staging` | Recorded intent only — never provisions a second environment |
| `infrastructure_mode` | `managed` (default) or `external` — see below |
| `schema_version` | `1`. An unrecognized version refuses rather than guessing at the fields |

### No secrets, ever, enforced by an allowlist

`save()` writes the keys in that table **and refuses any other key outright** —
an allowlist, not a denylist of secret-shaped names. A denylist misses
`bootstrap_token` and false-positives on an honest future field like
`ssh_key_pair_name`; an allowlist makes adding a field a deliberate schema
change with a review moment attached.

Generated secrets — the database password, the Django secret key, the node's
private SSH key — live in **`bootstrap-secrets.json` in the config bucket**,
written by the provisioner and read back by the booting node. They are never
asked for at the prompt and never written into the environment file.

One of them does reach the operator's disk, deliberately: `configure` and
`admin` copy `ssh_private_key` to `~/.ssh/<project>-<env>.pem` at mode `0600`
so they can SSH to the nodes without the operator extracting it from that JSON
by hand — see [`--identity` below](#flags). The path is printed; the key itself
is never printed, logged, or put into a finding.

`init` over an existing file **prefills every answer and preserves keys this
version does not recognize**, so a file written by a newer django-mojo survives
a round-trip. Preservation is bounded to bytes that were already in that same
file — nothing typed at a prompt can enter the file under an unknown name.

Keys are sorted, indented two spaces, and end with one newline, so the diff is
stable and readable.

## `apply`, and the order of its gate

Every step can only make the run *less* likely to proceed. The AWS mutation is
the last thing that happens.

1. **`--override-external` together with `--yes` is refused** before anything is
   read. No file content makes that combination sensible.
2. **Load and validate the environment file.** Absent, unreadable, not JSON, an
   unknown `schema_version`, or a value AWS would reject (a bad slug, a
   32-character-plus target group name, a malformed CIDR) → exit `2`, path
   named, no traceback, and **no AWS call made**. The prompt check is not the
   only check: a hand-edited file is validated through exactly the same
   functions.
3. **External-mode gate** (below) → exit `3`.
4. **Build the clients and echo the account id, region, project-env and
   preset.** The account is named *before* the preview, because "wrong account"
   is the mistake that costs an afternoon and it is invisible in a plan that
   only lists resource names.
5. **`plan.observe()`** — read-only.
6. **The preview**: `N create · N modify · N leave` from the finding statuses,
   then the approximate monthly cost table. The `leave` count is the one that
   matters on a re-run — it is the evidence that a second `apply` creates
   nothing.
7. **`--dry-run` stops here, exit 0.** `plan.apply` is not reached on that path
   at all — structurally, not by every ensure function honouring an
   `apply=False` argument — and a test asserts it was never called.
8. **A literal typed `yes`.** Not `y`, not Enter. `--yes` skips it; a
   non-interactive stdin without `--yes` exits `2`, because there is nobody to
   confirm.
9. **`plan.apply()`**, then the findings and the summary.

Ctrl-C anywhere is one line and exit `130`. Re-run `apply` to resume; there is
nothing to clean up.

### The cost table

Approximate US on-demand list prices, before data transfer. It exists so a
number is on screen before the button is pressed — it is not a billing
integration and is not meant to be one. The **load balancer line appears exactly
when an NLB will exist after this run**, because an estimate listing a resource
the run will not build is worse than no estimate: it is the number someone
budgets against. The node-addresses line follows the same rule: it appears
without a balancer, or behind one when `stable_node_ips` pins per-node
outbound addresses.

## Flags

| Flag | Applies to | Meaning |
|---|---|---|
| `--env` | all | Which environment file (default `prod`) |
| `--project-root` | all | The directory holding `aws/environments/` (default `.`) |
| `--profile` | all | `~/.aws` profile. **The path for MFA and long runs.** |
| `--role-arn` | all | Assume this role once, no MFA, no refresh |
| `--dry-run` | `apply`, `configure`, `admin` | Observe and preview, then stop. On `configure` this is before the first byte is published and before the first SSH connection |
| `--yes` | `apply` | Skip the typed confirmation |
| `--override-external` | `apply` | One run against an `external` environment |
| `--nlb` | all | Build a balancer the preset would not |
| `--stable-node-ips` | all | Give every node its own elastic IP even behind a balancer — fixed outbound addresses for providers that allowlist caller IPs (env-file key: `stable_node_ips`). DNS still points at the NLB. The Admin capacity panel's "Stable outbound IPs" control is the runtime form of the same policy; a panel disable will be re-attached by the next `apply`, so change both or expect that |
| `--skip-certificate` | `configure` | Converge the nodes, leave the placeholder certificate in place |
| `--ssh-user` | `configure`, `admin` | The account to reach the nodes as (default `ec2-user`) |
| `--identity` | `configure`, `admin` | Private key to authenticate with. **Rarely needed** — see below |
| `--email` | `admin` | The account to create (default: the file's `operator_email`) |
| `--list-resources` | `status` | Print the tag-scoped inventory |
| `--json` | `status` | Emit findings, steps and inventory as JSON |

Shared flags live on the **subcommands**: `apply --env staging`, not
`--env staging apply`.

### `--identity` finds itself

When `apply` generates the key pair, AWS hands back the private key exactly
once and the provisioner stores it in `bootstrap-secrets.json`. `configure` and
`admin` already read that object while observing the account, so with no
`--identity` they write it to `~/.ssh/<project>-<env>.pem` at `0600` and use
it. Nothing has to be extracted by hand, and re-running rewrites nothing when
the file already matches.

Pass `--identity` when you want a specific key — it always wins, and nothing is
written anywhere. If the environment's key pair was **imported** (you supplied
`public_key`, so AWS never generated a private half), there is nothing to
materialize: the command says so and falls back to your SSH agent, exactly as
before.

### Credentials: `--profile` is the one to use

`--profile NAME` becomes `boto3.Session(profile_name=NAME)`. A profile in
`~/.aws/config` carrying `role_arn` + `mfa_serial` gets botocore's own MFA
prompt, its credential cache, and — the part that matters — **automatic
refresh**. A first `apply` can sit for ten minutes waiting on Aurora, and a
one-hour credential that renews itself is the difference between a resumable run
and a half-built VPC.

`--role-arn ARN` is one plain `sts:AssumeRole`: no MFA, no refresh. It is a
convenience for the common "assume the bootstrap role in the target account"
case, and the credential it mints **expires and is not renewed**. Use
`--profile` for anything long-running.

Neither flag uses the ambient chain. A flag always beats the value in the
environment file — the file records what the environment is usually built with,
the flag is this operator, right now.

Exactly one `sts:GetCallerIdentity` is made by the CLI itself, and the account id
it returns is what the preview header prints.

## External mode

An environment whose AWS estate is declared and applied by an external IaC
pipeline sets `"infrastructure_mode": "external"` in its file. `apply` then
refuses with **exit 3**, naming the mode, the field and the file path — creating
resources there is not merely unwanted, it is a change the next pipeline apply
will revert or replace.

`--override-external` runs once anyway. Three properties are deliberate:

- It **must be typed at a terminal**. Combining it with `--yes` is refused
  outright, because the file is a committed team declaration and one operator
  silently overriding it inside a pipeline is the actual risk.
- It prints a loud acknowledgement naming the file and the value.
- It **never modifies the file**. The override is a property of one invocation,
  never of the environment.

Parsing is fail-closed and matches `mojo/helpers/infrastructure.py` exactly:
absent, empty and `"managed"` are managed; `"external"` is external; **anything
else — a typo, a number, a bool — is external**. A switch whose whole job is to
refuse must not be turned off by a spelling mistake.

`mojo/deploy/` may not import `mojo.helpers.infrastructure` (it is under
`mojo.helpers`, which needs configured Django settings and does not have them
here), so the rule is duplicated in `inputs.py`. A test in
`tests/test_deploy/provision_cli.py` imports both in one process and asserts
they agree on the whole value table — a duplicated fail-closed rule is only safe
while something proves the copies have not drifted.

`inputs.infrastructure_mode(answers)` is the public accessor. The node
configuration step writes what it returns into `django.conf`'s
`INFRASTRUCTURE_MODE` **verbatim** — never a hardcoded `"managed"`. An
`--override-external` run still renders `external` if the file declares it.

## `status`

Observes and judges nothing it was not asked to. Findings are grouped by step in
the same format `mojo.deploy.check_setup` uses, so an operator moving between the
audit and the provisioner reads one layout.

`status --list-resources` prints the flat, **tag-scoped** inventory — kind,
name/id, and the ARN where AWS gives one. It is the input to a teardown
checklist, which matters because **nothing in this package deletes anything**:
the `Clients` proxy refuses `delete_*`, `terminate_*`, `deregister_*`,
`revoke_*` and `remove_*` at runtime. Tearing an environment down is a
deliberate human act performed elsewhere, and this listing is what tells you
what is there to tear down.

`status` exits non-zero on any `BLIND` finding. A report that shows a clean
section it was never allowed to read is worse than one that refuses to answer.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Nothing failed. **Includes the normal "half of it is still building".** |
| `1` | Something `FAILED`, was `BLOCKED` by a failure, or the credential was `BLIND` to it |
| `2` | The invocation or the environment file is wrong. Nothing was attempted. |
| `3` | External mode, and the run is refused |
| `130` | Ctrl-C |

## `configure` — from running instances to a portal you can log into

`apply` gets you instances. It does not get you a portal: the gap between
"EC2 says running" and "the site answers over HTTPS" is where cloud-init,
config, migrations, the settings profile and the certificate all live.
`configure` closes that gap.

```bash
python3 -m mojo.deploy.provision configure --env prod --dry-run
python3 -m mojo.deploy.provision configure --env prod
```

It does four things, in this order:

1. **Publishes `django.conf`** to `s3://<config bucket>/config/<project>/<env>/`
   with its sha256 in object metadata — which is exactly what `config_sync`
   reads back to verify what it downloaded.
2. **Converges every node over SSH**: `cloud-init status --wait` →
   `config_sync` → `migrate_locked` (node 0 only) → `systemctl enable --now
   mojo-asgi` → poll `http://127.0.0.1/api/version` → **read `var/profile`
   back**.
3. **Finishes HTTPS**, on a single-node environment only (see below).
4. **Adds the node's deploy key** to the GitHub repo, best effort — on failure
   it prints the public key and the settings URL rather than failing the run.

`--dry-run` stops before the first byte is published and before the first SSH
connection is opened. `--skip-certificate` converges the nodes and leaves the
node on its self-signed placeholder, for the run where DNS has not moved yet.

### Why `var/profile` is read back, and why a mismatch FAILS

`settings/helper.py` chooses the `local` profile unless `VAR_ROOT/profile`
exists. A node missing that file boots `settings.local`, connects to nothing
that was just provisioned — and **looks completely healthy**: nginx serves,
uvicorn runs, `/api/version` returns 200. It is the failure that costs an
afternoon, so `configure` reads the file back over SSH and fails loudly with
the remedy rather than reporting a converge that did not happen.

Stage 1 writes it between `ec2_deploy.sh` and `config_sync` — after the
former's `var/` ownership sweep, and before the latter's
`CONFIG_SYNC_RESTART` restart, which would otherwise bring the app up on the
wrong profile.

## Stage 0 and stage 1

A node provisions itself in two stages, and the split exists for one reason:
**user data cannot be edited on a running instance**, and `ec2_bootstrap.sh`
is about 20KB against EC2's 16KB user-data ceiling.

**Stage 0** is the instance's user data — around 1.4KB, with a 4KB budget
enforced in code. It sets the hostname, makes swap, writes
`/opt/api/var/bootstrap.conf` at 0600 (region, config bucket, config prefix,
`CONFIG_SYNC_OWNER=ec2-user:www`, `CONFIG_SYNC_RESTART=true`), then downloads
`stage1.sh` from the config bucket and execs it. Logged to
`/var/log/mojo-stage0.log`.

**No credential appears in user data.** It is readable from IMDS by anything
on the box and echoed back by `describe-instance-attribute` to anyone with EC2
read access. The node reads S3 with its instance role.

That role also carries one separate AMI-observation statement:
`ssm:GetParameter` on the architecture-specific public AL2023 parameter ARN.
The Admin node runs provisioning convergence with its instance role, so it
needs that read to resolve the current base image; no other SSM action or
parameter is granted.

**Stage 1** is `mojo/deploy/provision/scripts/stage1.sh`, packaged in the wheel,
published to the config bucket with the version pin substituted, and logged to
`/var/log/mojo-stage1.log`. Its order is the whole point:

| # | Step | Why it is there and not elsewhere |
|---|---|---|
| 1 | untar the app tarball into `/opt/api` | `ec2_bootstrap.sh` and `ec2_deploy.sh` are files **inside** it |
| 2 | `aws/ec2_bootstrap.sh` | the OS: users, packages, nginx, certbot, and an **unpinned** `pip install django-mojo` |
| 3 | `pip install --upgrade "django-mojo==<version>"`, converged with bounded retries (`--no-cache-dir` after the first attempt) | **after** step 2, precisely so it overwrites that unpinned install; a freshly published version can lag pip's Simple-index caches, and a fresh node has nothing to fall back on, so exhaustion stays fatal here |
| 4 | `python3 -m mojo.deploy render --dest /opt/api/var/deploy …` | materializes the JUST-PINNED package's cron/systemd templates before the project tries to converge them; a fresh node has no earlier post-deploy render to inherit |
| 5 | `aws/ec2_deploy.sh` | the project: nginx vhosts, systemd units, `var/` ownership |
| 6 | `echo prod > var/profile`, chown `ec2-user:www`, chmod 640 | after step 5's ownership sweep, before step 8's restart |
| 7 | CloudWatch agent | installed if absent, configured, enabled — before the restart, so the app's first minutes are logged |
| 8 | `python3 -m mojo.deploy.config_sync` | last: it installs `django.conf` and restarts `mojo-asgi` |

The whole script is `set -euo pipefail` and idempotent — a resumed bootstrap
is a plain re-execution of it. `tests/test_deploy/harness/test_stage1_sh.sh`
asserts every one of those orderings against the real packaged script.

### The application tarball, and the version pin

`apply` publishes three objects under `s3://<config bucket>/bootstrap/` before
it launches anything:

| Object | What |
|---|---|
| `stage1.sh` | the packaged script with `@DJANGO_MOJO_VERSION@` substituted |
| `app.tar.gz` | `git archive HEAD` of the project |
| `cloudwatch-agent.json` | the agent template with this environment's log-group names substituted |

A tarball rather than a clone, because a fresh node has no deploy key yet. A
**dirty worktree warns and does not block**: `git archive HEAD` ships the
commit either way, so uncommitted work simply is not in what the node runs —
worth saying, not worth refusing over.

Before any instance is launched, `apply` checks that the exact pinned version
exists on PyPI. A pin that does not exist otherwise fails at `pip install` on
a node that is already running and already billing, and the operator finds out
by reading `/var/log/mojo-stage1.log` over SSH. (A *yanked* release still
installs from an exact pin under PEP 592, so "published once" is the whole
question.) If PyPI cannot be reached at all, the run continues with a warning
— a network flake should not stand between an operator and their environment.
Stage 1 also refreshes the `django-mojo` Simple API entry when pip supports it,
so pip 26.2 cannot reuse PyPI's cached catalog response after this preflight.

### Where the logs go

Stage 1 points the CloudWatch agent at three groups, all created by `apply`
with 90-day retention, and all named from `spec.names()` so the agent config,
the groups and the node role's scoped `logs:*` grant cannot drift apart:

| Group | Files |
|---|---|
| `/mojo/<project>-<env>/nginx` | `/var/log/nginx/access.log`, `/var/log/nginx/error.log` |
| `/mojo/<project>-<env>/app` | `/opt/api/var/logs/*.log` — the app's `logit` output (`VAR_ROOT/logs`) |
| `/mojo/<project>-<env>/cloud-init` | `/var/log/cloud-init-output.log` |

The cloud-init group is the one that matters when a bootstrap goes wrong: it
is where a failed stage 1 is visible **without SSH**.

A failed `dnf install` warns and continues. Logging is not worth failing a
bootstrap over, and the next `configure` picks it up.

## The certificate sequence

Let's Encrypt allows **five failed authorizations per hostname per hour**. A
provisioning tool that reaches for certbot on every run burns that in one
afternoon and then cannot issue anything for the rest of the hour no matter
what the operator fixes. So the sequence is ordered to put every free check
first, and only its last two steps talk to ACME at all:

| # | Step | Costs against the rate limit? |
|---|---|---|
| 1 | Rewrite `server_name` in the `:443` vhost to the apex, `nginx -t`, reload | no |
| 2 | **Skip check**: certificate exists, not expiring within 7 days, **and** its SAN covers the apex | no |
| 3 | Apex resolves, and resolves to this node | no |
| 4 | Probe file round-trips through `http://<apex>/.well-known/acme-challenge/…` | no |
| 5 | `certbot certonly --nginx --dry-run` against **staging** | no — staging failures are free |
| 6 | The real `certbot --nginx … --redirect` | **yes** |
| 7 | `nginx -t`, reload, and verify `https://<apex>/api/version` answers 200 | no |
| 8 | On any ACME failure: copy naming the limit and what is safe to retry | — |

**If step 2 passes, certbot is skipped entirely** and the sequence stops at
step 7. That skip — not the staging dry run — is what makes a re-run safe.

### Two things that look like over-engineering and are not

Both come from the same fact: **`ec2_deploy.sh` does an unconditional `cp -f`
of the shipped nginx configs on every run.**

**Step 1 is unconditional.** Guarding it behind "only rewrite if it still says
`yourdomain.com`" looks like caution — it is not. Any operator edit to
`app.conf` was already destroyed by that `cp -f` one step earlier, so the
guard protects nothing; and on a resumed node the placeholder is always back,
so the guard would fire exactly when it should not.

**The skip branch still rewrites the certificate paths.** That same `cp -f`
reset `ssl_certificate` and `ssl_certificate_key` to the snakeoil placeholder.
Skipping certbot without re-pointing them leaves a resumed node serving a
**self-signed certificate with a perfectly good Let's Encrypt one sitting
unused on disk** — which reads as a certificate failure and is not one.

And the skip check is expiry **and** a SAN match, not expiry alone: an
operator who changed `apex_domain` between runs holds a completely unexpired
certificate for the old name.

### Multi-node: certificates are not issued here

Any environment with more than one node, or with an NLB, prints the
dnsman/edge hand-off instead of attempting anything. `certbot --nginx`
rewrites `app.conf` with an `include /etc/letsencrypt/options-ssl-nginx.conf`
line, and that file exists only on the node certbot ran on — so the moment the
mutated config reaches a second node, `nginx -t` fails there and that node
serves nothing.

A fleet's certificates belong to dnsman (DNS-01, keys in KMS) and
`mojo.apps.edge`, which renders a vhost per domain into a generation under
`/opt/api/var/edge` and installs it fleet-wide through the one-line
`conf.d/mojo.conf` include.

> **Growing micro → small.** When a single node that already holds a
> certbot-issued certificate becomes a fleet, **do not copy the
> certbot-mutated `app.conf` to the new nodes** — it will fail `nginx -t`
> there. Move the certificate to the edge plane: issue it in dnsman, enable
> `EDGE_CONVERGE_ENABLED`, and let the edge plane install it on every node.
> The old node keeps working throughout; its `app.conf` is replaced by the
> next `ec2_deploy.sh` anyway.

## `admin` — the first superuser

```bash
python3 -m mojo.deploy.provision admin --env prod
```

Runs `create_user --email <operator email> --superuser --login-link` on node 0
and prints the link it produced. The account is created with a random 18-
character password that is **generated, set, and discarded unread** — nobody
ever sees it, which is why the printed link is a password *reset* link: the
operator chooses their own password rather than being handed one that lived in
a terminal scrollback. The link is single use and expires in one hour.

The link works on its **first** click even for a visitor with no session — see
[../account/auth_pages.md](../account/auth_pages.md) for the challenge-page
token hand-off that makes that true.

A re-run against an address that already exists prints re-issue guidance
rather than a traceback: the account is there, and what you actually want is
another link for it.

After logging in, `aws_s3` / `aws_email` / `aws_monitoring` show as unresolved
in System Setup. That is expected: those are portal-side integrations, not
part of what `apply` builds.

## Where the code lives

| Module | What it is |
|---|---|
| `mojo/deploy/provision/inputs.py` | The eight questions, the env file, `infrastructure_mode()` |
| `mojo/deploy/provision/clients.py` | The boto3 session factory — the one file that decides which credential a run uses |
| `mojo/deploy/provision/__main__.py` | `init` / `apply` / `configure` / `admin` / `status`. Prompts, previews, prices, confirms, renders — and creates no AWS resource itself |
| `mojo/deploy/provision/spec.py` | The topology as data: presets, derived names, tags, validation, costs |
| `mojo/deploy/provision/plan.py` | The DAG, and `observe()` / `apply()` |
| `mojo/deploy/provision/nodes.py` | The instances, their addresses, and the stage-0 user data |
| `mojo/deploy/provision/storage.py` | The config bucket, the secrets, and the boot payload (`stage1.sh`, the tarball, the agent config) |
| `mojo/deploy/provision/render.py` | Builds and publishes `django.conf`. **Not** `python3 -m mojo.deploy render`, which materializes cron/systemd templates on a node — same verb, opposite direction |
| `mojo/deploy/provision/remote.py` | The SSH driver: cloud-init → config_sync → migrate → service → probe → profile read-back |
| `mojo/deploy/provision/certificate.py` | The eight-step certificate sequence, and the fleet hand-off text |

Every AWS mutation belongs to `plan.apply()`. That separation is what lets the
portal offer the same provisioning without reimplementing the gate.
