"""Fleet drift: what is actually serving traffic vs. what the portal recorded.

Read-only. Answers one question nobody can answer from a dashboard: *did
anything change outside this portal?* It compares the instances registered
behind the load balancers with the node ids recorded in the protected
``EDGE_EXPECTED_TOPOLOGY`` setting, in BOTH directions, and reports the
difference as operator-facing prose.

NO MUTATING AWS CALL MAY APPEAR IN THIS MODULE. It observes and reports; every
remediation it names is something a person does.

Two categories, on purpose (the same split as
``mojo.apps.aws.services.version_drift``):

* ``CATEGORY`` (``system:health:infra_drift``) is the EVENT category, so the
  finding lands on the ``/api/incident/health/summary`` strip with the rest of
  the subsystem health rows.
* ``RULESET_CATEGORY`` (``infra:drift``) is the RuleSet category, matched
  through the event's ``scope``. It is deliberately outside the
  ``system:health:`` namespace so it can never satisfy the
  ``_ensure_health_defaults()`` bootstrap guard in incident cronjobs.

This signal registers NO System Setup readiness section, deliberately. It warns
on a *correct* installation (an unrecorded node in an externally-managed estate
is expected), and ``system_setup`` only calls a fix operation successful when
``overall == "pass"`` — a permanently-warn section would break the portal's
unscoped "Fix all" forever.

Two traps this module exists on the far side of:

**``except NoCredentialsError`` never fires here.** The AWS reads go through
``ProviderCaller.call``, which catches ``Exception`` and re-raises
``ProviderCallError``. Credential and endpoint failures arrive as
``provider_code`` ``credentials_unavailable`` / ``network_unavailable``, so the
branch is on the CODE, never on the exception type. Getting that wrong would
make ``status="unavailable"`` unreachable and file a level-4 event from every
credential-less box — which is every dev machine and every suite run.
``ProviderCallError.detail()`` also omits ``iam_action`` unless ``denied`` is
True, so the action is read off the exception attribute, never off ``detail()``.

**The hostname comparison is mismatched on both raw sides.** Topology node ids
come from ``job_engine.host_channel()`` — ``gethostname()`` lowercased with
``.``/``_`` turned into ``-``, DOMAIN KEPT. ``facts["private_hostname"]`` is the
private DNS name with the domain STRIPPED. A box whose ``gethostname()`` returns
``ip-10-0-1-23.ec2.internal`` is therefore recorded as
``ip-10-0-1-23-ec2-internal`` and observed as ``ip-10-0-1-23``: raw equality
misses every time and false-positives daily. Both sides go through ``_norm()``,
the one normalization in this module, and ``private_dns_name`` is compared under
it too.
"""

from django.utils import timezone

from mojo.apps.account.services import system_settings
from mojo.helpers import infrastructure
from mojo.helpers import logit
from mojo.helpers.aws import ec2 as ec2_helper
from mojo.helpers.aws import elbv2 as elbv2_helper
from mojo.helpers.aws.client import get_client, get_session
from mojo.helpers.aws.provider_call import ProviderCallError
from mojo.helpers.settings import settings


logger = logit.get_logger("aws_infra_drift", "aws.log")

SCHEMA_VERSION = 1
# The EVENT category — keeps the health-strip row.
CATEGORY = "system:health:infra_drift"
# The RULESET category — matched via Event.scope. NOT in the health namespace.
RULESET_CATEGORY = "infra:drift"

# Mirrors elbv2.serving_map's own cap: it silently drops target groups past
# this many, BEFORE any counter here could see them.
MAX_GROUPS = 20
# Mirrors ec2.instance_map's ids[:100] slice, same reason.
MAX_INSTANCE_IDS = 100

# The provider codes that mean "this box cannot talk to AWS at all". Neither is
# drift, and neither is worth a daily event on a laptop.
UNAVAILABLE_CODES = ("credentials_unavailable", "network_unavailable")

ELBV2_IAM_ACTION = "elasticloadbalancing:DescribeTargetGroups"
EC2_IAM_ACTION = "ec2:DescribeInstances"

CAPACITY_TAG_VALUE = "admin-capacity"

REASON_UNRECORDED = "unrecorded_node"
REASON_CAPACITY_ADDED = "capacity_added_not_recorded"
REASON_UNSERVING = "node_unserving"

