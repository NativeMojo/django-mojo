"""Regression: a failed carrier lookup must negative-cache, not crash.

`PhoneNumber.refresh()` called `self.save()` on the provider-error path while
`lookup_expires_at` was still None. The column is NOT NULL with no default, so
every failed lookup of a never-cached number raised IntegrityError: the
negative result was never cached and the provider was re-billed on every retry.

The fix stamps a short, backing-off error TTL on EVERY error, records the error
in `lookup_data`, and leaves the carrier verdict fields untouched — a provider
outage is "no verdict", never a verdict. `lookup_unavailable` is the flag a
consumer reads; `is_valid` keeps meaning "the carrier verdict from the last
*successful* lookup".

No mock, no patch: the provider call is driven through `refresh()`'s
keyword-only `lookup_fn` seam, so this file is offline and does not touch the
package's cold-site budget. Untagged on purpose — it must run in the `core`
preset, the only non-advisory gate.
"""
from objict import objict
from testit import helpers as th


ERR_NUMBER = "+14155559001"
CACHED_NUMBER = "+14155559002"
STALE_NUMBER = "+14155559003"
FRESH_NUMBER = "+14155559004"
GOOD_NUMBER = "+14155559005"
BAD_INPUT = "not a phone"

TEST_NUMBERS = [ERR_NUMBER, CACHED_NUMBER, STALE_NUMBER, FRESH_NUMBER, GOOD_NUMBER]

USER_EMAIL = "phonehub_lookup_err@test.com"
USER_PASSWORD = "Phonehub_lookup_err_pw_99"

ERROR_TEXT = "provider unavailable (test)"


def _failing_lookup(_number):
    """Stand-in for services.twilio.lookup on the provider-error path."""
    return objict(error=ERROR_TEXT)


def _good_lookup(_number):
    """Stand-in for services.twilio.lookup on the success path."""
    return objict(
        country_code="US",
        carrier="Test Carrier",
        line_type="mobile",
        is_mobile=True,
        is_voip=False,
        is_valid=True,
        caller_name="Test Owner",
        caller_type="CONSUMER",
        lookup_provider="twilio",
        error=None,
    )


@th.django_unit_setup()
def setup_phone_lookup_error_cache(opts):
    from mojo.apps.account.models import User
    from mojo.apps.phonehub.models import PhoneNumber

    # Tests run on long-lived databases — clean prior runs first.
    PhoneNumber.objects.filter(phone_number__in=TEST_NUMBERS).delete()
    User.objects.filter(email=USER_EMAIL).delete()

    user = User.objects.create_user(
        username=USER_EMAIL, email=USER_EMAIL, password=USER_PASSWORD)
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.add_permission("view_phone_numbers")
    user.save()
    opts.user_id = user.pk


@th.django_unit_test("a failed lookup of an uncached number negative-caches instead of raising")
def test_error_on_uncached_number_negative_caches(opts):
    from mojo.helpers import dates
    from mojo.apps.phonehub.models import PhoneNumber

    phone = PhoneNumber(phone_number=ERR_NUMBER)
    result = phone.refresh(lookup_fn=_failing_lookup)

    assert result is False, f"a provider error must report failure, got {result!r}"
    assert phone.pk is not None, "the negative result must be persisted, not dropped"
    assert phone.lookup_expires_at is not None, (
        "lookup_expires_at must be stamped on the error path — leaving it None is "
        "the IntegrityError this regression covers")
    now = dates.utcnow()
    assert phone.lookup_expires_at > now, (
        f"the error TTL must be in the future, got {phone.lookup_expires_at}")
    assert phone.lookup_expires_at <= dates.add(when=now, minutes=16), (
        f"the first error TTL must be short (~15m), got {phone.lookup_expires_at}")
    assert phone.lookup_error == ERROR_TEXT, (
        f"the provider error must be recorded, got {phone.lookup_error!r}")
    assert phone.lookup_unavailable is True, (
        "a row with an error and no successful lookup carries no verdict")
    assert phone.needs_lookup is False, (
        "the negative cache must suppress an immediate re-lookup")
    assert phone.is_valid is True, (
        "is_valid must not be written by the error path (it is the last "
        "SUCCESSFUL verdict — meaningless here, which is why "
        "lookup_unavailable exists)")
    assert phone.carrier is None, "carrier must not be written by the error path"
    assert phone.last_lookup_at is None, (
        "last_lookup_at is the last SUCCESSFUL lookup — an error must not set it")
    assert phone.lookup_count == 0, "an errored lookup is not a successful lookup"


