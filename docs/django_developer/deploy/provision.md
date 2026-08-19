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
asked for at the prompt and never written to disk on the operator's machine.

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
budgets against.

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
| `--skip-certificate` | `configure` | Converge the nodes, leave the placeholder certificate in place |
| `--ssh-user` | `configure`, `admin` | The account to reach the nodes as (default `ec2-user`) |
| `--identity` | `configure`, `admin` | Private key, when it is not one your agent already holds |
| `--email` | `admin` | The account to create (default: the file's `operator_email`) |
| `--list-resources` | `status` | Print the tag-scoped inventory |
| `--json` | `status` | Emit findings, steps and inventory as JSON |

Shared flags live on the **subcommands**: `apply --env staging`, not
`--env staging apply`.

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

**Stage 1** is `mojo/deploy/scripts/stage1.sh`, packaged in the wheel,
published to the config bucket with the version pin substituted, and logged to
`/var/log/mojo-stage1.log`. Its order is the whole point:

| # | Step | Why it is there and not elsewhere |
|---|---|---|
| 1 | untar the app tarball into `/opt/api` | `ec2_bootstrap.sh` and `ec2_deploy.sh` are files **inside** it |
| 2 | `aws/ec2_bootstrap.sh` | the OS: users, packages, nginx, certbot, and an **unpinned** `pip install django-mojo` |
| 3 | `pip install --upgrade "django-mojo==<version>"` | **after** step 2, precisely so it overwrites that unpinned install |
| 4 | `aws/ec2_deploy.sh` | the project: nginx vhosts, systemd units, `var/` ownership |
| 5 | `echo prod > var/profile`, chown `ec2-user:www`, chmod 640 | after step 4's ownership sweep, before step 7's restart |
| 6 | CloudWatch agent | installed if absent, configured, enabled — before the restart, so the app's first minutes are logged |
| 7 | `python3 -m mojo.deploy.config_sync` | last: it installs `django.conf` and restarts `mojo-asgi` |

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