# Who each finding actually costs something. Keyed by reason so the prose stays
# in one place and the finding row stays a closed set of fields.
WHO_IS_AFFECTED = {
    REASON_UNRECORDED: (
        "Who is affected: production traffic is being served by a box the "
        "portal does not track, so fleet readiness, deploys and pool "
        "convergence all skip it — a broken release on this node will not show "
        "up in System Setup."),
    REASON_CAPACITY_ADDED: (
        "Who is affected: production traffic is being served by a box the "
        "portal does not track, so fleet readiness, deploys and pool "
        "convergence all skip it — a broken release on this node will not show "
        "up in System Setup."),
    REASON_UNSERVING: (
        "Who is affected: this node believes it is serving and nothing is "
        "reaching it. Deploys and readiness still cover it, so nothing else "
        "will turn yellow."),
}


def _setting(name, default=None, kind=None):
    try:
        return settings.get_static(name, default, kind=kind) if kind else settings.get_static(name, default)
    except Exception:
        return default


def _norm(value):
    """THE normalization. Both sides of every comparison go through this.

    ``host_channel()`` keeps the domain and turns ``.``/``_`` into ``-``;
    ``private_hostname`` drops the domain. Running both through the same
    transform is what makes ``ip-10-0-1-23.ec2.internal`` and
    ``ip-10-0-1-23-ec2-internal`` comparable at all — and why
    ``private_dns_name`` is a candidate alongside the short hostname.
    """
    return str(value or "").strip().lower().replace(".", "-").replace("_", "-")


def _last_label(value):
    """The final ``-``-delimited label of a normalized identifier."""
    normalized = _norm(value)
    return normalized.rsplit("-", 1)[-1] if normalized else ""


def _groups_phrase(names):
    """``target group a`` / ``target groups a and b`` / ``a, b and c``."""
    clean = [str(name) for name in (names or []) if name]
    if not clean:
        return "a load balancer target group"
    if len(clean) == 1:
        return f"target group {clean[0]}"
    return f"target groups {', '.join(clean[:-1])} and {clean[-1]}"


def _label(instance_id, name, private_hostname):
    """``i-0abc ("web-04", ip-10-0-3-17)`` — whichever parts actually exist."""
    extras = []
    if name and name != instance_id:
        extras.append(f'"{name}"')
    if private_hostname:
        extras.append(str(private_hostname))
    if not extras:
        return str(instance_id or "this instance")
    return f"{instance_id} ({', '.join(extras)})"


