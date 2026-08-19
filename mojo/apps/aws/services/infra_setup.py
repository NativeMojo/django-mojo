"""The portal's read-only view of the provisioned AWS topology.

THREE THINGS IN THIS REPOSITORY LOOK AT AN AWS ACCOUNT. They are not
interchangeable, and picking the wrong one wastes an afternoon:

    mojo/deploy/check_setup.py          pre-Django, read-only, and it JUDGES.
                                        It scores an account against the
                                        django-mojo reference topology and
                                        universal security expectations, and
                                        exits non-zero so it can gate a deploy.
                                        Answers "is this account set up
                                        correctly?".
    mojo/apps/aws/services/aws_check.py in-Django, reads settings, and CREATES
                                        MISSING integration surfaces — the cron
                                        rule, the S3 file manager, SES, dnsman.
                                        It is about a running deployment's
                                        readiness, not about infrastructure.
                                        Answers "can this deployment talk to
                                        AWS?".
    mojo/deploy/provision/discover.py   pre-Django, read-only, and JUDGES
                                        NOTHING. It returns the raw shape of
                                        the resources the provisioner manages
                                        so its ensure-services can decide what
                                        to create. Answers "what is already
                                        there?".

THIS MODULE IS NOT A FOURTH OBSERVER. It owns no opinion about an account at
all: it resolves which environment this installation is, hands the resulting
spec to `mojo.deploy.provision.plan.observe` — the exact observation the CLI
performs before an `apply` — and renders the findings that come back as System
Setup readiness rows. The judgement is the provisioner's; the rendering is ours.

THE IMPORT DIRECTION IS THE LEGAL ONE. `mojo/deploy/` may never import Django
or `mojo.helpers.*` (see `mojo/deploy/__init__.py`). The reverse — a Django-side
module importing `mojo.deploy.provision` — is fine, and is exactly what happens
here. This module must never become the reason someone adds the reverse import
to the provisioner.

READ-ONLY IN v1, AND DELIBERATELY SO. The section registers with `fix=None`.
`system_setup._build_steps()` adds a step for EVERY fixable section on EVERY
"Fix all" run regardless of that section's current status, and `_execute_planned`
treats a raised `DefinitiveSetupFailure` as terminal (`operation.status =
"failed"`). A fixer here that refused under `INFRASTRUCTURE_MODE = external`
would therefore not refuse politely — it would hard-fail every Fix-all run on
such an install, before the operator ever reached the sections that can actually
be repaired. The external-mode fact is reported as a `check` row instead, and
`refuse_external()` below backstops any apply path this module ever grows.

NOTHING HAPPENS ON AN INSTALLATION THIS CLI NEVER PROVISIONED. `mojo.apps.aws`
is installed on every fresh clone, and `plan.observe()` is dozens of Describe
calls. The environment is resolved by DISCOVERING `aws/environments/*.json`
before a single client is constructed; an installation with no such file gets
one `pending` row and zero AWS calls, forever.
"""

import hashlib
import json
import os

from django.core.cache import cache

from mojo.apps.account.services import system_readiness
from mojo.deploy.provision import discover, inputs, plan, report
from mojo.helpers import infrastructure, logit
from mojo.helpers import paths
from mojo.helpers.aws.client import get_client
from mojo.helpers.aws.provider_call import ProviderCallError
from mojo.helpers.settings import settings


logger = logit.get_logger("aws_infra_setup", "aws.log")


SECTION_CODE = "aws_infrastructure"
SECTION_LABEL = "AWS infrastructure"
SECTION_ORDER = 34

# Matches `capacity.REPORT_TTL`. Short on purpose: this is a page an operator
# opens BECAUSE something is changing.
REPORT_TTL = 120

ENVIRONMENT_SETTING = "MOJO_ENVIRONMENT"

# `system_readiness.run` keeps 64 checks per section, but that is NOT the
# binding limit — `setup_safety.sanitize` bounds the WHOLE serialized report to
# 256 items, shared across every registered section, and one check row with
# details costs a dozen of them. Sixty-four rows here would crowd every other
# section out of the report an operator is reading.
#
# So this section follows the shape the hosting sections already use: a global
# summary row first, then problem detail rows only, bounded — see
# `docs/web_developer/account/system_setup.md`. A converged account costs two
# rows total, which is the common case and the one worth optimizing.
PROBLEM_ROW_BUDGET = 12

