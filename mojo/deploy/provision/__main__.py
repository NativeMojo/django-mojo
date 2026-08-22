"""`python3 -m mojo.deploy.provision` — the command an operator actually types.

    python3 -m mojo.deploy.provision init            # eight questions, one file
    python3 -m mojo.deploy.provision apply --dry-run # what it would build, priced
    python3 -m mojo.deploy.provision apply           # build it
    python3 -m mojo.deploy.provision configure       # config, nodes, HTTPS
    python3 -m mojo.deploy.provision admin           # first superuser + link
    python3 -m mojo.deploy.provision status          # what is there now
    python3 -m mojo.deploy.provision fleet-status --fleet shadow
    python3 -m mojo.deploy.provision fleet-apply --fleet shadow --dry-run

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
from mojo.deploy.provision import brownfield_inputs, brownfield_plan
from mojo.deploy.provision import brownfield_policy
from mojo.deploy.provision import handoff as handoff_module
from mojo.deploy.provision import clients as clients_module
from mojo.deploy.provision import (checkout, discover, github, inputs, plan,
                                   remote, render, report, storage)
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

_HANDOFF_FROM_RUNTIME = object()


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
    shared.add_argument(
        "--stable-node-ips", action="store_true",
        help="give every node its own elastic IP even behind a balancer — "
             "fixed outbound addresses for providers that allowlist caller IPs")

    parser = argparse.ArgumentParser(
        prog="python3 -m mojo.deploy.provision",
        description="Provision one AWS environment for django-mojo, "
                    "idempotently. Creates and modifies; never deletes.")
    commands = parser.add_subparsers(
        dest="command", required=True,
        metavar=("{init,apply,configure,admin,status,fleet-status,fleet-apply,"
                 "eip-handoff,eip-rollback}"))
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
    fleet_shared = argparse.ArgumentParser(add_help=False)
    fleet_shared.add_argument("--fleet", required=True,
                              help="aws/fleets/<fleet>.json to use")
    fleet_shared.add_argument("--project-root", default=".")
    fleet_shared.add_argument("--profile")
    fleet_shared.add_argument("--role-arn")
    fleet_shared.add_argument("--dry-run", action="store_true")
    fleet_shared.add_argument("--yes", action="store_true")
    fleet_status = commands.add_parser(
        "fleet-status", parents=[fleet_shared],
        help="validate exact brownfield dependencies and preview the shadow fleet")
    fleet_status.add_argument("--json", action="store_true")
    commands.add_parser(
        "fleet-apply", parents=[fleet_shared],
        help="revalidate and create only the declared shadow nodes/NLB")
    handoff_shared = argparse.ArgumentParser(add_help=False)
    handoff_shared.add_argument("--fleet", required=True)
    handoff_shared.add_argument("--project-root", default=".")
    handoff_shared.add_argument("--profile")
    handoff_shared.add_argument(
        "--role-arn",
        help="source role used only to assume the manifest's dedicated role")
    handoff_shared.add_argument("--plan-file")
    handoff_shared.add_argument("--plan-digest")
    handoff_shared.add_argument("--confirm")
    handoff_shared.add_argument("--operation-id")
    handoff_shared.add_argument(
        "--dry-run", action="store_true",
        help="force preview mode; never invokes a provider mutation")
    eip_handoff = commands.add_parser(
        "eip-handoff", parents=[handoff_shared],
        help="preview, rehearse, execute, or resume an exact EIP handoff")
    eip_handoff.add_argument(
        "--mode", choices=("preview", "rehearse", "apply", "resume"),
        default="preview")
    eip_rollback = commands.add_parser(
        "eip-rollback", parents=[handoff_shared],
        help="preview or execute exact source restoration from a journal")
    eip_rollback.add_argument(
        "--mode", choices=("preview", "apply"), default="preview")
    return parser


def main(argv, console=None, *,
         handoff_context=_HANDOFF_FROM_RUNTIME,
         handoff_plan_loader=_HANDOFF_FROM_RUNTIME,
         handoff_executor=_HANDOFF_FROM_RUNTIME,
         operation_id_factory=_HANDOFF_FROM_RUNTIME):
    """The whole program. Returns an exit code; never calls `sys.exit`.

    `console` is the reader/writer seam. Left alone it is the real terminal; a
    test hands in a scripted one and drives the entire command in-process,
    which is how the prompt flow, the confirmation and the refusals are covered
    without patching `builtins.input` — a process-global that leaks into
    whatever else the runner is doing in the same interpreter. The handoff
    callables are similarly local seams for recovery-path tests; their sentinel
    defaults resolve to the exact production functions below.
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
        if args.command == "fleet-status":
            return run_fleet_status(args, console)
        if args.command == "fleet-apply":
            return run_fleet_apply(args, console)
        if args.command == "eip-handoff":
            return run_eip_handoff(
                args, console, handoff_context=handoff_context,
                handoff_plan_loader=handoff_plan_loader,
                handoff_executor=handoff_executor,
                operation_id_factory=operation_id_factory)
        if args.command == "eip-rollback":
            return run_eip_rollback(args, console)
        return run_status(args, console)
    except KeyboardInterrupt:
        if args.command in ("eip-handoff", "eip-rollback"):
            print("\ninterrupted — do not guess or run ordinary apply; use "
                  "the recorded operation and journal to resume or rollback.",
                  file=sys.stderr)
            _repeat_recovery(args, console)
        else:
            print("\ninterrupted — re-run `apply` to pick up where this stopped; "
                  "there is no state file to clean up", file=sys.stderr)
        return EXIT_INTERRUPTED
    except inputs.EnvFileError as err:
        print(f"error: {err}", file=sys.stderr)
        return EXIT_USAGE
    except clients_module.CredentialError as err:
        print(f"error: {err}", file=sys.stderr)
        return EXIT_USAGE
    except brownfield_policy.BrownfieldCallBlocked as err:
        print(f"error: brownfield safety boundary refused the run: {err}",
              file=sys.stderr)
        return EXIT_FINDINGS
    except brownfield_plan.DependencyDriftError as err:
        print(f"error: {err}", file=sys.stderr)
        return EXIT_FINDINGS
    except handoff_module.HandoffError as err:
        print(f"error: EIP handoff refused: {err}", file=sys.stderr)
        _repeat_recovery(args, console)
        return EXIT_FINDINGS
    except Exception as err:
        if (args.command in ("eip-handoff", "eip-rollback")
                and (err.__class__.__module__.startswith("botocore")
                     or isinstance(err, OSError))):
            print(f"error: EIP operation failed: "
                  f"{handoff_module.bounded_error(err)}", file=sys.stderr)
            _repeat_recovery(args, console)
            return EXIT_FINDINGS
        raise


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
    if args.stable_node_ips:
        answers["stable_node_ips"] = True

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


