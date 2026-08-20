"""What a provisioning step tells you: findings, actions, and resolved values.

Three plain classes, deliberately not `objict`. `Finding` and `Action` cross a
serialization boundary — the portal turns them into JSON for a browser — and
objict's silent-`None` attribute access would turn `finding.mesage` into a field
that quietly vanishes from the response instead of an AttributeError at the line
that typed it wrong. Raw boto3 responses, which are deep and read-only, are
wrapped in `objict` elsewhere in this package; these are not.

THE SEVEN STATUSES, and why each one exists separately:

    PASS      the account already matches the spec. Nothing to do.
    MISSING   the resource does not exist. `apply` will create it.
    DRIFT     it exists, differs, and the difference is on a field AWS lets you
              modify in place. `apply` will modify it.
    MANUAL    it exists, differs, and the difference is on a field AWS makes
              immutable after creation — a target group's protocol, a subnet's
              CIDR, an RDS DBName, a security group's name. There is no modify
              call to make; attempting one raises ValidationError. The remedy
              names what a human has to do, and `apply` does not touch it.
    PENDING   it exists but AWS is still building it. Not an error and not a
              gap: an Aurora cluster reports `creating` for ten minutes. Steps
              that depend on a PENDING resource are SKIPPED, not BLOCKED, and
              the next `apply` picks them up.
    BLOCKED   this step never ran, because something it depends on failed.
              Emitted by `plan`, never by an ensure function.
    BLIND     the call could not be made — denied, throttled, or a credential
              that AWS rejected. This is NOT "fine": a converge that reports a
              clean section it was never allowed to read is worse than one that
              refuses to run, so BLIND fails its step and blocks dependents.
"""

from mojo.deploy.check_setup import (
    DENIED_CODES, THROTTLE_CODES, UNUSABLE_CODES,
)


PASS = "PASS"
DRIFT = "DRIFT"
MISSING = "MISSING"
BLOCKED = "BLOCKED"
BLIND = "BLIND"
PENDING = "PENDING"
MANUAL = "MANUAL"

STATUSES = (PASS, PENDING, MISSING, DRIFT, MANUAL, BLOCKED, BLIND)

# How bad each status is, for `Report.worst()`. PENDING sorts above PASS but
# below everything else: it is the normal state of a five-minute resource.
# MISSING outranks DRIFT because a resource that is not there yet is further
# from converged than one that is there and needs a setting changed.
SEVERITY = {
    PASS: 0,
    PENDING: 1,
    DRIFT: 2,
    MISSING: 3,
    MANUAL: 4,
    BLOCKED: 5,
    BLIND: 6,
}

# A step carrying any of these has not converged and cannot be trusted to have
# produced usable values for the steps that follow it.
BLOCKING_STATUSES = (BLIND, BLOCKED)


class Finding:
    """One observation about one resource.

    `step`    the DAG step that produced it ("network", "db", ...)
    `status`  one of STATUSES
    `code`    a stable, greppable identifier ("vpc.missing", "db.pending")
    `message` one sentence, for a human, naming the resource
    `remedy`  what to do about it, or None when there is nothing to do
    """

    __slots__ = ("step", "status", "code", "message", "remedy")

    def __init__(self, step, status, code, message, remedy=None):
        if status not in SEVERITY:
            raise ValueError(f"unknown finding status: {status!r}")
        self.step = step
        self.status = status
        self.code = code
        self.message = message
        self.remedy = remedy

    def as_dict(self):
        return {"step": self.step, "status": self.status, "code": self.code,
                "message": self.message, "remedy": self.remedy}

    def __repr__(self):
        return f"<Finding {self.status} {self.step}/{self.code}>"


class Action:
    """Something `apply` did, or would do at `apply=False`.

    The verb is the shape of the change, not the boto3 method name, so a reader
    can scan a plan without knowing the SDK: "create", "modify", "tag",
    "attach", "register", "write".
    """

    __slots__ = ("step", "verb", "target", "detail")

    def __init__(self, step, verb, target, detail=None):
        self.step = step
        self.verb = verb
        self.target = target
        self.detail = detail

    def as_dict(self):
        return {"step": self.step, "verb": self.verb, "target": self.target,
                "detail": self.detail}

    def __repr__(self):
        return f"<Action {self.verb} {self.target}>"


class Result:
    """The values a step resolved, for the steps that come after it.

    Subscript access raises KeyError on a name that was never set, which is the
    entire point of this not being a dict-with-attributes: `result["subnet_ids"]`
    when the step actually stored `public_subnet_ids` must fail loudly at the
    line that read it, not hand back None and fail four steps later inside a
    boto3 request that got `SubnetId=None`.
    """

    def __init__(self, **values):
        self.values = dict(values)

    def __getitem__(self, key):
        return self.values[key]

    def __contains__(self, key):
        return key in self.values

    def __len__(self):
        return len(self.values)

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value
        return value

    def update(self, **values):
        self.values.update(values)
        return self

    def as_dict(self):
        return dict(self.values)

    def __repr__(self):
        return f"<Result {sorted(self.values)}>"