class InfraDriftScanner:
    """Compare the serving fleet with the recorded fleet, both directions."""

    def __init__(self, region=None, profile=None, timeout=5, elbv2_client=None,
                 ec2_client=None, now=None):
        self.region = region or _setting("AWS_REGION", "us-east-1")
        self.profile = profile
        self.timeout = max(1, min(int(timeout or 5), 30))
        self.elbv2_client = elbv2_client
        self.ec2_client = ec2_client
        self.now = now or timezone.now
        self.warnings = []

    # ── AWS plumbing (mirrors VersionDriftScanner's injection contract) ──

    def _session(self, region=None):
        access_key = secret_key = None
        if not self.profile:
            access_key = _setting("AWS_KEY")
            secret_key = _setting("AWS_SECRET")
        return get_session(
            access_key=access_key, secret_key=secret_key, region=region or self.region,
            profile=self.profile if not access_key and not secret_key else None,
        )

    def _client(self, service, injected=None, region=None):
        if injected is not None:
            return injected
        return get_client(
            service, session=self._session(region),
            region=region or self.region, timeout=self.timeout,
        )

    def _warn(self, kind, message, **extra):
        warning = {"kind": kind, "message": message}
        warning.update(extra)
        self.warnings.append(warning)
        logger.warning("infra drift: %s — %s", kind, message)

    def _warn_provider(self, err, fallback_action):
        """Record a failed AWS read by its EXACT missing IAM action.

        ``detail()`` carries ``iam_action`` ONLY when the failure was a denial,
        so the action is taken off the exception attribute (or the literal this
        module knows it asked for) rather than out of the dict.
        """
        detail = err.detail()
        action = err.iam_action or fallback_action
        self._warn(
            "read_failed",
            f"{action} did not answer ({detail.get('provider_code')}); "
            f"fleet drift could not be compared from this read",
            iam_action=action,
            provider_code=detail.get("provider_code"),
            operation=detail.get("operation"),
        )

    # ── the two AWS reads ──

    def _topology(self):
        try:
            return system_settings.get_value(system_settings.EXPECTED_EDGE_TOPOLOGY)
        except Exception:
            logger.exception("infra drift: EDGE_EXPECTED_TOPOLOGY could not be read")
            return None

    def _serving(self):
        return elbv2_helper.serving_map(
            client=self._client("elbv2", self.elbv2_client),
            region=self.region, max_groups=MAX_GROUPS)

    def _instances(self, ids):
        return ec2_helper.instance_map(
            ids, client=self._client("ec2", self.ec2_client), region=self.region)

    # ── comparison ──

    def _registered(self, serving):
        """``{instance_id: [target group name, ...]}`` in first-seen order."""
        registered = {}
        for group in serving.get("groups") or []:
            label = group.get("name") or group.get("arn")
            for target in group.get("targets") or []:
                identifier = target.get("id")
                if not identifier or not str(identifier).startswith("i-"):
                    continue
                names = registered.setdefault(identifier, [])
                if label and label not in names:
                    names.append(label)
        return registered

    def _candidates(self, instance_id, facts):
        """The normalized identities one live instance could be recorded as."""
        return {
            _norm(facts.get("private_hostname")),
            _norm(facts.get("private_dns_name")),
            _norm(facts.get("name")),
        } - {""}

    def _suggested_node_id(self, instance_id, facts):
        """A HINT, not the authoritative node id.

        ``capacity.expected_node_id`` needs the fleet's ``base_name``, which
        this scanner never sees, so this is the Name tag plus the instance-id
        suffix — the same shape, an unverified base. The authoritative id for a
        stock EC2 box is its ``private_hostname``; the honest field name says
        this one is only a suggestion.
        """
        base = _norm(facts.get("name"))
        if not base or base == _norm(instance_id):
            return None
        suffix = str(instance_id or "").rsplit("-", 1)[-1]
        return f"{base}-{suffix}" if suffix else base

    def _preferred_name(self, instance_id, facts):
        """What the operator should actually type into Expected fleet."""
        return (facts.get("private_hostname")
                or self._suggested_node_id(instance_id, facts)
                or instance_id)

    def _forward(self, registered, facts_map, recorded, mode):
        """Serving instances that no recorded node id matches."""
        findings = []
        matched_nodes = set()
        for instance_id, group_names in registered.items():
            facts = facts_map.get(instance_id) or {}
            hits = {node for node in recorded if node in self._candidates(instance_id, facts)}
            # The suffix identity: a recorded node whose FINAL label is exactly
            # the instance id's final label. Exact equality of the last label,
            # so `web-<iid>` can never be satisfied by `api-<iid>`.
            suffix = _last_label(instance_id)
            if suffix:
                hits |= {node for node in recorded if _last_label(node) == suffix}
            if hits:
                matched_nodes |= hits
                continue
            added_by_capacity = (facts.get("tags") or {}).get(
                ec2_helper.CREATED_BY_TAG) == CAPACITY_TAG_VALUE
            findings.append(self._finding(
                reason=REASON_CAPACITY_ADDED if added_by_capacity else REASON_UNRECORDED,
                instance_id=instance_id, facts=facts,
                target_groups=list(group_names), added_by_capacity=added_by_capacity,
                mode=mode))
        return findings, matched_nodes

    def _reverse(self, recorded, matched_nodes, mode):
        """Recorded nodes that no registered instance matched.

        Computed from RECORDED names only — an opaque hand-built node id is
        fine here and no identity has to be derived from AWS, which is what
        makes this direction safe when the forward one needs four candidates.
        """
        findings = []
        for node in sorted(recorded - matched_nodes):
            findings.append(self._finding(
                reason=REASON_UNSERVING, instance_id=None,
                facts={"name": node}, target_groups=[],
                added_by_capacity=False, mode=mode))
        return findings

    # ── one finding row ──

    def _finding(self, reason, instance_id, facts, target_groups, added_by_capacity, mode):
        name = facts.get("name")
        private_hostname = facts.get("private_hostname") or ""
        if reason == REASON_UNSERVING:
            note = (f'"{name}" is in the portal\'s recorded fleet but is not '
                    f"registered behind any load balancer target group, so it is "
                    f"receiving no traffic.")
            preferred = name
        else:
            label = _label(instance_id, name, private_hostname)
            preferred = self._preferred_name(instance_id, facts)
            groups = _groups_phrase(target_groups)
            if reason == REASON_CAPACITY_ADDED:
                note = (f"{label} was added by this portal's Capacity panel "
                        f"(tag {ec2_helper.CREATED_BY_TAG}={CAPACITY_TAG_VALUE}) and is "
                        f"registered in {groups}, but it is not in "
                        f"EDGE_EXPECTED_TOPOLOGY — the topology record was not "
                        f"updated when the node was added.")
            else:
                note = (f"{label} is registered in {groups} and is answering "
                        f"requests, but no node in EDGE_EXPECTED_TOPOLOGY matches it.")
        return {
            "instance_id": instance_id,
            "name": name,
            "private_hostname": private_hostname,
            "instance_state": facts.get("state"),
            "target_groups": list(target_groups),
            "added_by_capacity": bool(added_by_capacity),
            "reason": reason,
            "suggested_node_id": (None if reason == REASON_UNSERVING
                                  else self._suggested_node_id(instance_id, facts)),
            "note": note,
            "remediation": self._remediation(reason, preferred, target_groups, mode),
        }

    def _remediation(self, reason, preferred, target_groups, mode):
        """The `What to do:` sentence. External mode reframes ONLY this.

        Same level, same finding count, same rows — an externally-managed
        estate is not less drifted, it is drifted for a reason the operator
        already knows, and the action is to record the node rather than to
        remove it.
        """
        if mode == infrastructure.EXTERNAL:
            return ("What to do: this installation is INFRASTRUCTURE_MODE=external, "
                    "so your infrastructure team's pipeline owns this node, not the "
                    f'portal — its presence is expected. Record "{preferred}" in '
                    "System Setup > Expected fleet so the portal's fleet readiness "
                    "covers it. The portal will not change anything in AWS either way.")
        if reason == REASON_UNSERVING:
            return ("What to do: register it behind the api target group if it "
                    f'should be serving, or remove "{preferred}" from System Setup > '
                    "Expected fleet if it is retired. Nothing here changes AWS for you.")
        return ("What to do: if this node belongs to the fleet, add "
                f'"{preferred}" to System Setup > Expected fleet. If it does not, '
                f"deregister it from {_groups_phrase(target_groups)}. "
                "Nothing here changes AWS for you.")

    # ── report ──

    def _level(self, findings, warnings):
        """1 = matched; 4 = a read did not answer; 5 = drift found. Never above 5.

        Level 1 files NOTHING (see the asyncjob): the catch-all RuleSet matches
        ``level >= 1`` with no handler, so an "all clear" event would manufacture
        a permanent Incident on every run.
        """
        level = 1
        if warnings:
            level = 4
        if findings:
            level = 5
        return level

    def scan(self):
        self.warnings = []
        mode = infrastructure.infrastructure_mode()
        report = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": self.now().isoformat(),
            "region": self.region,
            "mode": mode,
            "status": "ok",
            "level": 1,
            "findings": [],
            "warnings": [],
        }

        # FIRST, before any AWS call: with no recorded intent every single node
        # is "drift", which is a daily level-5 event that says nothing.
        topology = self._topology()
        if not topology or not topology.get("nodes"):
            report["status"] = "unconfigured"
            report["reason"] = ("EDGE_EXPECTED_TOPOLOGY records no nodes, so there is "
                                "no recorded fleet to compare against")
            return report

        try:
            serving = self._serving()
        except ProviderCallError as err:
            if err.provider_code in UNAVAILABLE_CODES:
                report["status"] = "unavailable"
                report["reason"] = err.provider_code
                return report
            self._warn_provider(err, ELBV2_IAM_ACTION)
            report["warnings"] = list(self.warnings)
            report["level"] = self._level([], self.warnings)
            return report

        groups = serving.get("groups") or []
        if len(groups) >= MAX_GROUPS:
            self._warn(
                "groups_truncated",
                f"only the first {MAX_GROUPS} attached target groups were read; "
                f"any node serving solely behind a later group cannot be compared")

        registered = self._registered(serving)
        ids = list(registered.keys())
        if not ids:
            # No instance targets at all is not drift — it is an installation
            # whose balancers front something else, or none.
            report["warnings"] = list(self.warnings)
            report["level"] = self._level([], self.warnings)
            return report

        if len(ids) > MAX_INSTANCE_IDS:
            unchecked = len(ids) - MAX_INSTANCE_IDS
            self._warn(
                "instance_truncated",
                f"{unchecked} registered instance(s) past the first "
                f"{MAX_INSTANCE_IDS} were not described and were not compared")
            ids = ids[:MAX_INSTANCE_IDS]
            registered = {key: registered[key] for key in ids}

        try:
            facts_map = self._instances(ids)
        except ProviderCallError as err:
            if err.provider_code in UNAVAILABLE_CODES:
                report["status"] = "unavailable"
                report["reason"] = err.provider_code
                return report
            # Without instance facts the only usable identity is the suffix, and
            # every hostname-recorded node would read as drift. Warn; invent
            # nothing.
            self._warn_provider(err, EC2_IAM_ACTION)
            report["warnings"] = list(self.warnings)
            report["level"] = self._level([], self.warnings)
            return report

        recorded = {_norm(node) for node in topology.get("nodes") or []} - {""}
        findings, matched = self._forward(registered, facts_map, recorded, mode)
        findings += self._reverse(recorded, matched, mode)

        report["findings"] = findings
        report["warnings"] = list(self.warnings)
        report["level"] = self._level(findings, self.warnings)
        return report


def scan(**kwargs):
    """Convenience wrapper — build a scanner from settings and run it."""
    return InfraDriftScanner(**kwargs).scan()
