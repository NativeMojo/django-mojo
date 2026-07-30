"""dnsman — house-account discovery.

`onboarding.discover_house_domains()` is the ONE account-wide listing in this
app, and it exists only for the HOUSE AWS account (see the module docstring in
services/onboarding.py — the BYO/tenant-credential no-listing rule is untouched,
and `3_credentials.py` still pins it).

These call the service directly in-process with both route53 listers patched, so
nothing here reaches AWS. The REST gate that fronts it is tested separately in
`8_rest_permissions.py`.
"""

from unittest.mock import patch

from objict import objict

from testit import helpers as th


MODULE = "mojo.helpers.aws.route53"

TRACKED = "disc-tracked-example.com"


def _registered(*names, truncated=False):
    return objict(
        domains=[objict(name=n, auto_renew=True, transfer_lock=False,
                        expires="2027-01-01T00:00:00Z") for n in names],
        truncated=truncated)


def _zones(*specs, truncated=False):
    """specs are (name, private) or a bare name (public)."""
    zones = []
    for index, spec in enumerate(specs):
        name, private = spec if isinstance(spec, tuple) else (spec, False)
        zones.append(objict(id=f"Z{index}TEST", name=name, private=private,
                            record_count=4 + index))
    return objict(zones=zones, truncated=truncated)


def _discover(registered, zones, **kwargs):
    from mojo.apps.dnsman.services import onboarding

    with patch(f"{MODULE}.list_registered_domains", return_value=registered), \
            patch(f"{MODULE}.list_hosted_zones", return_value=zones):
        return onboarding.discover_house_domains(**kwargs)


def _by_name(result):
    return {row.name: row for row in result.domains}


@th.django_unit_setup()
def setup_discovery(opts):
    from mojo.apps.dnsman.models import Domain

    # Long-lived DB: clear what this module creates before creating it.
    Domain.objects.filter(name__startswith="disc-").delete()

    opts.tracked = Domain.objects.create(
        name=TRACKED, group=None, user=None, provider="route53",
        status="active", verified=True, hosted_zone_id="ZTRACKED")


# ---------------------------------------------------------------------------
# the merge — two AWS APIs, one row per name
# ---------------------------------------------------------------------------

@th.django_unit_test("a registered domain with no zone is reported as registered only")
def test_registered_without_zone(opts):
    result = _discover(_registered("disc-regonly.com"), _zones())

    rows = _by_name(result)
    assert "disc-regonly.com" in rows, \
        f"a registered domain must appear in the inventory; got {sorted(rows)}"
    row = rows["disc-regonly.com"]
    assert row.registered is True, "expected registered=True for a Route53 Domains registration"
    assert row.hosted_zone is False, \
        "expected hosted_zone=False — no zone was returned for this name"
    assert row.hosted_zone_id is None, \
        f"a name with no zone must carry no zone id, got {row.hosted_zone_id!r}"


@th.django_unit_test("a hosted zone with no registration is reported as zone only")
def test_zone_without_registration(opts):
    result = _discover(_registered(), _zones("disc-zoneonly.com"))

    rows = _by_name(result)
    assert "disc-zoneonly.com" in rows, \
        f"a hosted zone must appear in the inventory; got {sorted(rows)}"
    row = rows["disc-zoneonly.com"]
    assert row.registered is False, \
        "expected registered=False — the name was not in the registrar listing"
    assert row.hosted_zone is True, "expected hosted_zone=True for a public zone"
    assert row.hosted_zone_id == "Z0TEST", \
        f"expected the zone id to be carried through, got {row.hosted_zone_id!r}"


@th.django_unit_test("a name in BOTH APIs merges into one row")
def test_registered_and_zone_merge(opts):
    result = _discover(_registered("disc-both.com"), _zones("disc-both.com"))

    assert result.count == 1, \
        f"the same name from both APIs must merge into one row, got {result.count}"
    row = result.domains[0]
    assert row.registered and row.hosted_zone, \
        "the merged row must report BOTH the registration and the zone"
    assert row.expires is not None, "the merged row lost the registrar expiry"
    assert row.record_count is not None, "the merged row lost the zone record count"


# ---------------------------------------------------------------------------
# private zones — VPC-internal, never adoptable
# ---------------------------------------------------------------------------

@th.django_unit_test("a private-only zone is excluded from the inventory")
def test_private_zone_excluded(opts):
    result = _discover(_registered(), _zones(("disc-private.com", True)))

    assert result.count == 0, (
        "a private (VPC-internal) zone is not an adoptable public domain and "
        f"must not appear in the inventory; got {[r.name for r in result.domains]}")