class Report:
    """Every finding and action from a whole run, in the order produced."""

    def __init__(self, findings=None, actions=None):
        self.findings = list(findings or ())
        self.actions = list(actions or ())

    def add(self, findings=None, actions=None):
        self.findings.extend(findings or ())
        self.actions.extend(actions or ())
        return self

    def counts(self):
        totals = {status: 0 for status in STATUSES}
        for finding in self.findings:
            totals[finding.status] += 1
        return totals

    def worst(self):
        """The highest-severity status present, or PASS for an empty report."""
        if not self.findings:
            return PASS
        return max((f.status for f in self.findings), key=lambda s: SEVERITY[s])

    def is_blocking(self):
        """True when something in here means the run cannot be trusted.

        PENDING is deliberately NOT blocking. A half-built Aurora cluster is the
        expected state five minutes into a fresh account, and a caller that
        treated it as failure would tell an operator their bootstrap broke when
        the correct advice is "run it again in ten minutes".
        """
        return any(f.status in BLOCKING_STATUSES for f in self.findings)

    def by_step(self, step):
        return [f for f in self.findings if f.step == step]

    def as_dict(self):
        return {
            "findings": [f.as_dict() for f in self.findings],
            "actions": [a.as_dict() for a in self.actions],
            "counts": self.counts(),
            "worst": self.worst(),
            "blocking": self.is_blocking(),
        }


def safe(findings, step, name, func, default=None):
    """Run an AWS call, turning a permission or credential failure into a BLIND
    finding instead of a traceback.

    Ported from `mojo.deploy.check_setup.safe`, which this imports its code
    tuples from rather than retyping them — the audit tool and the provisioner
    must agree on what "you cannot see this" looks like, and two copies of that
    list would drift.

    One deliberate difference from the audit's version: an unexpected ClientError
    is BLIND here, where the audit records it as INFO. The audit is describing an
    account; this is changing one. An error nobody classified means the step did
    not do what it claimed, and every step downstream of it must be blocked
    rather than run against values that were never resolved.
    """
    from botocore.exceptions import (BotoCoreError, ClientError,
                                     EndpointConnectionError,
                                     NoCredentialsError,
                                     PartialCredentialsError)

    try:
        return func()
    except ClientError as err:
        code = err.response.get("Error", {}).get("Code", "")
        if code in DENIED_CODES:
            findings.append(Finding(
                step, BLIND, f"{name}.denied",
                f"the provisioning credential cannot call {name} ({code}), so "
                f"this was not done",
                "grant the provisioning credential this action, then re-run"))
        elif code in THROTTLE_CODES:
            findings.append(Finding(
                step, BLIND, f"{name}.throttled",
                f"AWS throttled {name} ({code}), so this was not done",
                "re-run — the next pass picks up where this one stopped"))
        elif code in UNUSABLE_CODES:
            findings.append(Finding(
                step, BLIND, f"{name}.credential",
                f"AWS rejected the provisioning credential on {name} ({code})",
                "check the credential is active and not expired, then re-run"))
        else:
            findings.append(Finding(
                step, BLIND, f"{name}.error",
                f"{name} failed: {err}",
                "read the AWS error above; re-run once it is addressed"))
        return default
    except BotoCoreError as err:
        if isinstance(err, (NoCredentialsError, PartialCredentialsError,
                            EndpointConnectionError)):
            findings.append(Finding(
                step, BLIND, f"{name}.unreachable",
                f"{name} could not be attempted ({type(err).__name__})",
                "confirm the credential and network reachability, then re-run"))
        else:
            findings.append(Finding(
                step, BLIND, f"{name}.error",
                f"{name} failed: {err}",
                "read the AWS error above; re-run once it is addressed"))
        return default


def existing(step, code, message):
    return Finding(step, PASS, code, message)


def missing(step, code, message, remedy=None):
    return Finding(step, MISSING, code, message, remedy)


def drift(step, code, message, remedy=None):
    return Finding(step, DRIFT, code, message, remedy)


def manual(step, code, message, remedy):
    """AWS will not let this be changed in place. Say so; never try."""
    return Finding(step, MANUAL, code, message, remedy)


def pending(step, code, message, remedy=None):
    """Not ready YET. The remedy defaults to the one that is nearly always true.

    A caller passes its own only when "re-run once it reports ready" would send
    an operator to the wrong place — an instance that is still `pending` is
    waiting on EC2, not on the resource the finding names.
    """
    return Finding(step, PENDING, code, message, remedy or
                   "AWS is still building this — re-run once it reports ready")