@th.django_unit_test("every error re-stamps the expiry, never only the first")
def test_every_error_restamps_the_expiry(opts):
    from mojo.helpers import dates
    from mojo.apps.phonehub.models import PhoneNumber

    phone = PhoneNumber.objects.filter(phone_number=ERR_NUMBER).first()
    assert phone is not None, "the previous test must have persisted the negative cache"

    phone.lookup_expires_at = dates.subtract(days=1)
    phone.save()

    result = phone.refresh(lookup_fn=_failing_lookup)
    assert result is False, f"a provider error must report failure, got {result!r}"
    assert phone.lookup_expires_at > dates.utcnow(), (
        "a repeat error must re-stamp the expiry, not leave it in the past")
    assert phone.lookup_data.get("error_count") == 2, (
        f"the error counter must advance, got {phone.lookup_data.get('error_count')!r}")


@th.django_unit_test("the error TTL backs off exponentially")
def test_error_ttl_backs_off(opts):
    from mojo.helpers import dates
    from mojo.apps.phonehub.models import PhoneNumber

    phone = PhoneNumber.objects.filter(phone_number=ERR_NUMBER).first()
    assert phone is not None, "the negative-cached row must exist"
    assert phone.lookup_data.get("error_count") == 2, (
        "this test follows the second error")

    # The second error's TTL is 15 * 2**1 = 30 minutes.
    now = dates.utcnow()
    assert phone.lookup_expires_at > dates.add(when=now, minutes=25), (
        f"the second error TTL must back off past 15m, got {phone.lookup_expires_at}")
    assert phone.lookup_expires_at <= dates.add(when=now, minutes=31), (
        f"the second error TTL must be ~30m, got {phone.lookup_expires_at}")


@th.django_unit_test("a cached valid row survives a transient provider error")
def test_cached_valid_row_survives_a_transient_error(opts):
    from mojo.helpers import dates
    from mojo.apps.phonehub.models import PhoneNumber

    last_lookup = dates.subtract(days=2)
    phone = PhoneNumber.objects.create(
        phone_number=CACHED_NUMBER,
        carrier="AT&T",
        line_type="mobile",
        is_mobile=True,
        is_valid=True,
        registered_owner="Cached Owner",
        lookup_provider="twilio",
        lookup_expires_at=dates.subtract(days=1),
        last_lookup_at=last_lookup,
        lookup_count=3,
    )

    result = phone.refresh(lookup_fn=_failing_lookup)
    assert result is False, f"a provider error must report failure, got {result!r}"

    phone.refresh_from_db()
    assert phone.carrier == "AT&T", f"carrier must survive a transient error, got {phone.carrier!r}"
    assert phone.line_type == "mobile", f"line_type must survive, got {phone.line_type!r}"
    assert phone.is_valid is True, "is_valid must survive a transient error"
    assert phone.registered_owner == "Cached Owner", (
        f"registered_owner must survive, got {phone.registered_owner!r}")
    assert phone.lookup_count == 3, f"lookup_count must not move, got {phone.lookup_count}"
    assert phone.last_lookup_at is not None, "last_lookup_at must survive"
    assert abs((phone.last_lookup_at - last_lookup).total_seconds()) < 2, (
        "last_lookup_at must not be rewritten by an error")
    assert phone.lookup_expires_at > dates.utcnow(), (
        "the expiry must move forward — that is what stops the per-call re-billing")
    assert phone.lookup_error == ERROR_TEXT, "the error must still be recorded"
    assert phone.lookup_unavailable is False, (
        "a row with a recent successful lookup still carries a usable verdict")


@th.django_unit_test("the negative cache suppresses the provider call")
def test_negative_cache_suppresses_the_provider_call(opts):
    from mojo.apps.phonehub.models import PhoneNumber

    cached = PhoneNumber.objects.filter(phone_number=ERR_NUMBER).first()
    assert cached is not None, "the negative-cached row must exist"
    expires_before = cached.lookup_expires_at
    count_before = cached.lookup_data.get("error_count")

    # The real classmethod, the real provider path: inside the TTL it must not
    # enter refresh() at all, so Twilio is never called and never billed.
    phone = PhoneNumber.lookup(ERR_NUMBER)
    assert phone is not None, "the cached row must be returned"
    assert phone.pk == cached.pk, "the cached row must be reused, not recreated"
    assert phone.lookup_data.get("error_count") == count_before, (
        "a cached error inside its TTL must not trigger another provider call")
    assert phone.lookup_expires_at == expires_before, (
        "the expiry must be untouched while the negative cache is valid")


