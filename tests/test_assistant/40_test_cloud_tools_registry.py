"""The cloud domain's registry declarations, against their Admin twins.

Pure and DB-light on purpose: no AWS, no patching, no settings. The invariant
is the epic's — a tool is never easier to reach through chat than through the
portal — so every assertion here is "this tool declares what its twin
declares", spelled out per tool rather than derived, because a table that
derives its expectation from the code under test proves nothing.
"""

from testit import helpers as th
from testit.helpers import assert_eq, assert_true


AWS_EMAIL = "cloud-registry-aws@example.com"
PLATFORM_EMAIL = "cloud-registry-platform@example.com"
ROOT_EMAIL = "cloud-registry-root@example.com"
TEST_PASSWORD = "TestPass1!"

READ_TOOLS = (
    "get_platform_health", "get_platform_overview", "get_advanced_inventory",
    "get_framework_status", "get_fleet_capacity",
    "get_capacity_operation_status", "get_managed_upgrades",
    "get_upgrade_status", "get_setup_readiness", "get_setup_operation",
    "get_version_drift", "list_cloud_resources", "fetch_cloud_metrics",
)

# tool -> (permission, requires_superuser, requires_managed_infrastructure)
MUTATING_TOOLS = {
    # No infrastructure.refuse() on the three deploy endpoints, so no gate here.
    "retry_platform_deployment": (["manage_platform", "admin"], False, False),
    "verify_platform_deployment": (["manage_platform", "admin"], False, False),
    "converge_platform_deployment": (["manage_platform", "admin"], False, False),
    "apply_framework_update": (["manage_platform", "admin"], False, True),
    "apply_managed_upgrade": ("manage_aws", False, True),
    "apply_capacity_change": ("manage_aws", True, True),
    "apply_capacity_plan": ("manage_aws", True, True),
}

# The two reads whose twins demand an active literal superuser.
SUPERUSER_READS = ("get_setup_readiness", "get_setup_operation")

EXPECTED_ACTIONS_BY_STATUS = {
    "failed": ("retry", "verify"),
    "verified": ("verify", "converge"),
    "partial": ("verify", "converge"),
    "unknown": ("verify", "converge"),
    "converged": ("verify",),
}
EXPECTED_ACTIVE_STATUSES = {"requested", "canary", "fleet"}


def _cloud_entries():
    from mojo.apps.assistant import get_registry

    return {name: entry for name, entry in get_registry().items()
            if entry["domain"] == "cloud"}


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_cloud_registry(opts):
    from mojo.apps.account.models import User

    User.objects.filter(
        email__in=[AWS_EMAIL, PLATFORM_EMAIL, ROOT_EMAIL]).delete()

    def make(email, perms, superuser=False):
        user = User.objects.create_user(
            username=email, email=email, password=TEST_PASSWORD)
        user.is_email_verified = True
        if superuser:
            user.is_superuser = True
        user.save()
        for perm in perms:
            user.add_permission(perm)
        return user

    # manage_aws only — no platform tier, not a superuser.
    opts.aws = make(AWS_EMAIL, ["view_admin", "assistant", "manage_aws"])
    # The platform tier, still not a superuser.
    opts.platform = make(PLATFORM_EMAIL,
                         ["view_admin", "assistant", "manage_platform", "admin"])
    opts.root = make(ROOT_EMAIL, ["view_admin", "assistant"], superuser=True)


# ---------------------------------------------------------------------------
# Registration shape
# ---------------------------------------------------------------------------

@th.django_unit_test("the cloud domain registers exactly its twenty tools, none core")
def test_cloud_domain_roster(opts):
    from mojo.apps.assistant import DOMAIN_DESCRIPTIONS

    entries = _cloud_entries()
    expected = set(READ_TOOLS) | set(MUTATING_TOOLS)
    assert_eq(set(entries), expected,
              f"the cloud roster drifted: {sorted(set(entries) ^ expected)}")
    assert_eq(len(entries), 20, f"expected 20 cloud tools, got {len(entries)}")
    core = [name for name, entry in entries.items() if entry["core"]]
    assert_eq(core, [],
              f"cloud tools are on-demand; these declared core=True: {core}")
    assert_true("cloud" in DOMAIN_DESCRIPTIONS,
                "the cloud domain has no DOMAIN_DESCRIPTIONS entry, so "
                "load_tools cannot describe it")