# Finding status → readiness status.
#
# PENDING and MANUAL are shipped statuses the original plan predates. PENDING
# maps to `pending` because that is literally what it means — an Aurora cluster
# reports `creating` for ten minutes and the next observation clears it. MANUAL
# maps to `warn`, not `fail`: the resource exists and works, the difference is
# on a field AWS makes immutable after creation, and there is no repair anyone
# can perform from this portal — only a human with a migration plan. Painting a
# section red for a state nothing can change trains operators to ignore red.
FINDING_STATUS = {
    report.PASS: "pass",
    report.DRIFT: "warn",
    report.MISSING: "pending",
    report.MANUAL: "warn",
    report.PENDING: "pending",
    report.BLOCKED: "fail",
}

# Readiness precedence, worst first — the same order `_section_status` uses.
STATUS_RANK = {"fail": 3, "pending": 2, "warn": 1, "pass": 0}


# ── environment resolution ──────────────────────────────────────────────────

def _project_root():
    """The checkout this installation runs from, or the working directory.

    `paths.PROJECT_ROOT` is created inside `configure_paths()`, so a process
    that never ran it has no attribute at all rather than a wrong value.
    """
    root = getattr(paths, "PROJECT_ROOT", None)
    return str(root) if root else os.getcwd()


def _environments_dir():
    return os.path.join(_project_root(), inputs.ENV_DIR)


def _environment_names():
    """The env slugs with a file, sorted. An unreadable directory is zero."""
    directory = _environments_dir()
    try:
        entries = os.listdir(directory)
    except OSError:
        return []
    return sorted(name[:-len(".json")] for name in entries
                  if name.endswith(".json") and len(name) > len(".json"))


def _selected_environment():
    try:
        raw = settings.get_static(ENVIRONMENT_SETTING, "")
    except Exception as err:
        logger.error(
            f"{ENVIRONMENT_SETTING} could not be read "
            f"({err.__class__.__name__}) — resolving by discovery instead")
        return ""
    return str(raw or "").strip()


def _unresolved(explanation, remediation, details=None):
    return None, {"explanation": explanation, "remediation": remediation,
                  "details": details or {}}


def _resolve_spec():
    """`(spec, problem)` — exactly one of the two is None.

    Fail closed. Two environment files and nothing naming which one this
    installation is means we do not know, and guessing would report a
    production account's drift on a staging portal.
    """
    names = _environment_names()
    selected = _selected_environment()

    if selected:
        if selected not in names:
            return _unresolved(
                f"{ENVIRONMENT_SETTING} names the {selected!r} environment, and "
                f"no {selected}.json exists in this project's aws/environments.",
                f"Create the environment with the provisioning CLI, or correct "
                f"{ENVIRONMENT_SETTING}, then rerun.",
                {"setting": ENVIRONMENT_SETTING, "selected": selected,
                 "environment_count": len(names)})
        chosen = selected
    elif not names:
        return _unresolved(
            "This installation's AWS infrastructure was not provisioned by "
            "django-mojo, so there is nothing here to observe.",
            "Run `python3 -m mojo.deploy.provision init` to declare an "
            "environment, or leave this section unresolved.",
            {"environment_count": 0})
    elif len(names) > 1:
        return _unresolved(
            f"This project declares {len(names)} AWS environments, and nothing "
            f"names which one this installation is.",
            f"Set {ENVIRONMENT_SETTING} in this deployment's settings file to "
            f"the environment this installation runs, then rerun.",
            {"setting": ENVIRONMENT_SETTING, "environment_count": len(names)})
    else:
        chosen = names[0]

    path = inputs.env_path(_project_root(), chosen)
    try:
        answers = inputs.load(path)
    except inputs.EnvFileError as err:
        return _unresolved(
            f"The {chosen!r} environment file could not be read: {err}",
            "Correct the environment file in aws/environments, then rerun.",
            {"environment": chosen})
    except Exception as err:
        logger.error(
            f"aws/environments/{chosen}.json could not be loaded "
            f"({err.__class__.__name__})")
        return _unresolved(
            f"The {chosen!r} environment file could not be read safely.",
            "Correct the environment file in aws/environments, then rerun.",
            {"environment": chosen})

    problems = inputs.problems(answers)
    if problems:
        return _unresolved(
            f"The {chosen!r} environment file is not usable: {problems[0]}",
            "Correct the environment file in aws/environments, then rerun.",
            {"environment": chosen, "problem_count": len(problems)})

    return inputs.to_spec(answers), None