@th.django_unit_test("an unnormalizable number creates no row")
def test_unnormalizable_number_makes_no_row(opts):
    from mojo.apps.phonehub.models import PhoneNumber

    result = PhoneNumber.lookup(BAD_INPUT)
    assert result is None, (
        f"an unnormalizable number has no cache key and must return None, got {result!r}")
    assert PhoneNumber.objects.filter(phone_number=None).count() == 0, (
        "no row may be inserted with a null phone_number")


@th.django_unit_test("force_refresh on an unnormalizable number is not a 500")
def test_force_refresh_on_unnormalizable_number_is_not_a_500(opts):
    assert opts.client.login(USER_EMAIL, USER_PASSWORD), "login failed"
    resp = opts.client.post(
        "/api/phonehub/number/lookup",
        {"phone_number": BAD_INPUT, "force_refresh": True},
    )
    assert resp.status_code == 200, (
        f"an unnormalizable number must return the documented body, not a 500: "
        f"{resp.status_code}: {resp.response}")
    assert resp.json.get("status") is False, (
        f"expected the documented failure body, got {resp.json}")
    opts.client.logout()


@th.django_unit_test("lookup_data is never serialized")
def test_lookup_data_is_never_serialized(opts):
    from mojo.apps.phonehub.models import PhoneNumber

    phone = PhoneNumber.objects.filter(phone_number=ERR_NUMBER).first()
    assert phone is not None, "the negative-cached row must exist"

    data = phone.to_dict("default")
    assert "lookup_data" not in data, (
        "lookup_data holds raw provider error text and must never be served")
    assert data.get("lookup_unavailable") is True, (
        "clients read lookup_unavailable instead of guessing from is_valid")


@th.django_unit_test("a verdict too stale to refresh reads as unavailable")
def test_stale_verdict_that_cannot_refresh_is_unavailable(opts):
    from mojo.helpers import dates
    from mojo.apps.phonehub.models import PhoneNumber

    stale = PhoneNumber.objects.create(
        phone_number=STALE_NUMBER,
        carrier="Old Carrier",
        is_valid=True,
        lookup_expires_at=dates.subtract(days=1),
        last_lookup_at=dates.subtract(days=91),
        lookup_count=1,
    )
    stale.refresh(lookup_fn=_failing_lookup)
    assert stale.lookup_unavailable is True, (
        "a verdict older than the success TTL that cannot be refreshed is no verdict")

    fresh = PhoneNumber.objects.create(
        phone_number=FRESH_NUMBER,
        carrier="New Carrier",
        is_valid=True,
        lookup_expires_at=dates.subtract(days=1),
        last_lookup_at=dates.subtract(days=1),
        lookup_count=1,
    )
    fresh.refresh(lookup_fn=_failing_lookup)
    assert fresh.lookup_unavailable is False, (
        "a recent successful verdict keeps serving through a transient outage")


@th.django_unit_test("a successful lookup clears the error marker")
def test_success_after_error_clears_the_marker(opts):
    from mojo.helpers import dates
    from mojo.apps.phonehub.models import PhoneNumber

    phone = PhoneNumber(phone_number=GOOD_NUMBER)
    phone.refresh(lookup_fn=_failing_lookup)
    assert phone.lookup_error == ERROR_TEXT, "the row must start negative-cached"

    result = phone.refresh(lookup_fn=_good_lookup)
    assert result is True, f"a successful lookup must report success, got {result!r}"
    assert phone.lookup_error is None, "success must clear the error marker"
    assert phone.lookup_unavailable is False, "a successful lookup is a verdict"
    assert phone.last_lookup_at is not None, "success must stamp last_lookup_at"
    assert phone.lookup_count == 1, f"success must bump lookup_count, got {phone.lookup_count}"
    assert phone.lookup_expires_at > dates.add(days=89), (
        f"a success must be cached for the full TTL, got {phone.lookup_expires_at}")


@th.django_unit_test("phone lookup error cache cleanup")
def test_cleanup(opts):
    from mojo.apps.account.models import User
    from mojo.apps.phonehub.models import PhoneNumber

    PhoneNumber.objects.filter(phone_number__in=TEST_NUMBERS).delete()
    User.objects.filter(email=USER_EMAIL).delete()
