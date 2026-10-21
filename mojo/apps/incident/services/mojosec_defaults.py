"""Create-only MojoSec policies and revision-bound legacy visibility repair."""
import hashlib
import json

from django.db import connection, transaction

from . import rule_validation


VERSION = 1
MARKER = "mojosec_default_version"
LEGACY_KINDS = (
    "auth.ssh_login", "auth.sudo_command", "auth.sudo_failure", "system.oom",
    "system.service_error", "web.probe", "fim.change", "fim.overflow",
    "fim.expected_change_error",
)


def policies():
    """The shipped, governed policy vocabulary; no arbitrary handler strings."""
    from .mojosec import KIND_POLICY

    rows = []

    def add(kind, label, priority, handlers, rules=(), source=False):
        rows.append({
            "name": "MojoSec - " + label, "category": "mojosec." + kind,
            "priority": priority, "match_by": 0,
            "bundle_by": 4 if source else 3, "bundle_minutes": 60,
            "bundle_by_rule_set": True, "handlers": handlers,
            "rules": list(rules), "is_active": True,
            "delete_on_resolution": False,
        })

    def condition(field, value, operator="=="):
        return {"field": field, "operator": operator, "value": value}

    bounded = condition("level", 8, "<=")
    for kind, label in (
            ("auth.ssh_login", "Routine public-key SSH"),
            ("auth.sudo_command", "Proven local command"),
            ("auth.session_open", "Proven local session")):
        add(kind, label, 10, [{"type": "ignore"}],
            [bounded, condition("mojosec_routine", True)])
    add("fim.change", "Registered expected file change", 10,
        [{"type": "ignore"}],
        [bounded, condition("mojosec_expected_change", True)])
    add("web.probe", "Repeated sensitive-path scanner", 5,
        [{"type": "block", "ttl_seconds": 3600, "fleet_wide": True}],
        [bounded, condition("mojosec_scanner_confirmed", True)], source=True)
    add("web.probe", "Redirected or refused probe", 10,
        [{"type": "ignore"}], [bounded,
            condition("mojosec_status", 300, ">="),
            condition("mojosec_status", 500, "<")])
    for kind in (*KIND_POLICY, "unrecognized"):
        add(kind, "Review " + kind, 100, [], source=kind.startswith("web."))
    return rows


def _legacy_visibility(row):
    """An exact, unedited consumer visibility default, never a name heuristic."""
    return (
        row.category in {"mojosec." + kind for kind in LEGACY_KINDS} and
        row.name == "WMWX " + row.category and
        row.metadata == {"managed_by": "wmx", "purpose": "mojosec_incident_visibility"} and
        row.priority == 20 and row.match_by == 0 and
        row.bundle_by == 1 and row.bundle_minutes == 60 and
        row.bundle_by_rule_set is True and not row.handler and
        row.trigger_count is None and row.trigger_window is None and
        row.retrigger_every is None and not list(row.rules.all()))


def _plan(repair_visibility=False):
    from mojo.apps.incident.models import RuleSet

    definitions = policies()
    names = {row["name"] for row in definitions}
    categories = {row["category"] for row in definitions}
    stored = list(RuleSet.objects.filter(category__in=categories).order_by("pk").prefetch_related("rules"))
    by_category = {}
    snapshots = []
    for row in stored:
        row._admin_security_rules = sorted(
            row.rules.all(), key=lambda rule: (rule.index, rule.pk))
        by_category.setdefault(row.category, []).append(row)
        snapshots.append({
            "id": row.pk, "modified": row.modified.isoformat(),
            "policy": rule_validation.ruleset_payload(row)
            if rule_validation.parse_handlers(row.handler) is not None else {
                "name": row.name, "category": row.category, "handler": row.handler},
            "metadata": row.metadata,
        })
    create = []
    retire = []
    preserved = []
    for category in sorted(categories):
        existing = by_category.get(category, [])
        foreign = [row for row in existing if not (
            row.name in names and isinstance(row.metadata, dict) and
            row.metadata.get(MARKER) == VERSION)]
        legacy = [row for row in foreign if _legacy_visibility(row)]
        if foreign and not (repair_visibility and len(legacy) == len(foreign)):
            preserved.append({"category": category, "ids": [row.pk for row in foreign],
                              "reason": "existing_operator_policy"})
            continue
        if repair_visibility:
            retire.extend(row.pk for row in legacy if row.is_active)
        for definition in definitions:
            if definition["category"] != category:
                continue
            if not any(row.name == definition["name"] for row in existing):
                create.append(definition)
    state = {"version": VERSION, "repair_visibility": repair_visibility,
             "existing": snapshots, "create": create, "retire": retire,
             "preserved": preserved}
    revision = hashlib.sha256(json.dumps(
        state, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    return {"revision": revision, "create": create, "retire": retire,
            "preserved": preserved}


def setup_defaults(*, apply=False, repair_visibility=False, expected_revision=None):
    """Preview by default. Explicit repair is bound to the previewed revisions."""
    from mojo.apps.incident.models import Rule, RuleSet

    with transaction.atomic():
        if apply and connection.vendor == "postgresql":
            # No natural unique key exists on legacy RuleSets. A short table
            # lock serializes setup with other seeds and policy mutations.
            with connection.cursor() as cursor:
                cursor.execute("LOCK TABLE " + connection.ops.quote_name(
                    RuleSet._meta.db_table) + " IN EXCLUSIVE MODE")
        plan = _plan(repair_visibility)
        if not apply:
            return plan
        if repair_visibility and not expected_revision:
            raise ValueError("Visibility repair requires the preview revision")
        if expected_revision is not None and expected_revision != plan["revision"]:
            raise ValueError("Rule policies changed; preview again before applying")
        for pk in plan["retire"]:
            row = RuleSet.objects.get(pk=pk)
            row.is_active = False
            row.save(update_fields=["is_active", "modified"])
        created = []
        for definition in plan["create"]:
            normalized = rule_validation.normalize_ruleset(definition)
            rules = normalized.pop("rules")
            metadata = rule_validation.mark_governed(normalized.pop("metadata"))
            metadata[MARKER] = VERSION
            row = RuleSet.objects.create(metadata=metadata, **normalized)
            for rule in rules:
                Rule.objects.create(parent=row, **rule)
            created.append(row.pk)
        return {**plan, "created": created, "applied": True}


def ensure_defaults():
    """Create missing defaults, preserving every existing policy and edit."""
    plan = setup_defaults()
    if not plan["create"]:
        return plan
    return setup_defaults(apply=True)


def after_migrate(sender, using="default", **kwargs):
    if using == "default":
        ensure_defaults()