# ── clients ─────────────────────────────────────────────────────────────────

class _FactorySession:
    """A boto3-Session-shaped shim so `discover.Clients` stays lazy.

    `Clients(**overrides)` would need every service named up front — twelve
    clients built to answer a section an operator may never open. `Clients`
    asks its session for a client only when a step actually reads that service,
    so routing the session through `get_client` keeps both the laziness and the
    bounded timeouts/retries the rest of the portal uses.
    """

    def __init__(self, factory, region):
        self._factory = factory
        self._region = region

    def client(self, service, **kwargs):
        return self._factory(service, region=self._region)


def _clients(context, spec):
    """The injection seam every `aws_setup.check_*` already uses, plus a factory.

    NEVER called before `_resolve_spec()` succeeds — constructing a client is
    the first step toward an AWS call this installation may have no business
    making.
    """
    injected = context.get("aws_clients")
    if injected is not None:
        return injected
    factory = context.get("aws_client_factory") or get_client
    region = spec.region or settings.get_static("AWS_REGION", "us-east-1")
    return discover.Clients(session=_FactorySession(factory, region))


def _observe(context, spec):
    """`plan.observe`, or the seam a test puts in front of it."""
    observer = context.get("aws_observe") or plan.observe
    return observer(_clients(context, spec), spec)


# ── rows ────────────────────────────────────────────────────────────────────

def _row(code, status, explanation, remediation="", details=None):
    """Every row this section produces. Nothing here is fixable — see the
    module docstring for why registering a fixer would break Fix-all."""
    return {"code": code, "status": status, "explanation": explanation,
            "remediation": remediation, "fixable": False, "details": details}


def _mode_row():
    if infrastructure.is_external():
        return _row(
            f"{SECTION_CODE}.mode", "warn",
            infrastructure.refusal_message("Infrastructure repair"),
            f"Apply infrastructure changes through the pipeline that owns them, "
            f"or set {infrastructure.SETTING} to {infrastructure.MANAGED} if "
            f"this portal owns them.",
            {"mode": infrastructure.EXTERNAL, "setting": infrastructure.SETTING})
    return _row(
        f"{SECTION_CODE}.mode", "pass",
        "This portal owns this installation's AWS infrastructure.", "",
        {"mode": infrastructure.MANAGED, "setting": infrastructure.SETTING})


def _finding_status(finding):
    """BLIND splits by cause, matching `system_setup`'s own rule.

    A denied IAM action is a permanent operator problem and reads `fail`.
    Anything else BLIND — a throttle, a timeout, an unreachable endpoint — is
    `pending`: still blocking green, because a section nobody was allowed to
    read must never look converged, but not a red page for a transient blip.
    """
    if finding.status != report.BLIND:
        return FINDING_STATUS.get(finding.status, "pending")
    return "fail" if _denied_action(finding) else "pending"


def _denied_action(finding):
    """The IAM action a BLIND finding names, when it names one.

    `report.safe` encodes the cause in the finding code: `<name>.denied` is the
    denied case, and `<name>` is the operation. There is no structured field to
    read, so the code is the contract — and it is a stable, greppable one.
    """
    code = str(getattr(finding, "code", "") or "")
    if code.endswith(".denied"):
        return code[:-len(".denied")]
    return ""


