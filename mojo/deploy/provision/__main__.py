"""`python3 -m mojo.deploy.provision` — the command an operator actually types.

    python3 -m mojo.deploy.provision init            # eight questions, one file
    python3 -m mojo.deploy.provision apply --dry-run # what it would build, priced
    python3 -m mojo.deploy.provision apply           # build it
    python3 -m mojo.deploy.provision configure       # config, nodes, HTTPS
    python3 -m mojo.deploy.provision admin           # first superuser + link
    python3 -m mojo.deploy.provision status          # what is there now

THIS FILE CREATES NO AWS RESOURCE. Every AWS mutation belongs to `plan.apply()`;
this is prompting, a preview, a confirmation and rendering. That separation is
what lets the portal offer the same provisioning without reimplementing the gate
— and it is why `--dry-run` is safe by construction rather than by discipline:
on that path `plan.apply` is not reached at all, and a test asserts it.

`configure` and `admin` are the two commands that reach PAST AWS — they publish
one S3 object and then drive already-running nodes over SSH. They still create
no infrastructure: a node that does not exist is reported, never launched, and
`--dry-run` stops both before the first byte is written and before the first
SSH connection is opened.

A FRESH ACCOUNT TAKES ABOUT THREE RUNS, AND THAT IS THE DESIGN. Aurora and
ElastiCache take five to fifteen minutes to become usable, and this package
holds no waiters (a waiter would either hang the terminal or need every poll
stubbed). So the first `apply` builds the network, the bucket, the secrets, the
database and the cache and stops; the second, ten minutes later, writes the
stage-1 payload and launches the nodes; the third attaches the balancer. PENDING
and SKIPPED are therefore rendered as PROGRESS, never as failure, and the exit
code stays 0 — a bootstrap that told an operator it had broken when the correct
advice is "run it again in ten minutes" would be worse than useless.

EXIT CODES

    0    nothing failed. Includes the very normal "half of it is still building".
    1    something FAILED, was BLOCKED by a failure, or the credential was BLIND
         to it. A BLIND step is a failure, not a warning: a converge that
         reports a clean section it was never allowed to read is the fail-open
         case this whole package is arranged against.
    2    the invocation or the environment file is wrong. Nothing was attempted.
    3    the environment file declares `infrastructure_mode: external` and this
         run is refused.
    130  Ctrl-C. Re-run `apply` to resume; there is no state file to clean up.

EVERY MODULE-LEVEL STATEMENT HERE IS AN IMPORT, A CONSTANT OR A `def`. Nothing
executes on import: no argparse construction, no prompting, no network. The
package's import-isolation walk imports modules under `mojo/deploy/` to prove
they work with no Django settings configured, and a module that printed or
prompted at import time would fire during that check.
"""

import sys

from mojo.deploy.provision import certificate as certificate_module
from mojo.deploy.provision import clients as clients_module
from mojo.deploy.provision import (discover, inputs, plan, remote, render,
                                   report, storage)
from mojo.deploy.provision import spec as spec_module


EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2
EXIT_EXTERNAL = 3
EXIT_INTERRUPTED = 130

# Same column width as `check_setup.Report.render`, so an operator moving
# between the audit and the provisioner reads one format.
MARKS = {
    report.PASS: "  ok  ",
    report.PENDING: " wait ",
    report.MISSING: " make ",
    report.DRIFT: " edit ",
    report.MANUAL: " MANUAL",
    report.BLOCKED: " BLOCK",
    report.BLIND: " BLIND",
}

# What the preview calls each status, in the counts line. `leave` is the number
# that matters most on a re-run: it is the evidence that a second `apply`
# creates nothing.
PREVIEW_LABELS = (
    ("create", report.MISSING),
    ("modify", report.DRIFT),
    ("leave", report.PASS),
    ("waiting on AWS", report.PENDING),
    ("needs a human", report.MANUAL),
    ("could not see", report.BLIND),
)


# ── argument parsing ────────────────────────────────────────────────────────