@th.django_unit_test("a registered name whose only zone is private is flagged un-adoptable")
def test_registered_with_private_only_zone(opts):
    result = _discover(_registered("disc-privreg.com"),
                       _zones(("disc-privreg.com", True)))

    rows = _by_name(result)
    assert "disc-privreg.com" in rows, \
        "a REGISTERED name is still a real row even when its only zone is private"
    row = rows["disc-privreg.com"]
    assert row.hosted_zone is False, \
        "a private zone must not be reported as this name's hosted zone"
    assert row.adoptable is False, (
        "find_zone_id falls back to the private zone, so adopting this would "
        "manage a zone that resolves nowhere — it must be flagged un-adoptable")
    assert row.reason and "private" in row.reason.lower(), \
        f"the row must say WHY it is un-adoptable, got {row.reason!r}"


@th.django_unit_test("a public zone wins over a private zone of the same name")
def test_public_zone_beats_private(opts):
    result = _discover(_registered(),
                       _zones(("disc-mixed.com", True), ("disc-mixed.com", False)))

    rows = _by_name(result)
    row = rows.get("disc-mixed.com")
    assert row is not None, "a name with a public zone must appear in the inventory"
    assert row.hosted_zone is True, "the public zone must be reported"
    assert row.adoptable is True, (
        "a name that HAS a public zone is adoptable — the private zone alongside "
        "it is irrelevant")


# ---------------------------------------------------------------------------
# tracked flags
# ---------------------------------------------------------------------------

@th.django_unit_test("a name dnsman already tracks is flagged with its domain id")
def test_tracked_flag(opts):
    result = _discover(_registered(TRACKED, "disc-fresh.com"), _zones())

    rows = _by_name(result)
    tracked = rows[TRACKED]
    assert tracked.tracked is True, "an existing Domain row must set tracked=True"
    assert tracked.domain == opts.tracked.pk, \
        f"expected the tracked row to carry Domain pk {opts.tracked.pk}, got {tracked.domain}"
    assert tracked.adoptable is False, \
        "an already-tracked name cannot be adopted again — adopt would refuse it"

    fresh = rows["disc-fresh.com"]
    assert fresh.tracked is False, "an unknown name must report tracked=False"
    assert fresh.domain is None, \
        f"an untracked row must carry no domain id, got {fresh.domain!r}"


@th.django_unit_test("untracked_only drops tracked rows and count follows")
def test_untracked_only(opts):
    result = _discover(_registered(TRACKED, "disc-fresh2.com"), _zones(),
                       untracked_only=True)

    names = [row.name for row in result.domains]
    assert TRACKED not in names, \
        f"untracked_only must drop already-tracked names, got {names}"
    assert "disc-fresh2.com" in names, "untracked_only dropped an untracked name"
    assert result.count == len(result.domains), \
        f"count ({result.count}) must match the filtered rows ({len(result.domains)})"


@th.django_unit_test("the tracked lookup is a single query, not one per row")
def test_tracked_lookup_is_one_query(opts):
    from django.test.utils import CaptureQueriesContext
    from django.db import connection

    with CaptureQueriesContext(connection) as captured:
        _discover(_registered("disc-q1.com", "disc-q2.com", TRACKED),
                  _zones("disc-q3.com"))

    assert len(captured.captured_queries) <= 1, (
        "discovery must resolve tracked flags with ONE query over name__in, not "
        f"a lookup per row; ran {len(captured.captured_queries)}")


# ---------------------------------------------------------------------------
# names that will not normalize must not blank the inventory
# ---------------------------------------------------------------------------

@th.django_unit_test("an unnormalizable zone name is flagged, not fatal")
def test_bad_name_is_flagged_not_fatal(opts):
    # A leading-underscore label is rejected by naming.normalize_domain. One odd
    # zone in the account must not take the whole listing down with it.
    result = _discover(_registered("disc-good.com"), _zones("_weird.disc-bad.com"))

    rows = _by_name(result)
    assert "disc-good.com" in rows, \
        f"a bad name must not remove the good rows; got {sorted(rows)}"
    bad = rows.get("_weird.disc-bad.com")
    assert bad is not None, \
        f"the un-normalizable name should still be listed; got {sorted(rows)}"
    assert bad.adoptable is False, "an un-normalizable name cannot be adopted"
    assert bad.reason, "an un-adoptable row must carry a reason"


# ---------------------------------------------------------------------------
# truncation is reported, never silent
# ---------------------------------------------------------------------------

@th.django_unit_test("truncation from the registrar listing reaches the caller")
def test_truncated_from_registrar(opts):
    result = _discover(_registered("disc-t1.com", truncated=True), _zones())

    assert result.truncated is True, (
        "a partial inventory that reports as complete is a wrong answer — "
        "truncated must propagate from the registrar lister")