def _step_rows(findings, run):
    """A summary row, then the steps that are not `pass`. Aggregation is
    mandatory, not cosmetic.

    A converge reports well over a hundred findings on a healthy account. One
    row per finding would blow the report's shared 256-item sanitize budget and
    take the other sections' rows down with it, so every finding a step produced
    is rolled into that step's single row, and a step with nothing to say does
    not get a row at all.
    """
    order = []
    grouped = {}
    for finding in findings:
        name = str(getattr(finding, "step", "") or "unknown")
        if name not in grouped:
            grouped[name] = []
            order.append(name)
        grouped[name].append(finding)
    for name in (run.steps or {}):
        if name not in grouped:
            grouped[name] = []
            order.append(name)

    rows = [_step_row(name, grouped[name], run) for name in order]
    problems = [row for row in rows if row["status"] != "pass"]
    return [_summary_row(rows)] + _bounded(problems)


def _summary_row(rows):
    """The authoritative count, always present and never truncated away.

    An operator reads this one to know how bad it is; the rows below it are the
    detail, and they are the half that can be bounded without losing the answer.
    """
    counts = {"pass": 0, "warn": 0, "pending": 0, "fail": 0}
    for row in rows:
        counts[row["status"]] += 1
    status = "pass"
    for candidate in ("fail", "pending", "warn"):
        if counts[candidate]:
            status = candidate
            break
    return _row(
        f"{SECTION_CODE}.summary", status,
        f"Infrastructure readiness: {counts['pass']} ready, {counts['warn']} "
        f"warning, {counts['pending']} pending, {counts['fail']} failed across "
        f"{len(rows)} provisioning steps.",
        "" if status == "pass" else
        "Run `python3 -m mojo.deploy.provision plan` for the full report, then "
        "`apply` to converge.",
        {"steps": len(rows), "failed": counts["fail"],
         "pending": counts["pending"], "warning": counts["warn"]})


def _step_row(name, findings, run):
    statuses = [_finding_status(finding) for finding in findings]
    status = max(statuses, key=lambda item: STATUS_RANK[item]) if statuses else "pass"

    worst = None
    for finding, mapped in zip(findings, statuses):
        if worst is None or STATUS_RANK[mapped] > STATUS_RANK[worst[1]]:
            worst = (finding, mapped)

    step_state = str((run.steps or {}).get(name, {}).get("status") or "")
    if worst is None:
        explanation = f"{name}: nothing to report."
        remediation = ""
    else:
        explanation = f"{name}: {worst[0].message}"
        remediation = str(worst[0].remedy or "")

    details = {"step": name, "findings": len(findings)}
    if step_state:
        details["step_status"] = step_state
    if worst is not None:
        details["worst_code"] = worst[0].code
    return _row(f"{SECTION_CODE}.{name}", status, explanation, remediation,
                details)


def _bounded(rows):
    """Worst first, then bounded. Order is the correctness half.

    Both ceilings — readiness's 64 checks and sanitize's shared 256-item budget
    — truncate from the END of the list. A `fail` row that sorted after twelve
    `warn` rows would therefore vanish while the warnings survived, which turns
    a display limit into a correctness bug. Sorting by severity first means the
    row that gets dropped is always the least important one present, and the
    overflow row that replaces the remainder still carries their worst status.
    """
    ranked = sorted(range(len(rows)),
                    key=lambda index: (-STATUS_RANK[rows[index]["status"]],
                                       index))
    ordered = [rows[index] for index in ranked]
    if len(ordered) <= PROBLEM_ROW_BUDGET:
        return ordered
    dropped = ordered[PROBLEM_ROW_BUDGET:]
    worst = max((row["status"] for row in dropped),
                key=lambda item: STATUS_RANK[item])
    kept = ordered[:PROBLEM_ROW_BUDGET]
    kept.append(_row(
        f"{SECTION_CODE}.additional_steps", worst,
        f"{len(dropped)} further provisioning steps need attention and are "
        f"counted in the summary above.",
        "Run `python3 -m mojo.deploy.provision plan` for the complete report.",
        {"omitted_steps": len(dropped), "worst_status": worst}))
    return kept


# ── the check ───────────────────────────────────────────────────────────────