def build_parser():
    import argparse

    # The shared flags live on the SUBCOMMANDS, not on the top-level parser.
    # Declared in both places, argparse lets the subparser's defaults overwrite
    # a value already parsed at the top level, so `--env staging apply` would
    # silently act on prod. Declared only at the top, `apply --env staging`
    # — the order everyone types — is a usage error. One place, on the
    # subcommand, is the only arrangement with no trap in it.
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "--env", default="prod",
        help="which environment file to use (default: prod), i.e. "
             "aws/environments/<env>.json")
    shared.add_argument(
        "--project-root", default=".",
        help="the project directory holding aws/environments/ (default: .)")
    shared.add_argument(
        "--profile",
        help="~/.aws profile to use. THE PATH FOR MFA AND LONG RUNS: a profile "
             "carrying role_arn + mfa_serial gets botocore's own prompt, cache "
             "and automatic credential refresh")
    shared.add_argument(
        "--role-arn",
        help="assume this role once, without MFA, and do not refresh it. A "
             "convenience — use --profile if the run might outlive an hour")
    shared.add_argument(
        "--dry-run", action="store_true",
        help="observe and preview, then stop. Nothing is created")
    shared.add_argument(
        "--yes", action="store_true",
        help="skip the typed confirmation (for a scripted run)")
    shared.add_argument(
        "--override-external", action="store_true",
        help="run once against an environment whose file declares "
             "infrastructure_mode: external. Must be confirmed at a terminal — "
             "it cannot be combined with --yes — and never modifies the file")
    shared.add_argument(
        "--nlb", action="store_true",
        help="build a network load balancer even when the size would not")

    parser = argparse.ArgumentParser(
        prog="python3 -m mojo.deploy.provision",
        description="Provision one AWS environment for django-mojo, "
                    "idempotently. Creates and modifies; never deletes.")
    commands = parser.add_subparsers(
        dest="command", required=True,
        metavar="{init,apply,configure,admin,status}")
    commands.add_parser(
        "init", parents=[shared],
        help="ask the eight questions and write the environment file")
    commands.add_parser(
        "apply", parents=[shared],
        help="show what would change, confirm, then converge")
    configure = commands.add_parser(
        "configure", parents=[shared],
        help="publish django.conf, converge every node over SSH, and finish "
             "HTTPS on a single-node environment")
    configure.add_argument(
        "--skip-certificate", action="store_true",
        help="converge the nodes but do not touch certbot. For a run where "
             "DNS has not moved yet")
    configure.add_argument(
        "--ssh-user", default=remote.SSH_USER,
        help=f"the account to reach the nodes as (default: {remote.SSH_USER})")
    configure.add_argument(
        "--identity",
        help="private key to authenticate with. Not normally needed: the key "
             "this tool generated is fetched from the environment's secrets "
             "object and written to ~/.ssh/<project>-<env>.pem automatically")
    admin = commands.add_parser(
        "admin", parents=[shared],
        help="create the first superuser and print a single-use login link")
    admin.add_argument(
        "--email",
        help="the account to create (default: the environment file's "
             "operator_email)")
    admin.add_argument(
        "--ssh-user", default=remote.SSH_USER,
        help=f"the account to reach the node as (default: {remote.SSH_USER})")
    admin.add_argument(
        "--identity",
        help="private key to authenticate with. Not normally needed — see "
             "`configure --help`")
    status = commands.add_parser(
        "status", parents=[shared],
        help="observe the account and report, changing nothing")
    status.add_argument(
        "--list-resources", action="store_true",
        help="print the tag-scoped inventory of what this environment owns")
    status.add_argument(
        "--json", action="store_true",
        help="emit findings, steps and inventory as JSON")
    return parser