# ── exact-resource brownfield fleets ───────────────────────────────────────

def _fleet(args, console, announce=True):
    path = brownfield_inputs.fleet_path(args.project_root, args.fleet)
    manifest = brownfield_inputs.load(path)
    if manifest["fleet"] != args.fleet:
        raise inputs.EnvFileError(
            f"{path} declares fleet {manifest['fleet']!r}, not "
            f"{args.fleet!r}")
    topology = brownfield_inputs.to_spec(manifest,
                                         project_root=args.project_root)
    connection = clients_module.build_clients(
        profile=args.profile, role_arn=args.role_arn,
        region=topology.region,
        mutation_policy=brownfield_policy.MutationPolicy())
    identity = clients_module.identify(connection)
    if announce:
        console.say(f"account {identity['account_id']} · {topology.region} · "
                    f"fleet {topology.fleet} · exact-resource brownfield")
        console.say(f"  as {identity.get('arn')}")
    return manifest, topology, connection


def run_fleet_status(args, console):
    manifest, topology, connection = _fleet(
        args, console, announce=not args.json)
    findings, actions, run = brownfield_plan.observe(connection, topology)
    if args.json:
        import json
        console.say(json.dumps({
            "account_id": run.observed.get("account_id"),
            "region": topology.region, "fleet": topology.fleet,
            "manifest_digest": manifest["manifest_digest"],
            "dependency_digest": run.observed.get("dependency_digest"),
            "action_digest": run.observed.get("action_digest"),
            "inventory": run.observed.get("dependency_inventory"),
            "steps": {name: dict(step) for name, step in run.steps.items()},
            **report.Report(findings, actions).as_dict(),
        }, indent=2, default=str))
    else:
        render_findings(findings, console.say)
        _render_fleet_preview(topology, findings, actions, run, console)
        console.say("")
        console.say("fleet-status is read-only: nothing was created or changed.")
    return EXIT_FINDINGS if run.blocking else EXIT_OK


