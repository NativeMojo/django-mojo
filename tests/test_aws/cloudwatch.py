from testit import helpers as th
from testit import TestitSkip
from mojo.helpers.settings import settings

TEST_USER = "aws_user"
TEST_PWORD = "aws##mojo99"

ADMIN_USER = "aws_admin"
ADMIN_PWORD = "aws##mojo99"


def _require_aws():
    """Raise TestitSkip when AWS_KEY is not configured on the live server."""
    if not settings.get("AWS_KEY"):
        raise TestitSkip("AWS_KEY not configured — skipping live CloudWatch tests")


def _assert_has_data(data_dict, label):
    """
    Assert that at least one value across all slugs is non-zero.

    data_dict is the inner data field: {slug_name: [values, ...]}
    On a live system, metrics like cpu and conns can never be all-zero.
    """
    for slug, values in data_dict.items():
        if any(v != 0.0 for v in values):
            return
    raise AssertionError(
        f"{label}: all values are 0.0 on a live system — possible metric fetch or timezone bug"
    )


def _get_values(data_dict, slug):
    """Return the values list for a slug from the {slug: [values]} dict."""
    return data_dict.get(slug)


# ------------------------------------------------------------------
# Setup
# ------------------------------------------------------------------

@th.django_unit_setup()
def setup_users(opts):
    from mojo.apps.account.models import User

    User.objects.filter(username__in=[TEST_USER, ADMIN_USER]).delete()

    user = User(username=TEST_USER, display_name=TEST_USER, email=f"{TEST_USER}@example.com")
    user.save()
    user.is_email_verified = True
    user.save_password(TEST_PWORD)

    admin = User(username=ADMIN_USER, display_name=ADMIN_USER, email=f"{ADMIN_USER}@example.com")
    admin.save()
    admin.add_permission(["manage_aws", "manage_users", "view_global", "view_admin"])
    admin.is_staff = True
    admin.is_superuser = True
    admin.is_email_verified = True
    admin.save_password(ADMIN_PWORD)


# ------------------------------------------------------------------
# Auth helpers
# ------------------------------------------------------------------

@th.unit_test("cw_admin_login")
def test_admin_login(opts):
    resp = opts.client.login(ADMIN_USER, ADMIN_PWORD)
    assert opts.client.is_authenticated, "admin authentication failed"
    opts.admin_id = opts.client.jwt_data.uid


@th.unit_test("cw_user_login")
def test_user_login(opts):
    resp = opts.client.login(TEST_USER, TEST_PWORD)
    assert opts.client.is_authenticated, "user authentication failed"
    opts.user_id = opts.client.jwt_data.uid


# ------------------------------------------------------------------
# Permission guard tests — always run, no AWS credentials needed
# ------------------------------------------------------------------

@th.unit_test("cw_resources_unauthenticated")
def test_resources_unauthenticated(opts):
    """Unauthenticated request must be rejected."""
    opts.client.logout()
    resp = opts.client.get("/api/aws/cloudwatch/resources")
    assert resp.status_code in (401, 403), \
        f"Expected 401 or 403, got {resp.status_code}"


@th.unit_test("cw_fetch_unauthenticated")
def test_fetch_unauthenticated(opts):
    """Unauthenticated fetch must be rejected."""
    opts.client.logout()
    resp = opts.client.get("/api/aws/cloudwatch/fetch?account=ec2&category=cpu")
    assert resp.status_code in (401, 403), \
        f"Expected 401 or 403, got {resp.status_code}"


@th.unit_test("cw_fetch_no_manage_aws")
def test_fetch_no_permission(opts):
    """User without manage_aws must be denied."""
    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.get("/api/aws/cloudwatch/fetch?account=ec2&category=cpu")
    assert resp.status_code in (401, 403), \
        f"Expected 401 or 403, got {resp.status_code}"
    opts.client.logout()


