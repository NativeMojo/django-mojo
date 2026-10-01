"""Maestro item #6225 — a token link only points at a trusted frontend.

`password_reset`, `magic_login` and `invite` links carry a sign-in token, so
their host must be one the operator configured or the frontend of the tenant
that created the account. A request can choose between those and can never add
one, and the framework never fetches such a link itself.

No settings are changed here: operator origins go in through the
`operator_origins=` seam, and tenant values through a test-owned org, as in
tests/test_account/test_create_user_command.py. The over-HTTP regression lives
in tests/test_auth/token_link_host.py.
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

SCRAPE_JOB = "mojo.apps.shortlink.services.scraper.scrape_og_metadata"
EMAIL_SCRAPE = "wut_scrape@test.com"
# `.invalid` never resolves, so the control job below can fetch nothing even
# where a jobs runner picks it up.
TOKEN_URL = "https://wut-frontend.invalid/auth?flow=magic_login&token=ml:wut"
CONTROL_URL = "https://wut-control.invalid/page"


def _scrape_jobs(short_url):
    from mojo.apps.jobs.models import Job
    from mojo.apps.shortlink.models import ShortLink

    link = ShortLink.objects.get(code=short_url.rsplit("/", 1)[-1])
    return link, Job.objects.filter(func=SCRAPE_JOB, payload__shortlink_id=link.pk)


@th.django_unit_setup()
def setup_webapp_url_trust(opts):
    from mojo.apps.account.models import User
    from mojo.apps.shortlink.models import ShortLink

    ShortLink.objects.filter(source__startswith="wut_").delete()
    User.objects.filter(email__in=[EMAIL_SCRAPE]).delete()
    opts.scrape_user = User.objects.create(
        username=EMAIL_SCRAPE, email=EMAIL_SCRAPE, is_active=True)


@th.django_unit_test("token short links never queue a fetch of their destination")
def test_token_shortlink_queues_no_scrape(opts):
    from mojo.apps.shortlink import maybe_shorten_url, shorten

    short = maybe_shorten_url(TOKEN_URL, source="wut_magic_login",
                              user=opts.scrape_user, expire_hours=1)
    assert_true(short != TOKEN_URL, "the token link must have been shortened")
    link, jobs = _scrape_jobs(short)
    assert_eq(link.url, TOKEN_URL, "the short link must keep the token URL as its destination")
    assert_true(link.bot_passthrough is False,
                "token links must keep bot_passthrough=False so preview bots can't use the token")
    assert_eq(jobs.count(), 0,
              "maybe_shorten_url must not queue a scrape job: it would fetch the token URL")

    short = shorten(TOKEN_URL, source="wut_scrape_off", user=opts.scrape_user,
                    expire_hours=1, scrape=False)
    _, jobs = _scrape_jobs(short)
    assert_eq(jobs.count(), 0, "shorten(scrape=False) must not queue a scrape job")

    # Control: an ordinary short link still gets its preview scrape.
    short = shorten(CONTROL_URL, source="wut_scrape_on", user=opts.scrape_user, expire_hours=1)
    _, jobs = _scrape_jobs(short)
    assert_eq(jobs.count(), 1, "shorten() without scrape=False must still queue the scrape job")
    jobs.delete()