def main(argv, console=None):
    """The whole program. Returns an exit code; never calls `sys.exit`.

    `console` is the reader/writer seam. Left alone it is the real terminal; a
    test hands in a scripted one and drives the entire command in-process,
    which is how the prompt flow, the confirmation and the refusals are covered
    without patching `builtins.input` — a process-global that leaks into
    whatever else the runner is doing in the same interpreter.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    console = console or inputs.Console()

    try:
        if args.command == "init":
            return run_init(args, console)
        if args.command == "apply":
            return run_apply(args, console)
        if args.command == "configure":
            return run_configure(args, console)
        if args.command == "admin":
            return run_admin(args, console)
        return run_status(args, console)
    except KeyboardInterrupt:
        print("\ninterrupted — re-run `apply` to pick up where this stopped; "
              "there is no state file to clean up", file=sys.stderr)
        return EXIT_INTERRUPTED
    except inputs.EnvFileError as err:
        print(f"error: {err}", file=sys.stderr)
        return EXIT_USAGE
    except clients_module.CredentialError as err:
        print(f"error: {err}", file=sys.stderr)
        return EXIT_USAGE


# ── init ────────────────────────────────────────────────────────────────────

def run_init(args, console):
    """Ask, validate, write, and say what to run next.

    Needs no AWS credential at all: it is eight questions and a file. Wanting
    the credential only at `apply` time means an operator can write the
    declaration on a plane.
    """
    import os

    path = inputs.env_path(args.project_root, args.env)
    preserved, prefill = {}, {"env": args.env}
    if os.path.exists(path):
        existing = inputs.load(path)
        preserved = inputs.unrecognized(existing)
        prefill = inputs.schema_only(existing)
        prefill.setdefault("env", args.env)
        console.say(f"reading {path} — press Enter to keep each answer")
        if preserved:
            console.say(f"  carrying through {', '.join(sorted(preserved))}, "
                        f"which this version does not recognize")

    if not console.is_interactive():
        remaining = inputs.problems(prefill)
        if remaining:
            _print_problems(
                f"{path} is incomplete and stdin is not a terminal", remaining)
            return EXIT_USAGE
        console.say(f"{path} already answers everything — nothing to ask")
        return EXIT_OK

    answers = inputs.ask(inputs.PROMPTS, answers=prefill, console=console)
    answers = inputs.ask(inputs.OPTIONAL_PROMPTS, answers=answers,
                         console=console)
    if args.nlb:
        answers["nlb"] = True

    remaining = inputs.problems(answers)
    if remaining:
        _print_problems("these answers cannot be used", remaining)
        return EXIT_USAGE

    target = inputs.env_path(args.project_root, answers["env"])
    # Unrecognized keys are carried only back into the file they came from. An
    # operator who answered a different env slug is writing a NEW environment,
    # and inheriting another environment's unknown fields into it would be an
    # invention, not a preservation.
    carry = preserved if os.path.abspath(target) == os.path.abspath(path) else {}
    inputs.save(target, inputs.schema_only(answers), preserved=carry)

    console.say("")
    console.say(f"wrote {target}")
    console.say("  commit it — this file is the declaration of what this "
                "environment is, and it holds no secrets")
    console.say("")
    console.say("next:")
    console.say(f"  python3 -m mojo.deploy.provision apply "
                f"--env {answers['env']} --dry-run")
    if answers.get("staging"):
        console.say("")
        console.say("staging was recorded but NOT provisioned — one apply "
                    "builds one environment. When you want it:")
        console.say("  python3 -m mojo.deploy.provision init --env staging")
    return EXIT_OK


# ── status ──────────────────────────────────────────────────────────────────

def run_status(args, console):
    answers = _answers(args)
    topology = _topology(args, answers)
    connection = _connect(args, answers, topology, console,
                          announce=not args.json)

    findings, actions, run = plan.observe(connection, topology)
    rows = inventory(run.observed, topology)

    if args.json:
        summary = report.Report(findings, actions)
        import json
        console.say(json.dumps({
            "account_id": run.observed.get("account_id"),
            "project": topology.project, "env": topology.env,
            "region": topology.region, "preset": topology.preset,
            "steps": {name: dict(step) for name, step in run.steps.items()},
            "resources": rows,
            **summary.as_dict(),
        }, indent=2, default=str))
    else:
        render_findings(findings, console.say)
        render_summary(findings, run, console.say)
        if args.list_resources:
            render_inventory(rows, console.say)

    return EXIT_FINDINGS if run.blocking else EXIT_OK


# ── apply ───────────────────────────────────────────────────────────────────

def run_apply(args, console):
    """The ordered gate. Every step below can only make the run LESS likely to
    proceed, and the AWS call is the last thing that happens."""
    # First, and before the file is even read: no content of any environment
    # file makes this combination sensible, and answering it here means it can
    # never be reached by a run that happened to load a managed file.
    if args.override_external and args.yes:
        print("error: --override-external cannot be combined with --yes.\n"
              "  Overriding a committed `infrastructure_mode: external` "
              "declaration is a decision one operator takes in front of a "
              "terminal, not a flag a pipeline carries. Drop --yes and confirm "
              "at the prompt.", file=sys.stderr)
        return EXIT_EXTERNAL

    answers = _answers(args)
    path = inputs.env_path(args.project_root, args.env)

    if inputs.mode_is_external(answers):
        if not args.override_external:
            print(f"error: {path} declares {inputs.MODE_KEY}: "
                  f"{inputs.EXTERNAL} — this environment's AWS estate is "
                  f"managed by an external pipeline, and applying here would "
                  f"create resources that pipeline will revert or replace.\n"
                  f"  If that declaration is wrong, change the file.\n"
                  f"  If you genuinely need one run anyway, pass "
                  f"--override-external and confirm at the prompt.",
                  file=sys.stderr)
            return EXIT_EXTERNAL
        console.say("!" * 72)
        console.say(f"! {path} declares {inputs.MODE_KEY}: "
                    f"{answers.get(inputs.MODE_KEY)!r}")
        console.say("! --override-external overrides that for THIS RUN ONLY.")
        console.say("! The file is NOT modified, and the nodes this builds are "
                    "still configured")
        console.say("! as external — the override is per-invocation, never a "
                    "property of the environment.")
        console.say("!" * 72)

    topology = _topology(args, answers)
    connection = _connect(args, answers, topology, console)

    findings, actions, run = plan.observe(connection, topology)
    render_findings(findings, console.say)
    render_preview(findings, topology, console.say)

    if args.dry_run:
        console.say("")
        console.say("--dry-run: nothing was created. Re-run without it to "
                    "build this.")
        return EXIT_OK

    if not args.yes:
        if not console.is_interactive():
            print("error: stdin is not a terminal and --yes was not given, so "
                  "there is nobody to confirm this. Re-run at a terminal, or "
                  "pass --yes if you have read the plan above.",
                  file=sys.stderr)
            return EXIT_USAGE
        if not console.confirm(
                f"\nBuild this in account "
                f"{run.observed.get('account_id')} / {topology.region}?"):
            console.say("nothing was created.")
            return EXIT_OK

    console.say("")
    findings, actions, run = plan.apply(connection, topology)
    render_findings(findings, console.say)
    render_summary(findings, run, console.say)
    return _exit_for(run)


# ── configure ───────────────────────────────────────────────────────────────

def run_configure(args, console):
    """From "the instances are running" to "the portal answers over HTTPS".

    Read-only until it is not: the config is rendered and compared before it is
    published, and `--dry-run` stops before the first byte is written or the
    first SSH connection is made.
    """
    answers = _answers(args)
    topology = _topology(args, answers)
    connection = _connect(args, answers, topology, console)

    findings, _actions, run = plan.observe(connection, topology)
    if run.blocking:
        render_findings(findings, console.say)
        console.say("")
        console.say("The account could not be read cleanly, so nothing was "
                    "configured. Fix the above and re-run.")
        return EXIT_FINDINGS

    conf_findings, conf_actions, conf = render.ensure_config(
        connection, topology, answers, run.observed, apply=not args.dry_run)
    render_findings(conf_findings, console.say)

    hosts = _node_hosts(run.observed, topology)
    if not hosts:
        console.say("")
        console.say("No node addresses were resolved, so there is nothing to "
                    "converge yet. Run `apply` until the nodes exist.")
        return EXIT_FINDINGS

    if args.dry_run:
        console.say("")
        console.say("--dry-run: django.conf was NOT published and no node was "
                    "touched.")
        console.say(f"  would converge: {', '.join(hosts)}")
        if len(hosts) == 1:
            console.say(f"  would finish HTTPS on {hosts[0]}")
        else:
            console.say("")
            console.say(certificate_module.FLEET_HANDOFF.format(
                apex=answers.get("apex_domain")))
        return EXIT_OK

    if not conf.as_dict().get("django_conf"):
        console.say("")
        console.say("django.conf was not published, so converging the nodes "
                    "would install nothing. Fix the above and re-run.")
        return EXIT_FINDINGS

    identity = _resolve_identity(args, topology, run.observed, console)

    console.say("")
    console.say(f"converging {len(hosts)} node(s) over SSH — this waits for "
                f"cloud-init, so it can take several minutes")
    node_findings = remote.converge(
        hosts, runner_for=lambda host: remote.build_runner(
            host, user=args.ssh_user, identity=identity))
    render_findings(node_findings, console.say)

    all_findings = list(conf_findings) + list(node_findings)
    if report.Report(node_findings).is_blocking():
        _summarize(all_findings, console)
        return EXIT_FINDINGS

    all_findings.extend(
        _finish_https(args, answers, topology, hosts, identity, console))
    all_findings.extend(
        _add_deploy_key(args, answers, hosts, identity, console))

    _summarize(all_findings, console)
    return EXIT_FINDINGS if report.Report(all_findings).is_blocking() else EXIT_OK


def _resolve_identity(args, topology, observed, console):
    """The SSH key `configure` and `admin` authenticate with, found for itself.

    An explicit `--identity` always wins: an operator naming a file has said
    something this cannot know better than.

    Otherwise, the key pair this package generated has its private half sitting
    in the bootstrap secrets object, which `plan.observe` has ALREADY read out
    of the config bucket by the time either command reaches here — so there is
    no second S3 call, and no reason to make an operator extract the key from
    that JSON by hand before they can run the next command. That manual step is
    the whole difference between "one command" and "one command plus a page of
    instructions", and it was the first thing a real end-to-end run tripped on.

    An environment whose key pair was IMPORTED has no private half in the bucket
    (that is the point of importing: nothing that can only be read once ever
    exists). That is not a failure — the operator holds the key and their agent
    is what SSH will use — so it says so and returns None, which `build_runner`
    reads as "no -i flag", exactly as before this existed.

    The path is printed. THE KEY IS NOT: see `storage.materialize_ssh_identity`.
    """
    if args.identity:
        return args.identity

    path, wrote = storage.materialize_ssh_identity(
        topology, observed.get("secrets"))
    console.say("")
    if not path:
        console.say("no generated private key is stored for this environment "
                    "— its key pair was imported, so SSH will use whatever "
                    "your agent holds. Pass --identity to name a key file.")
        return None
    console.say(f"authenticating with the environment's generated key at "
                f"{path}{' (just written)' if wrote else ''}")
    return path


def _finish_https(args, answers, topology, hosts, identity, console):
    """The certificate, on a single node only.

    A fleet gets the hand-off text instead of an attempt: `certbot --nginx`
    mutates the vhost in a way that breaks `nginx -t` on any node certbot did
    not run on, so "try it and see" is not a safe default here.
    """
    apex = answers.get("apex_domain")
    if len(hosts) > 1 or spec_module.wants_balancer(topology):
        console.say("")
        console.say(certificate_module.FLEET_HANDOFF.format(apex=apex))
        return []
    if args.skip_certificate:
        console.say("")
        console.say("--skip-certificate: the node is serving its self-signed "
                    "placeholder. Re-run without the flag once DNS points here.")
        return []

    console.say("")
    console.say(f"finishing HTTPS for {apex}")
    run = remote.build_runner(hosts[0], user=args.ssh_user, identity=identity)
    findings = certificate_module.configure_certificate(
        run, apex, answers.get("operator_email"), expected_ip=hosts[0])
    render_findings(findings, console.say)
    return findings


def _add_deploy_key(args, answers, hosts, identity, console):
    """Best effort, and honest about it.

    A failure here is not a failed provision: it costs the operator one paste
    into a browser, so it prints the key and the URL rather than exiting
    non-zero.
    """
    repo = answers.get("github_repo")
    if not repo:
        return []
    run = remote.build_runner(hosts[0], user=args.ssh_user, identity=identity)
    rc, pubkey, _ = run("cat /home/ec2-user/.ssh/id_ed25519.pub", timeout=30)
    if rc != 0 or not pubkey.strip():
        console.say("")
        console.say("could not read the node's deploy public key — "
                    "ec2_bootstrap.sh generates /home/ec2-user/.ssh/id_ed25519")
        return []

    import subprocess

    title = f"ec2-user@{hosts[0]}"
    done = subprocess.run(
        ["gh", "repo", "deploy-key", "add", "/dev/stdin", "--repo", repo,
         "--title", title],
        input=pubkey + "\n", capture_output=True, text=True, check=False)
    console.say("")
    if done.returncode == 0:
        console.say(f"added the node's deploy key to {repo} as {title}")
        return []
    detail = (done.stderr or "").strip().splitlines()
    console.say(f"could not add the deploy key to {repo} automatically "
                f"({detail[-1] if detail else 'is gh installed and logged in?'})")
    console.say(f"  add it by hand at https://github.com/{repo}/settings/keys")
    console.say(f"  {pubkey.strip()}")
    return []


def _summarize(findings, console):
    counts = report.Report(findings).counts()
    console.say("")
    console.say("─" * 72)
    console.say(f"  {counts[report.PASS]} ok · {counts[report.MISSING]} "
                f"missing · {counts[report.PENDING]} pending · "
                f"{counts[report.BLIND]} failed")


# ── admin ───────────────────────────────────────────────────────────────────

LINK_NOTE = ("This link is SINGLE USE and expires in one hour. Opening it sets "
             "the password for the account; it does not reveal one.")


def run_admin(args, console):
    """Create the first superuser on node 0 and print its login link."""
    answers = _answers(args)
    topology = _topology(args, answers)
    connection = _connect(args, answers, topology, console)

    _findings, _actions, run = plan.observe(connection, topology)
    hosts = _node_hosts(run.observed, topology)
    if not hosts:
        print("error: no node address was resolved, so there is nowhere to "
              "create the account. Run `apply` first.", file=sys.stderr)
        return EXIT_FINDINGS

    email = args.email or answers.get("operator_email")
    if args.dry_run:
        console.say(f"--dry-run: would create {email} as a superuser on "
                    f"{hosts[0]} and print a one-hour login link.")
        return EXIT_OK

    identity = _resolve_identity(args, topology, run.observed, console)
    runner = remote.build_runner(hosts[0], user=args.ssh_user,
                                 identity=identity)
    rc, out, err = runner(
        f"cd {remote.PROJ_PATH} && sudo -u ec2-user python3 bin/manage.py "
        f"create_user --email {email} --superuser --login-link", timeout=300)
    output = "\n".join(part for part in (out, err) if part)

    if rc != 0 and "already exists" in output:
        # Not a failure worth a traceback: the account is there, and what the
        # operator actually wants is another link for it.
        console.say("")
        console.say(f"{email} already exists on this environment, so no "
                    f"account was created.")
        console.say("  To send that account a fresh link, use the portal's "
                    "Users section (Send reset link), or on the node:")
        console.say(f"    sudo -u ec2-user python3 {remote.PROJ_PATH}/bin/"
                    f"manage.py shell -c \\")
        console.say("      \"from mojo.apps.account.models import User; "
                    "from mojo.apps.account.utils import tokens; "
                    "from mojo.apps.account.utils.webapp_url import "
                    "build_token_url; "
                    f"u=User.objects.get(email='{email}'); "
                    "print(build_token_url('password_reset', "
                    "tokens.generate_password_reset_token(u), user=u))\"")
        return EXIT_OK
    if rc != 0:
        print(f"error: creating {email} on {hosts[0]} failed:\n{output}",
              file=sys.stderr)
        return EXIT_FINDINGS

    console.say("")
    for line in output.splitlines():
        console.say(f"  {line}")
    console.say("")
    console.say(f"  {LINK_NOTE}")
    return EXIT_OK


# ── shared ──────────────────────────────────────────────────────────────────

def _topology(args, answers):
    """The spec, plus where `git archive HEAD` is taken from.

    `project_root` is not one of the eight questions and never lands in the
    committed environment file: it is a property of the machine running the
    command, not of the environment.
    """
    topology = inputs.to_spec(answers, nlb=args.nlb)
    topology.project_root = args.project_root
    return topology


def _node_hosts(observed, topology):
    """The addresses to SSH to, node 0 first.

    Elastic IPs where the topology has them; otherwise the instances' own
    public addresses, which is the shape behind a balancer.
    """
    hosts = [address for address in (observed.get("node_addresses") or [])
             if address]
    if hosts:
        return hosts
    names = spec_module.names(topology)
    by_name = {}
    for instance in observed.get("instances") or []:
        if (instance.get("State") or {}).get("Name") not in ("pending",
                                                             "running"):
            continue
        tags = discover.tags_of(instance)
        if instance.get("PublicIpAddress"):
            by_name[tags.get("Name")] = instance.get("PublicIpAddress")
    return [by_name[name] for name in names["nodes"] if name in by_name]


def _answers(args):
    """Load and validate the environment file, or raise EnvFileError."""
    path = inputs.env_path(args.project_root, args.env)
    answers = inputs.load(path)
    remaining = inputs.problems(inputs.schema_only(answers))
    if remaining:
        raise inputs.EnvFileError(
            f"{path} cannot be used:\n  - " + "\n  - ".join(remaining))
    return answers


def _credential(args, answers):
    """A flag beats the file. The file records what the environment is usually
    built with; the flag is this operator, right now, and they can see which
    credential they meant."""
    profile = args.profile or (None if args.role_arn else answers.get("aws_profile"))
    role_arn = args.role_arn or (None if args.profile else answers.get("role_arn"))
    return profile, role_arn


def _connect(args, answers, topology, console, announce=True):
    """Build the clients and say, first thing, which account this is.

    The account id is echoed BEFORE the preview because "wrong account" is the
    mistake that costs an afternoon, and it is invisible in a plan that only
    lists resource names.
    """
    profile, role_arn = _credential(args, answers)
    connection = clients_module.build_clients(
        profile=profile, role_arn=role_arn, region=topology.region)
    identity = clients_module.identify(connection)
    if announce:
        console.say(f"account {identity['account_id']} · {topology.region} · "
                    f"{topology.project}-{topology.env} · {topology.preset}")
        console.say(f"  as {identity.get('arn')}")
    return connection


def _exit_for(run):
    if not run.get("validated", True):
        return EXIT_USAGE
    return EXIT_FINDINGS if run.get("blocking") else EXIT_OK


def _print_problems(headline, problems):
    print(f"error: {headline}:", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)


# ── rendering ───────────────────────────────────────────────────────────────

def render_findings(findings, writer=print):
    """Grouped by step, in the order the DAG produced them."""
    current = None
    for finding in findings:
        if finding.step != current:
            current = finding.step
            writer("")
            writer(f"── {current.upper()} " + "─" * max(1, 66 - len(current)))
        writer(f"[{MARKS.get(finding.status, finding.status)}] {finding.code}")
        writer(f"          {finding.message}")
        if finding.remedy:
            writer(f"          → {finding.remedy}")


def render_preview(findings, topology, writer=print):
    """Counts, then the price. This is the last thing before the typed yes."""
    counts = report.Report(findings).counts()
    parts = [f"{counts[status]} {label}" for label, status in PREVIEW_LABELS
             if counts[status]]
    writer("")
    writer("─" * 72)
    writer("  " + (" · ".join(parts) if parts else "nothing to do"))

    estimate = spec_module.estimate_cost(topology)
    writer("")
    writer("  approximate monthly cost:")
    for row in estimate["rows"]:
        writer(f"    {row['item']:<22} {row['detail']:<34} "
               f"{row['monthly']:>9.2f}")
    writer(f"    {'total':<22} {'':<34} {estimate['total']:>9.2f} "
           f"{estimate['currency']}")
    writer(f"    {estimate['note']}")
    if spec_module.wants_balancer(topology):
        writer("    a network load balancer will exist after this run — the "
               "load balancer and")
        writer("    balancer address lines above are it, and it bills whether "
               "traffic flows or not")


def render_summary(findings, run, writer=print):
    """The bottom line, and — when AWS is still building something — the one
    instruction that actually resolves it."""
    summary = report.Report(findings)
    counts = summary.counts()
    writer("")
    writer("─" * 72)
    writer(f"  {counts[report.PASS]} ok · {counts[report.PENDING]} building · "
           f"{counts[report.MISSING]} missing · {counts[report.DRIFT]} drifted "
           f"· {counts[report.MANUAL]} manual · {counts[report.BLOCKED]} "
           f"blocked · {counts[report.BLIND]} blind")

    waiting = [name for name, step in run.steps.items()
               if step.get("status") in (plan.PENDING, plan.SKIPPED)]
    if waiting and not run.blocking:
        writer("")
        writer(f"  still coming up: {', '.join(sorted(waiting))}")
        writer("  This is normal on a fresh account — Aurora and the cache take "
               "five to fifteen")
        writer("  minutes. Run `apply` again in a few minutes and it will pick "
               "these up.")
    if run.blocking:
        writer("")
        writer("  Something failed or could not be read. Fix what is reported "
               "above and re-run;")
        writer("  nothing is deleted, so a re-run resumes rather than restarts.")


def render_inventory(rows, writer=print):
    writer("")
    writer("─" * 72)
    writer("  inventory (tag-scoped — everything this environment owns)")
    if not rows:
        writer("    nothing exists yet")
        return
    for row in rows:
        writer(f"    {row['kind']:<20} {str(row['name'] or ''):<34} "
               f"{row['id'] or ''}")
        if row.get("arn"):
            writer(f"    {'':<20} {row['arn']}")


def inventory(observed, topology):
    """Every resource this environment owns, flat, with an ARN where AWS gives
    one. The input to a teardown checklist — which is a deliberate human act
    performed elsewhere, because nothing in this package deletes.
    """
    names = spec_module.names(topology)
    rows = []

    def add(kind, name, identifier=None, arn=None):
        if name or identifier or arn:
            rows.append({"kind": kind, "name": name, "id": identifier,
                         "arn": arn})

    def named(resource, fallback=None):
        return discover.tags_of(resource).get("Name") or fallback

    vpc = observed.get("vpc")
    if vpc:
        add("vpc", named(vpc, names["vpc"]), vpc.get("VpcId"))
    for subnet in observed.get("subnets") or []:
        add("subnet", named(subnet), subnet.get("SubnetId"),
            subnet.get("SubnetArn"))
    gateway = observed.get("internet_gateway")
    if gateway:
        add("internet gateway", named(gateway, names["internet_gateway"]),
            gateway.get("InternetGatewayId"))
    for role, group in (observed.get("security_groups") or {}).items():
        add(f"security group ({role})", group.get("GroupName"),
            group.get("GroupId"))

    key_pair = observed.get("key_pair")
    if key_pair:
        add("key pair", key_pair.get("KeyName"), key_pair.get("KeyPairId"))
    role = observed.get("node_role")
    if role:
        add("iam role", role.get("RoleName"), None, role.get("Arn"))
    profile = observed.get("instance_profile")
    if profile:
        add("instance profile", profile.get("InstanceProfileName"), None,
            profile.get("Arn"))

    bucket = observed.get("config_bucket")
    if bucket:
        add("s3 bucket", bucket, None, f"arn:aws:s3:::{bucket}")

    subnet_group = observed.get("db_subnet_group")
    if subnet_group:
        add("db subnet group", subnet_group.get("DBSubnetGroupName"), None,
            subnet_group.get("DBSubnetGroupArn"))
    cluster = observed.get("db_cluster")
    if cluster:
        add("aurora cluster", cluster.get("DBClusterIdentifier"), None,
            cluster.get("DBClusterArn"))
    for instance in observed.get("db_instances") or []:
        add("aurora instance", instance.get("DBInstanceIdentifier"), None,
            instance.get("DBInstanceArn"))
    cache_subnets = observed.get("cache_subnet_group")
    if cache_subnets:
        add("cache subnet group", cache_subnets.get("CacheSubnetGroupName"),
            None, cache_subnets.get("ARN"))
    cache = observed.get("cache_group")
    if cache:
        add("cache group", cache.get("ReplicationGroupId"), None,
            cache.get("ARN"))

    if observed.get("ami_id"):
        add("ami", None, observed.get("ami_id"))
    for instance in observed.get("instances") or []:
        add("instance", named(instance), instance.get("InstanceId"))
    for address in observed.get("addresses") or []:
        add("elastic ip", address.get("PublicIp"),
            address.get("AllocationId"))

    balancer = observed.get("balancer")
    if balancer:
        add("load balancer", balancer.get("LoadBalancerName"), None,
            balancer.get("LoadBalancerArn"))
    for role_name, group in (observed.get("target_groups") or {}).items():
        add(f"target group ({role_name})", group.get("TargetGroupName"), None,
            group.get("TargetGroupArn"))

    if observed.get("cloudtrail_bucket"):
        add("s3 bucket", observed.get("cloudtrail_bucket"), None,
            f"arn:aws:s3:::{observed.get('cloudtrail_bucket')}")
    for trail in observed.get("trails") or []:
        add("cloudtrail", trail.get("Name"), None, trail.get("TrailARN"))
    for detector in observed.get("detector_ids") or []:
        add("guardduty detector", None, detector)
    for name, group in (observed.get("log_groups") or {}).items():
        add("log group", name, None, group.get("arn"))

    zone = observed.get("hosted_zone")
    if zone:
        add("hosted zone", zone.get("Name"), zone.get("Id"))

    return rows


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