@th.unit_test("cw_resources_no_manage_aws")
def test_resources_no_permission(opts):
    """User without manage_aws must be denied on resources."""
    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.get("/api/aws/cloudwatch/resources")
    assert resp.status_code in (401, 403), \
        f"Expected 401 or 403, got {resp.status_code}"
    opts.client.logout()


# ------------------------------------------------------------------
# Required parameter validation — always run, no AWS credentials needed
# ------------------------------------------------------------------

@th.unit_test("cw_fetch_missing_account")
def test_fetch_missing_account(opts):
    """Missing account must return 400."""
    opts.client.login(ADMIN_USER, ADMIN_PWORD)
    resp = opts.client.get("/api/aws/cloudwatch/fetch?category=cpu")
    assert resp.status_code == 400, \
        f"Expected 400 for missing account, got {resp.status_code}"
    opts.client.logout()


@th.unit_test("cw_fetch_missing_category")
def test_fetch_missing_category(opts):
    """Missing category must return 400."""
    opts.client.login(ADMIN_USER, ADMIN_PWORD)
    resp = opts.client.get("/api/aws/cloudwatch/fetch?account=ec2")
    assert resp.status_code == 400, \
        f"Expected 400 for missing category, got {resp.status_code}"
    opts.client.logout()


@th.unit_test("cw_fetch_invalid_account")
def test_fetch_invalid_account(opts):
    """Unknown account type must return 400."""
    opts.client.login(ADMIN_USER, ADMIN_PWORD)
    resp = opts.client.get("/api/aws/cloudwatch/fetch?account=bogus&category=cpu")
    assert resp.status_code == 400, \
        f"Expected 400 for invalid account, got {resp.status_code}"
    opts.client.logout()


@th.unit_test("cw_fetch_invalid_category")
def test_fetch_invalid_category(opts):
    """Unknown category must return 400."""
    opts.client.login(ADMIN_USER, ADMIN_PWORD)
    resp = opts.client.get("/api/aws/cloudwatch/fetch?account=ec2&category=bogus_metric")
    assert resp.status_code == 400, \
        f"Expected 400 for invalid category, got {resp.status_code}"
    opts.client.logout()


@th.unit_test("cw_fetch_category_wrong_account")
def test_fetch_category_wrong_account(opts):
    """Category unsupported for the given account type must return 400."""
    opts.client.login(ADMIN_USER, ADMIN_PWORD)
    resp = opts.client.get("/api/aws/cloudwatch/fetch?account=ec2&category=cache_hits")
    assert resp.status_code == 400, \
        f"Expected 400 for cache_hits on ec2, got {resp.status_code}"
    resp = opts.client.get("/api/aws/cloudwatch/fetch?account=redis&category=free_storage")
    assert resp.status_code == 400, \
        f"Expected 400 for free_storage on redis, got {resp.status_code}"
    resp = opts.client.get("/api/aws/cloudwatch/fetch?account=rds&category=disk_read")
    assert resp.status_code == 400, \
        f"Expected 400 for disk_read on rds, got {resp.status_code}"
    opts.client.logout()


# ------------------------------------------------------------------
# Live AWS tests — skipped when AWS_KEY is not configured
# ------------------------------------------------------------------

@th.unit_test("cw_resources_list")
def test_resources_list(opts):
    """
    GET /aws/cloudwatch/resources returns ec2, rds, and redis lists.
    Stashes first IDs for downstream tests.
    """
    _require_aws()
    opts.client.login(ADMIN_USER, ADMIN_PWORD)
    resp = opts.client.get("/api/aws/cloudwatch/resources")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    assert resp.response.status is True
    assert isinstance(resp.response.ec2, list), "ec2 should be a list"
    assert isinstance(resp.response.rds, list), "rds should be a list"
    assert isinstance(resp.response.redis, list), "redis should be a list"
    if resp.response.ec2:
        first_ec2 = resp.response.ec2[0]
        assert hasattr(first_ec2, "slug"), "EC2 resource entry missing slug field"
        assert first_ec2.slug, "EC2 resource slug should not be empty"
        opts.ec2_id = first_ec2.id
        opts.ec2_slug = first_ec2.slug
    if resp.response.rds:
        first_rds = resp.response.rds[0]
        assert hasattr(first_rds, "slug"), "RDS resource entry missing slug field"
        opts.rds_id = first_rds.id
        opts.rds_slug = first_rds.slug
    if resp.response.redis:
        first_redis = resp.response.redis[0]
        assert hasattr(first_redis, "slug"), "Redis resource entry missing slug field"
        opts.redis_id = first_redis.id
        opts.redis_slug = first_redis.slug
    opts.client.logout()