def run_fleet_apply(args, console):
    manifest, topology, connection = _fleet(args, console)
    findings, actions, preview = brownfield_plan.observe(connection, topology)
    render_findings(findings, console.say)
    _render_fleet_preview(topology, findings, actions, preview, console)
    if preview.blocking:
        console.say("")
        console.say("The exact dependency boundary is not clean, so apply is "
                    "refused before its first mutation.")
        return EXIT_FINDINGS
    if args.dry_run:
        console.say("")
        console.say("--dry-run: nothing was created. Re-run without it to "
                    "prepare the shadow fleet.")
        return EXIT_OK
    if not args.yes:
        if not console.is_interactive():
            print("error: stdin is not a terminal and --yes was not given; "
                  "there is nobody to confirm the exact fleet preview.",
                  file=sys.stderr)
            return EXIT_USAGE
        if not console.confirm(
                f"\nCreate only the shadow fleet resources in account "
                f"{preview.observed.get('account_id')} / {topology.region}?"):
            console.say("nothing was created.")
            return EXIT_OK
    expected_digest = preview.observed.get("dependency_digest")
    expected_action_digest = preview.observed.get("action_digest")
    console.say("")
    findings, actions, run = brownfield_plan.apply(
        connection, topology, expected_digest=expected_digest,
        expected_action_digest=expected_action_digest)
    render_findings(findings, console.say)
    render_summary(findings, run, console.say)
    return _exit_for(run)


def _handoff_context(args):
    path = brownfield_inputs.fleet_path(args.project_root, args.fleet)
    manifest = brownfield_inputs.load(path)
    if manifest["fleet"] != args.fleet:
        raise inputs.EnvFileError(
            f"{path} declares fleet {manifest['fleet']!r}, not "
            f"{args.fleet!r}")
    topology = brownfield_inputs.to_spec(
        manifest, project_root=args.project_root)
    connection = clients_module.build_handoff_clients(
        topology, profile=args.profile, role_arn=args.role_arn)
    plan_path = args.plan_file or handoff_module.default_plan_path(topology)
    return topology, connection, plan_path


def _require_handoff_args(args, *names):
    missing = [f"--{name.replace('_', '-')}" for name in names
               if not getattr(args, name, None)]
    if missing:
        raise handoff_module.HandoffRefused(
            f"{args.command} --mode {args.mode} requires "
            f"{', '.join(missing)}")