@th.django_unit_test("read tools declare no approval gate")
def test_read_tools_declare_no_gate(opts):
    entries = _cloud_entries()
    for name in READ_TOOLS:
        entry = entries[name]
        assert_eq(entry["mutates"], False, f"{name} must not be mutating")
        assert_eq(entry["fresh_auth_seconds"], None,
                  f"{name} declares fresh_auth_seconds on a read tool")
        assert_eq(entry["requires_superuser"], False,
                  f"{name} declares requires_superuser on a read tool")
        assert_eq(entry["requires_managed_infrastructure"], False,
                  f"{name} declares requires_managed_infrastructure on a read tool")
        assert_eq(entry["summarize"], None, f"{name} declares summarize on a read")
        assert_eq(entry["preview"], None, f"{name} declares preview on a read")
        expected_authorize = name in SUPERUSER_READS
        assert_eq(bool(entry["authorize"]), expected_authorize,
                  f"{name} authorize={bool(entry['authorize'])}, "
                  f"expected {expected_authorize}")


@th.django_unit_test("every mutating cloud tool declares its twin's gates exactly")
def test_mutating_tools_mirror_their_twins(opts):
    entries = _cloud_entries()
    for name, (permission, superuser, infra) in MUTATING_TOOLS.items():
        entry = entries[name]
        assert_eq(entry["mutates"], True, f"{name} must declare mutates=True")
        assert_eq(entry["permission"], permission,
                  f"{name} permission is {entry['permission']}, expected {permission}")
        assert_eq(entry["fresh_auth_seconds"], 600,
                  f"{name} fresh_auth_seconds is {entry['fresh_auth_seconds']}; "
                  f"every mirrored endpoint carries requires_fresh_auth(600)")
        assert_eq(entry["requires_superuser"], superuser,
                  f"{name} requires_superuser is {entry['requires_superuser']}, "
                  f"expected {superuser}")
        assert_eq(entry["requires_managed_infrastructure"], infra,
                  f"{name} requires_managed_infrastructure is "
                  f"{entry['requires_managed_infrastructure']}, expected {infra} "
                  f"(true exactly where the twin calls infrastructure.refuse())")
        assert_true(callable(entry["summarize"]),
                    f"{name} has no callable summarize, so its card has no sentence")
        assert_true(callable(entry["preview"]),
                    f"{name} has no callable preview, so nothing binds a revision")


@th.django_unit_test("no cloud tool exposes a typed-echo confirmation field")
def test_no_confirm_fields(opts):
    for name, entry in _cloud_entries().items():
        schema = entry["definition"]["input_schema"]
        properties = list((schema.get("properties") or {}).keys())
        echoes = [key for key in properties if key.startswith("confirm")]
        assert_eq(echoes, [],
                  f"{name} exposes {echoes}; the approval card replaces the "
                  f"browser's typed echo and a model retyping a string proves "
                  f"nothing")
        assert_eq(schema.get("type"), "object",
                  f"{name} input_schema is not an object schema")


@th.django_unit_test("bounded inputs use enums or server-derived identifiers")
def test_inputs_are_bounded(opts):
    entries = _cloud_entries()
    from mojo.apps.aws.services import capacity, maintenance
    from mojo.helpers.aws.cloudwatch import ACCOUNT_NAMESPACE, CATEGORY_METRIC

    def enum_of(name, field):
        return (entries[name]["definition"]["input_schema"]["properties"]
                [field].get("enum"))

    assert_eq(set(enum_of("apply_capacity_change", "action")),
              set(capacity.ACTIONS),
              "apply_capacity_change's action enum is not the service's ACTIONS")
    assert_eq(set(enum_of("apply_managed_upgrade", "kind")), set(maintenance.KINDS),
              "apply_managed_upgrade's kind enum is not the service's KINDS")
    assert_eq(set(enum_of("get_upgrade_status", "kind")), set(maintenance.KINDS),
              "get_upgrade_status's kind enum is not the service's KINDS")
    assert_eq(set(enum_of("fetch_cloud_metrics", "account")),
              set(ACCOUNT_NAMESPACE),
              "fetch_cloud_metrics' account enum is not ACCOUNT_NAMESPACE")
    assert_eq(set(enum_of("fetch_cloud_metrics", "category")),
              set(CATEGORY_METRIC),
              "fetch_cloud_metrics' category enum is not CATEGORY_METRIC")
    steps = (entries["apply_capacity_plan"]["definition"]["input_schema"]
             ["properties"]["steps"]["items"]["properties"]["action"]["enum"])
    assert_eq(set(steps), set(capacity.BATCH_ACTIONS),
              "apply_capacity_plan's step actions are not BATCH_ACTIONS")
    required = (entries["get_platform_overview"]["definition"]["input_schema"]
                .get("required"))
    assert_eq(required, ["sections"],
              "get_platform_overview must REQUIRE sections; a bare call "
              "collects all ten, including the per-app HTTPS probes")
    assert_eq(entries["get_setup_readiness"]["definition"]["input_schema"]
              .get("required"), ["section"],
              "get_setup_readiness must REQUIRE section")