@th.unit_test("cw_fetch_ec2_all_instances")
def test_fetch_ec2_all_instances(opts):
    """
    account=ec2&category=cpu with no slugs returns data for all EC2 instances.
    Response shape: {data: {slug: [values]}, labels: [...]}
    """
    _require_aws()
    if not getattr(opts, "ec2_id", None):
        raise TestitSkip("No EC2 instances found")

    opts.client.login(ADMIN_USER, ADMIN_PWORD)
    resp = opts.client.get("/api/aws/cloudwatch/fetch?account=ec2&category=cpu&granularity=hours")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    result = resp.response
    assert result.status is True
    data = result.data
    assert hasattr(data, "labels"), "data missing labels key"
    assert isinstance(data.labels, list), "labels should be a list"
    assert len(data.labels) > 0, "labels should not be empty"
    assert isinstance(data.data, dict), "data.data should be a dict of {slug: values}"
    _assert_has_data(data.data, "EC2 cpu (all instances)")
    opts.client.logout()


@th.unit_test("cw_fetch_ec2_single_slug")
def test_fetch_ec2_single_slug(opts):
    """
    Passing a single slug returns data keyed by that slug in the data dict.
    """
    _require_aws()
    if not getattr(opts, "ec2_slug", None):
        raise TestitSkip("No EC2 instances found")

    opts.client.login(ADMIN_USER, ADMIN_PWORD)
    resp = opts.client.get(
        f"/api/aws/cloudwatch/fetch?account=ec2&category=cpu&slugs={opts.ec2_slug}"
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    data = resp.response.data
    assert isinstance(data.data, dict), "data.data should be a {slug: values} dict"
    assert opts.ec2_slug in data.data, \
        f"Expected friendly slug '{opts.ec2_slug}', got keys: {list(data.data.keys())}"
    values = data.data[opts.ec2_slug]
    assert isinstance(values, list), \
        f"values for '{opts.ec2_slug}' should be a list, got {type(values)}"
    assert len(values) == len(data.labels), \
        f"values length {len(values)} must match labels length {len(data.labels)}"
    _assert_has_data(data.data, "EC2 cpu (single slug)")
    opts.client.logout()


@th.unit_test("cw_fetch_ec2_slug_is_name")
def test_fetch_ec2_slug_is_name(opts):
    """
    When an EC2 instance has a Name tag the returned slug must be that name,
    not the raw AWS instance ID. Slug must match what resources endpoint advertised.
    """
    _require_aws()
    if not getattr(opts, "ec2_slug", None):
        raise TestitSkip("No EC2 instances found")

    opts.client.login(ADMIN_USER, ADMIN_PWORD)
    resp = opts.client.get(
        f"/api/aws/cloudwatch/fetch?account=ec2&category=cpu&slugs={opts.ec2_slug}"
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    returned_slugs = list(resp.response.data.data.keys())
    assert opts.ec2_slug in returned_slugs, \
        f"fetch slug '{opts.ec2_slug}' not in response keys: {returned_slugs}"

    if opts.ec2_slug != opts.ec2_id:
        import re
        raw_id_pattern = re.compile(r"^i-[0-9a-f]{8,17}$")
        assert not raw_id_pattern.match(opts.ec2_slug), \
            f"slug '{opts.ec2_slug}' looks like a raw AWS ID — expected friendly Name tag value"
    opts.client.logout()


@th.unit_test("cw_fetch_ec2_net_in")
def test_fetch_ec2_net_in(opts):
    """NetworkIn (net_in) should be valid for ec2."""
    _require_aws()
    if not getattr(opts, "ec2_slug", None):
        raise TestitSkip("No EC2 instances found")

    opts.client.login(ADMIN_USER, ADMIN_PWORD)
    resp = opts.client.get(
        f"/api/aws/cloudwatch/fetch?account=ec2&category=net_in&slugs={opts.ec2_slug}"
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    assert resp.response.status is True, "response status should be True"
    opts.client.logout()


@th.unit_test("cw_fetch_ec2_memory")
def test_fetch_ec2_memory(opts):
    """
    memory category uses the CWAgent namespace (mem_used_percent).
    Valid regardless of whether the CloudWatch Agent is installed.
    """
    _require_aws()
    if not getattr(opts, "ec2_slug", None):
        raise TestitSkip("No EC2 instances found")

    opts.client.login(ADMIN_USER, ADMIN_PWORD)
    resp = opts.client.get(
        f"/api/aws/cloudwatch/fetch?account=ec2&category=memory&slugs={opts.ec2_slug}"
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    assert resp.response.status is True, "response status should be True"
    data = resp.response.data
    assert isinstance(data.data, dict), "data.data should be a {slug: values} dict"
    values = _get_values(data.data, opts.ec2_slug)
    assert isinstance(values, list), \
        f"memory values should be a list, got {type(values)}"
    assert len(values) == len(data.labels), \
        "memory values length must match labels length"
    if any(v != 0.0 for v in values):
        _assert_has_data(data.data, "EC2 memory (CWAgent)")
    opts.client.logout()


@th.unit_test("cw_fetch_ec2_disk")
def test_fetch_ec2_disk(opts):
    """
    disk category uses the CWAgent namespace (disk_used_percent) targeting root filesystem.
    Valid regardless of whether the CloudWatch Agent is installed.
    """
    _require_aws()
    if not getattr(opts, "ec2_slug", None):
        raise TestitSkip("No EC2 instances found")

    opts.client.login(ADMIN_USER, ADMIN_PWORD)
    resp = opts.client.get(
        f"/api/aws/cloudwatch/fetch?account=ec2&category=disk&slugs={opts.ec2_slug}"
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    assert resp.response.status is True, "response status should be True"
    data = resp.response.data
    assert isinstance(data.data, dict), "data.data should be a {slug: values} dict"
    values = _get_values(data.data, opts.ec2_slug)
    assert isinstance(values, list), \
        f"disk values should be a list, got {type(values)}"
    assert len(values) == len(data.labels), \
        "disk values length must match labels length"
    if any(v != 0.0 for v in values):
        _assert_has_data(data.data, "EC2 disk (CWAgent)")
    opts.client.logout()


@th.unit_test("cw_fetch_ec2_granularity_days")
def test_fetch_ec2_granularity_days(opts):
    """granularity=days should produce fewer, wider buckets than granularity=hours."""
    _require_aws()
    if not getattr(opts, "ec2_slug", None):
        raise TestitSkip("No EC2 instances found")

    opts.client.login(ADMIN_USER, ADMIN_PWORD)
    resp_hours = opts.client.get(
        f"/api/aws/cloudwatch/fetch?account=ec2&category=cpu&slugs={opts.ec2_slug}&granularity=hours"
    )
    resp_days = opts.client.get(
        f"/api/aws/cloudwatch/fetch?account=ec2&category=cpu&slugs={opts.ec2_slug}&granularity=days"
    )
    assert resp_hours.status_code == 200
    assert resp_days.status_code == 200
    hours_labels = resp_hours.response.data.labels
    days_labels = resp_days.response.data.labels
    assert len(hours_labels) > len(days_labels), \
        "hours granularity should produce more buckets than days"
    opts.client.logout()


@th.unit_test("cw_fetch_ec2_stat_max")
def test_fetch_ec2_stat_max(opts):
    """stat=max should be accepted and return a valid response."""
    _require_aws()
    if not getattr(opts, "ec2_slug", None):
        raise TestitSkip("No EC2 instances found")

    opts.client.login(ADMIN_USER, ADMIN_PWORD)
    resp = opts.client.get(
        f"/api/aws/cloudwatch/fetch?account=ec2&category=cpu&slugs={opts.ec2_slug}&stat=max"
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    assert resp.response.status is True, "response status should be True"
    opts.client.logout()


@th.unit_test("cw_fetch_rds_all_instances")
def test_fetch_rds_all_instances(opts):
    """account=rds&category=cpu with no slugs returns data for all RDS instances."""
    _require_aws()
    if not getattr(opts, "rds_id", None):
        raise TestitSkip("No RDS instances found")

    opts.client.login(ADMIN_USER, ADMIN_PWORD)
    resp = opts.client.get("/api/aws/cloudwatch/fetch?account=rds&category=cpu")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    assert resp.response.status is True, "response status should be True"
    assert isinstance(resp.response.data.labels, list), \
        f"labels should be a list, got {type(resp.response.data.labels)}"
    _assert_has_data(resp.response.data.data, "RDS cpu (all instances)")
    opts.client.logout()


@th.unit_test("cw_fetch_rds_conns")
def test_fetch_rds_conns(opts):
    """conns category should resolve to DatabaseConnections for rds."""
    _require_aws()
    if not getattr(opts, "rds_slug", None):
        raise TestitSkip("No RDS instances found")

    opts.client.login(ADMIN_USER, ADMIN_PWORD)
    resp = opts.client.get(
        f"/api/aws/cloudwatch/fetch?account=rds&category=conns&slugs={opts.rds_slug}"
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    assert resp.response.status is True, "response status should be True"
    _assert_has_data(resp.response.data.data, "RDS conns")
    opts.client.logout()


@th.unit_test("cw_fetch_redis_all_clusters")
def test_fetch_redis_all_clusters(opts):
    """account=redis&category=cpu with no slugs returns data for all ElastiCache clusters."""
    _require_aws()
    if not getattr(opts, "redis_id", None):
        raise TestitSkip("No ElastiCache clusters found")

    opts.client.login(ADMIN_USER, ADMIN_PWORD)
    resp = opts.client.get("/api/aws/cloudwatch/fetch?account=redis&category=cpu")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    assert resp.response.status is True, "response status should be True"
    assert isinstance(resp.response.data.labels, list), \
        f"labels should be a list, got {type(resp.response.data.labels)}"
    opts.client.logout()


@th.unit_test("cw_fetch_redis_conns")
def test_fetch_redis_conns(opts):
    """conns category should resolve to CurrConnections for redis."""
    _require_aws()
    if not getattr(opts, "redis_slug", None):
        raise TestitSkip("No ElastiCache clusters found")

    opts.client.login(ADMIN_USER, ADMIN_PWORD)
    resp = opts.client.get(
        f"/api/aws/cloudwatch/fetch?account=redis&category=conns&slugs={opts.redis_slug}"
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    assert resp.response.status is True, "response status should be True"
    _assert_has_data(resp.response.data.data, "Redis conns")
    opts.client.logout()


@th.unit_test("cw_fetch_redis_cache_hits")
def test_fetch_redis_cache_hits(opts):
    """cache_hits is a redis-specific category and should return valid data."""
    _require_aws()
    if not getattr(opts, "redis_slug", None):
        raise TestitSkip("No ElastiCache clusters found")

    opts.client.login(ADMIN_USER, ADMIN_PWORD)
    resp = opts.client.get(
        f"/api/aws/cloudwatch/fetch?account=redis&category=cache_hits&slugs={opts.redis_slug}"
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    assert resp.response.status is True, "response status should be True"
    opts.client.logout()