@th.django_unit_test("truncation from the zone listing reaches the caller")
def test_truncated_from_zones(opts):
    result = _discover(_registered(), _zones("disc-t2.com", truncated=True))

    assert result.truncated is True, \
        "truncated must propagate from the hosted-zone lister"


@th.django_unit_test("a complete listing reports truncated False")
def test_not_truncated(opts):
    result = _discover(_registered("disc-t3.com"), _zones("disc-t3.com"))

    assert result.truncated is False, \
        "a listing that exhausted both APIs must not claim to be truncated"


@th.django_unit_test("an empty account is an empty inventory, not an error")
def test_empty_account(opts):
    result = _discover(_registered(), _zones())

    assert result.count == 0, f"expected an empty inventory, got {result.count}"
    assert result.domains == [], f"expected no rows, got {result.domains}"
    assert result.truncated is False, "an empty account is not a truncated one"


# ---------------------------------------------------------------------------
# a failing AWS call is an explicit error, never a half inventory
# ---------------------------------------------------------------------------

@th.django_unit_test("a failing registrar listing raises ValueException, not a boto error")
def test_registrar_failure_raises(opts):
    from mojo.apps.dnsman.services import onboarding
    from mojo import errors as me

    with patch(f"{MODULE}.list_registered_domains", side_effect=RuntimeError("AccessDenied")), \
            patch(f"{MODULE}.list_hosted_zones", return_value=_zones("disc-x.com")):
        try:
            onboarding.discover_house_domains()
            raise AssertionError(
                "a failed registrar listing must raise, not return the zones alone — "
                "a half inventory is indistinguishable from an empty account")
        except me.ValueException as err:
            assert "AccessDenied" in str(err), \
                f"the refusal must name the underlying reason, got {err}"


@th.django_unit_test("a failing zone listing raises ValueException, not a half inventory")
def test_zone_failure_raises(opts):
    from mojo.apps.dnsman.services import onboarding
    from mojo import errors as me

    with patch(f"{MODULE}.list_registered_domains", return_value=_registered("disc-y.com")), \
            patch(f"{MODULE}.list_hosted_zones", side_effect=RuntimeError("Throttled")):
        try:
            result = onboarding.discover_house_domains()
            raise AssertionError(
                "a failed zone listing must raise, not return the registrations "
                f"alone; got {result.count} rows")
        except me.ValueException as err:
            assert "Throttled" in str(err), \
                f"the refusal must name the underlying reason, got {err}"


# ---------------------------------------------------------------------------
# discovery is read-only
# ---------------------------------------------------------------------------

@th.django_unit_test("discovery creates no Domain rows")
def test_discovery_creates_nothing(opts):
    from mojo.apps.dnsman.models import Domain

    before = Domain.objects.count()
    _discover(_registered("disc-nocreate.com"), _zones("disc-nocreate2.com"))
    after = Domain.objects.count()

    assert before == after, (
        f"discovery must be read-only — Domain count moved from {before} to "
        f"{after}. Ingest is an explicit adopt call, never a side effect of listing")


@th.django_unit_test("duplicate public zones for one name are flagged, not silently collapsed")
def test_duplicate_public_zones_flagged(opts):
    """
    Route53 permits several public zones for the same name. Collapsing to
    last-wins would show one zone id while adopt binds whatever find_zone_id
    returns first — a different API with a different ordering — so the operator
    could adopt the empty, non-delegated duplicate and believe DNS is managed.
    """
    result = _discover(_registered(), _zones("disc-dupe.com", "disc-dupe.com"))

    rows = _by_name(result)
    row = rows.get("disc-dupe.com")
    assert row is not None, "the duplicated name must still be listed"
    assert row.adoptable is False, (
        "a name with more than one public hosted zone must not be offered for "
        "adoption — dnsman cannot tell which zone adopt would bind")
    assert row.reason and "more than one" in row.reason.lower(), \
        f"the row must say the zones are duplicated, got {row.reason!r}"


@th.django_unit_test("an unparseable registered name is listed, not fatal")
def test_unparseable_registered_name(opts):
    """The registrar path must be as tolerant as the zone path — a listing that
    dies on one odd row is a listing nobody can trust."""
    result = _discover(_registered("disc-ok.com", "bad name.com"), _zones())

    rows = _by_name(result)
    assert "disc-ok.com" in rows, \
        f"a good registration must survive a bad one; got {sorted(rows)}"
    bad = rows.get("bad name.com")
    assert bad is not None, f"the odd row should still be listed; got {sorted(rows)}"
    assert bad.adoptable is False, "a name that will not normalize cannot be adopted"