# ---------------------------------------------------------------------------
# Listings
# ---------------------------------------------------------------------------

@th.django_unit_test("the cloud domain is offered and existing domains are unchanged")
def test_domain_listing(opts):
    from mojo.apps.assistant import get_available_domains, get_domain_tools_for_user

    domains = get_available_domains(opts.root)
    assert_true("cloud" in domains,
                f"a superuser's domain listing has no cloud entry: {sorted(domains)}")
    assert_eq(domains["cloud"]["count"], 20,
              f"the cloud domain lists {domains['cloud']['count']} tools for a "
              f"superuser, expected 20")
    security = {t["name"] for t in get_domain_tools_for_user(opts.root, ["security"])}
    assert_true("block_ip" in security and "query_incidents" in security,
                f"an existing domain's tools moved: {sorted(security)[:8]}")


@th.django_unit_test("a manage_aws holder who is not a superuser is never offered "
                     "the capacity tools")
def test_capacity_tools_hidden_from_non_superuser(opts):
    from mojo.apps.assistant import (
        get_available_domains, get_domain_tools_for_user, get_tools_for_user,
    )

    listed = {t["name"] for t in get_domain_tools_for_user(opts.aws, ["cloud"])}
    every = {t["name"] for t in get_tools_for_user(opts.aws)}
    domain_tools = set(get_available_domains(opts.aws).get("cloud", {}).get(
        "tools", []))
    for name in ("apply_capacity_change", "apply_capacity_plan"):
        assert_true(name not in listed,
                    f"{name} was offered to a non-superuser in the domain listing")
        assert_true(name not in every,
                    f"{name} was offered to a non-superuser in the full listing")
        assert_true(name not in domain_tools,
                    f"{name} was offered to a non-superuser in get_available_domains")
    assert_true("get_fleet_capacity" in listed,
                f"the capacity READS must stay available to manage_aws: {sorted(listed)}")


@th.django_unit_test("apply_managed_upgrade needs the platform tier as well as manage_aws")
def test_managed_upgrade_requires_the_manage_tier(opts):
    from mojo.apps.assistant import get_domain_tools_for_user

    aws_only = {t["name"] for t in get_domain_tools_for_user(opts.aws, ["cloud"])}
    assert_true("apply_managed_upgrade" not in aws_only,
                "apply_managed_upgrade was offered to a manage_aws-only user; "
                "its twin ANDs a platform management tier on top")
    assert_true("get_managed_upgrades" in aws_only,
                "the upgrade READ must stay available to manage_aws")
    root = {t["name"] for t in get_domain_tools_for_user(opts.root, ["cloud"])}
    assert_true("apply_managed_upgrade" in root,
                "a superuser must still be offered apply_managed_upgrade")


@th.django_unit_test("the System Setup reads are hidden from a non-superuser admin")
def test_setup_reads_are_superuser_only(opts):
    from mojo.apps.assistant import get_domain_tools_for_user

    listed = {t["name"] for t in get_domain_tools_for_user(opts.platform, ["cloud"])}
    for name in SUPERUSER_READS:
        assert_true(name not in listed,
                    f"{name} was offered to a non-superuser holding 'admin'")
    root = {t["name"] for t in get_domain_tools_for_user(opts.root, ["cloud"])}
    for name in SUPERUSER_READS:
        assert_true(name in root, f"{name} was hidden from a superuser")


# ---------------------------------------------------------------------------
# The hoisted deploy-status table
# ---------------------------------------------------------------------------

@th.django_unit_test("the deploy action table matches what the Admin offers")
def test_actions_by_status_pinned(opts):
    from mojo.apps.edge.services import platform_deploy

    actual = {status: tuple(actions)
              for status, actions in platform_deploy.ACTIONS_BY_STATUS.items()}
    assert_eq(actual, EXPECTED_ACTIONS_BY_STATUS,
              f"the hoisted deploy action table drifted from the Admin's: {actual}")
    assert_eq(set(platform_deploy.ACTIVE_STATUSES), EXPECTED_ACTIVE_STATUSES,
              f"ACTIVE_STATUSES drifted: {set(platform_deploy.ACTIVE_STATUSES)}")
    for status in EXPECTED_ACTIVE_STATUSES:
        assert_eq(platform_deploy.actions_for_status(status), (),
                  f"an active {status} attempt must earn no control — the "
                  f"orchestrator is driving it")
    assert_eq(platform_deploy.actions_for_status("superseded"), (),
              "a superseded attempt is history, not a control surface")
    assert_eq(platform_deploy.actions_for_status("failed"), ("retry", "verify"),
              "retry must be offered on a failed attempt")