def run_eip_handoff(args, console, *,
                    handoff_context=_HANDOFF_FROM_RUNTIME,
                    handoff_plan_loader=_HANDOFF_FROM_RUNTIME,
                    handoff_executor=_HANDOFF_FROM_RUNTIME,
                    operation_id_factory=_HANDOFF_FROM_RUNTIME):
    if handoff_context is _HANDOFF_FROM_RUNTIME:
        handoff_context = _handoff_context
    if handoff_plan_loader is _HANDOFF_FROM_RUNTIME:
        handoff_plan_loader = handoff_module.load_plan
    if handoff_executor is _HANDOFF_FROM_RUNTIME:
        handoff_executor = handoff_module.handoff
    topology, connection, plan_path = handoff_context(args)
    mode = "preview" if args.dry_run else args.mode
    if args.dry_run and args.mode not in ("preview", "rehearse"):
        console.say("--dry-run forced preview mode; no provider mutation is reachable.")
    if mode == "preview":
        plan = handoff_module.build_plan(connection, topology)
        handoff_module.save_plan(plan_path, plan)
        _render_handoff_plan(plan, plan_path, console)
        console.say("")
        console.say("preview is read-only: no address, NLB mapping, DNS record, "
                    "certificate, or data-plane resource was changed.")
        return EXIT_OK

    _require_handoff_args(args, "plan_digest", "confirm")
    plan = handoff_plan_loader(plan_path)
    if mode == "resume":
        _require_handoff_args(args, "operation_id")
    elif not args.operation_id:
        if operation_id_factory is _HANDOFF_FROM_RUNTIME:
            import uuid
            operation_id_factory = uuid.uuid4
        args.operation_id = str(operation_id_factory())
    handoff_module.validate_operation_id(args.operation_id)
    args._handoff_recovery = (topology, args.operation_id)
    console.say("recovery coordinates (record these before continuing):")
    _render_handoff_coordinates(topology, args.operation_id, console)
    if mode == "rehearse":
        journal = handoff_module.rehearse(
            connection, topology, plan, args.plan_digest, args.confirm,
            operation_id=args.operation_id)
        console.say(f"rehearsal {journal['operation_id']} completed without "
                    f"a provider mutation")
        _render_handoff_coordinates(topology, journal["operation_id"], console)
        return EXIT_OK
    if mode == "resume":
        journal = handoff_module.resume(
            connection, topology, plan, args.plan_digest, args.confirm,
            args.operation_id)
    else:
        journal = handoff_executor(
            connection, topology, plan, args.plan_digest, args.confirm,
            operation_id=args.operation_id)
    console.say(f"handoff {journal['operation_id']} reached {journal['state']}")
    _render_handoff_coordinates(topology, journal["operation_id"], console)
    return EXIT_OK


def run_eip_rollback(args, console):
    topology, connection, plan_path = _handoff_context(args)
    _require_handoff_args(args, "plan_digest", "operation_id")
    handoff_module.validate_operation_id(args.operation_id)
    plan = handoff_module.load_plan(plan_path)
    args._handoff_recovery = (topology, args.operation_id)
    handoff_module._bind_plan(plan, args.plan_digest)
    handoff_module._bind_boundary_from_plan(connection, plan)
    if args.dry_run or args.mode == "preview":
        store = handoff_module.JournalStore(
            connection, topology, args.operation_id)
        store.verify_bucket()
        store.inspect_lock(plan["plan_digest"])
        journal = store.load(recover=False)
        handoff_module._journal_matches(journal, plan)
        state = handoff_module.revalidate_runtime(
            connection, topology, plan, journal, direction="rollback")
        console.say("rollback preview — no provider mutation is reachable")
        console.say(json_dump({
            "operation_id": args.operation_id,
            "plan_digest": plan["plan_digest"],
            "journal_state": journal["state"],
            "current_nlb_map": state["map"],
            "exact_inverse": plan["inverse"],
            "confirmation": handoff_module.expected_confirmation(
                plan, "ROLLBACK", args.operation_id),
        }))
        _render_handoff_coordinates(topology, journal["operation_id"], console)
        return EXIT_OK
    _require_handoff_args(args, "confirm")
    console.say("recovery coordinates (record these before continuing):")
    _render_handoff_coordinates(topology, args.operation_id, console)
    journal = handoff_module.rollback(
        connection, topology, plan, args.plan_digest, args.confirm,
        args.operation_id)
    console.say(f"rollback {journal['operation_id']} reached "
                f"{journal['state']}")
    _render_handoff_coordinates(topology, journal["operation_id"], console)
    return EXIT_OK


def _render_handoff_plan(plan, path, console):
    console.say(json_dump(plan))
    console.say("")
    console.say(f"immutable plan: {path} (0600)")
    console.say(f"plan digest: {plan['plan_digest']}")
    console.say("rehearsal confirmation:")
    console.say(f"  {handoff_module.expected_confirmation(plan, 'REHEARSE')}")
    console.say("live handoff confirmation:")
    console.say(f"  {handoff_module.expected_confirmation(plan, 'HANDOFF')}")