def _cache_key(spec):
    """Region AND spec identity.

    Keyed on the spec alone, two installations pointing the same declaration at
    different accounts or regions would serve each other's observation.
    """
    identity = {key: value for key, value in spec.as_dict().items()
                if isinstance(value, (str, int, float, bool, list, type(None)))}
    digest = hashlib.sha1(
        json.dumps(identity, sort_keys=True, default=str).encode()
    ).hexdigest()[:20]
    return f"{SECTION_CODE}:v1:{spec.region}:{digest}"


def _observed_rows(context, spec):
    """The step rows, cached briefly — except on the final-readiness path.

    `context["operation"]` is present only when System Setup is re-checking
    after a fix attempt. Serving a pre-fix observation as proof of a post-fix
    state is not merely stale, it is wrong, so that path always observes.
    """
    bypass = bool(context.get("operation"))
    key = _cache_key(spec)
    if not bypass:
        try:
            cached = cache.get(key)
        except Exception:
            cached = None
        if isinstance(cached, list):
            return cached

    findings, _actions, run = _observe(context, spec)
    rows = _step_rows(findings, run)

    if not bypass:
        try:
            cache.set(key, rows, REPORT_TTL)
        except Exception:
            logger.warning("aws infrastructure observation could not be cached")
    return rows


def check_infrastructure(context):
    """Read-only readiness for the provisioned topology. Never raises.

    An exception escaping here does not produce a worse row — it replaces the
    ENTIRE section with `system_readiness.run`'s opaque `check_error`, losing
    the mode row and every step with it. So every failure mode is caught and
    rendered.
    """
    context = context or {}
    rows = [_mode_row()]

    try:
        spec, problem = _resolve_spec()
    except Exception as err:
        logger.error(
            f"AWS infrastructure environment could not be resolved "
            f"({err.__class__.__name__})")
        rows.append(_row(
            f"{SECTION_CODE}.environment", "pending",
            "This installation's AWS environment could not be resolved.",
            "Inspect the server log for this check, then rerun."))
        return rows

    if spec is None:
        rows.append(_row(
            f"{SECTION_CODE}.environment", "pending", problem["explanation"],
            problem["remediation"], problem["details"]))
        return rows

    try:
        rows.extend(_observed_rows(context, spec))
    except ProviderCallError as exc:
        remediation = (
            f"Grant {exc.iam_action} to the selected AWS identity, then rerun."
            if exc.iam_action else
            "Verify the selected AWS identity, region, and read permissions, "
            "then rerun.")
        rows.append(_row(
            f"{SECTION_CODE}.observation", "fail",
            "The provisioned AWS topology could not be inspected safely.",
            remediation, exc.detail()))
    except Exception as err:
        logger.error(
            f"AWS infrastructure observation failed "
            f"({err.__class__.__name__})")
        rows.append(_row(
            f"{SECTION_CODE}.observation", "fail",
            "The provisioned AWS topology could not be inspected safely.",
            "Inspect the server log for this check, then rerun.",
            {"exception_class": err.__class__.__name__}))
    return rows


# ── the backstop ────────────────────────────────────────────────────────────

def refuse_external(action_label=""):
    """`None` when managed; raises `DefinitiveSetupFailure` when external.

    The first statement in any apply path this module ever grows. It is a
    BACKSTOP, not the operator-facing explanation: `system_setup._execute_planned`
    catches `DefinitiveSetupFailure` and records only `exception_class`, so the
    message raised here never reaches a human. The sentence an operator reads
    lives in `_mode_row()` above, on the `check` — which is also why this
    section registers with no `fix` at all in v1.
    """
    if not infrastructure.is_external():
        return None
    logger.error(
        f"{action_label or infrastructure.DEFAULT_ACTION} refused: "
        f"{infrastructure.SETTING} is {infrastructure.EXTERNAL}")
    raise system_readiness.DefinitiveSetupFailure(
        infrastructure.refusal_message(action_label))


def register_sections():
    """Read-only. `fix=None` is load-bearing — see the module docstring."""
    system_readiness.register_section(
        SECTION_CODE, SECTION_LABEL, check_infrastructure, fix=None,
        order=SECTION_ORDER)