def _render_handoff_coordinates(topology, operation_id, console):
    coordinates = handoff_module.journal_coordinates(topology, operation_id)
    console.say(f"operation id: {coordinates['operation_id']}")
    console.say(f"local journal: {coordinates['local_journal']}")
    console.say(f"remote journal: {coordinates['remote_journal']}")
    console.say(f"remote lock: {coordinates['remote_lock']}")


def _repeat_recovery(args, console):
    recovery = getattr(args, "_handoff_recovery", None)
    if recovery:
        topology, operation_id = recovery
        console.say("recovery coordinates:")
        _render_handoff_coordinates(topology, operation_id, console)


def json_dump(value):
    import json
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def _render_fleet_preview(topology, findings, actions, run, console):
    creates = sum(1 for action in actions if action.verb == "create")
    modifies = sum(1 for action in actions if action.verb != "create")
    inventory = run.observed.get("dependency_inventory") or {}
    dependency_fields = len(_fleet_leaves(inventory))
    console.say("")
    console.say("─" * 72)
    console.say(f"  account: {run.observed.get('account_id')}  "
                f"region: {topology.region}  fleet: {topology.fleet}")
    console.say(f"  dependency digest: "
                f"{run.observed.get('dependency_digest')}")
    console.say(f"  manifest digest: {topology.manifest_digest}")
    console.say(f"  action digest: {run.observed.get('action_digest')}")
    for declaration in topology.node_declarations:
        if "request_service" in declaration:
            selected = str(declaration["request_service"]).lower()
            console.say(f"  node request service: {declaration['name']}="
                        f"{selected}")
    console.say(f"  allowed actions: {creates} create · {modifies} modify  "
                f"read-only dependency fields: {dependency_fields}")
    console.say("  forced false: manage/publish DNS · certificates/ACM · "
                "preserved-EIP handoff · database/cache/storage writes")
    console.say("  no teardown exists; only fleet-tagged preparation "
                "resources are eligible for convergence")


def _fleet_leaves(value):
    if isinstance(value, dict):
        rows = []
        for item in value.values():
            rows.extend(_fleet_leaves(item))
        return rows
    if isinstance(value, list):
        rows = []
        for item in value:
            rows.extend(_fleet_leaves(item))
        return rows
    return [value]


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
        _wire_deploy_plane(args, answers, hosts, identity, run.observed,
                           console))

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


def _wire_deploy_plane(args, answers, hosts, identity, observed, console):
    """The last mile: make a push to the project's repo able to deploy it.

    Three things, in this order, because each depends on the one before:
    every node's deploy key on the repository, then every node's `/opt/api`
    wired to that repository as a real checkout, then one push webhook
    pointed at the fleet. Without all three a node serves perfectly and
    accepts no deploy — a state that looks like success from the outside,
    which is exactly why this runs inside `configure` rather than living in a
    runbook.

    Best effort throughout. `gh` frequently cannot administer the repository
    being deployed, and an estate that is otherwise finished is not failed by
    that — every failure names the manual step instead.
    """
    repo = answers.get("github_repo")
    if not repo:
        console.say("")
        console.say("no repository was configured, so nothing was wired for "
                    "push-to-deploy — re-run `provision configure` after "
                    "setting one to finish the deploy plane.")
        return []

    console.say("")
    console.say(f"wiring push-to-deploy for {repo}")
    findings = []
    for host in hosts:
        run = remote.build_runner(host, user=args.ssh_user, identity=identity)
        findings.extend(github.ensure_deploy_key(run, host, repo))
        findings.extend(checkout.ensure_checkout(run, host, repo))

    secrets = observed.get("secrets") or {}
    findings.extend(github.ensure_webhook(
        repo, answers.get("apex_domain"),
        secrets.get("github_webhook_secret")))

    render_findings(findings, console.say)
    return findings


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
    topology = inputs.to_spec(answers, nlb=args.nlb,
                              stable_node_ips=args.stable_node_ips)
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
