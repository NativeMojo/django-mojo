"""
Tests for the shortlink app — URL shortening, resolution, OG preview, click tracking.
"""
from datetime import timedelta
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_USER = "shortlink_test_user"
TEST_PWORD = "shortlink##mojo99"
REAL_URL_A = "https://github.com/openai"
REAL_URL_B = "https://openai.com/research"


@th.django_unit_setup()
def setup_shortlink(opts):
    from mojo.apps.account.models import User
    from mojo.apps.shortlink.models import ShortLink, ShortLinkClick
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    user = User.objects.filter(username=TEST_USER).last()
    if user is None:
        user = User(username=TEST_USER, email=f"{TEST_USER}@example.com")
        user.save()
    user.save_password(TEST_PWORD)
    user.add_permission(["view_fileman", "manage_shortlinks"])
    user.save()
    opts.user = user

    # Clean up any leftover test data
    ShortLinkClick.objects.all().delete()
    ShortLink.objects.filter(source="test").delete()


# ---------------------------------------------------------------------------
# Unit: ShortLink.create and code generation
# ---------------------------------------------------------------------------

@th.django_unit_test("ShortLink: create stores url, source, and 7-char code")
def test_create(opts):
    from mojo.apps.shortlink.models import ShortLink

    link = ShortLink.create(
        url="https://example.com/long/path?token=abc123",
        source="test",
        user=opts.user,
    )
    assert_true(link.pk is not None, "should have a pk after create")
    assert_eq(len(link.code), 7, f"code should be 7 chars, got {len(link.code)}")
    assert_eq(link.url, "https://example.com/long/path?token=abc123", "url should match")
    assert_eq(link.source, "test", "source should match")
    assert_eq(link.hit_count, 0, "hit_count should start at 0")
    assert_true(link.is_active, "should be active by default")
    opts.link_code = link.code
    opts.link_id = link.pk


@th.django_unit_test("ShortLink: different URLs get different codes")
def test_different_codes(opts):
    from mojo.apps.shortlink.models import ShortLink

    link1 = ShortLink.create(url="https://example.com/a", source="test")
    link2 = ShortLink.create(url="https://example.com/b", source="test")
    assert_true(link1.code != link2.code, f"codes should differ: {link1.code} vs {link2.code}")


# ---------------------------------------------------------------------------
# Unit: Expiry
# ---------------------------------------------------------------------------

@th.django_unit_test("ShortLink: expire_days and expire_hours compute correctly")
def test_expiry_combined(opts):
    from mojo.apps.shortlink.models import ShortLink
    from mojo.helpers import dates

    link = ShortLink.create(url="https://example.com/x", source="test", expire_days=1, expire_hours=2)
    assert_true(link.expires_at is not None, "should have expires_at")
    diff = link.expires_at - dates.utcnow()
    # Should be ~26 hours (allow 1 minute tolerance)
    total_seconds = diff.total_seconds()
    assert_true(25 * 3600 < total_seconds < 27 * 3600,
                f"should expire in ~26 hours, got {total_seconds / 3600:.1f}h")


@th.django_unit_test("ShortLink: expire_days=0 and expire_hours=0 means no expiry")
def test_no_expiry(opts):
    from mojo.apps.shortlink.models import ShortLink

    link = ShortLink.create(url="https://example.com/forever", source="test",
                            expire_days=0, expire_hours=0)
    assert_eq(link.expires_at, None, "expires_at should be None for no-expiry link")
    assert_true(not link.is_expired, "should not be expired")


@th.django_unit_test("ShortLink: expired link returns None from resolve")
def test_resolve_expired(opts):
    from mojo.apps.shortlink.models import ShortLink
    from mojo.helpers import dates

    link = ShortLink.create(url="https://example.com/old", source="test", expire_days=0, expire_hours=0)
    # Force expire
    ShortLink.objects.filter(pk=link.pk).update(expires_at=dates.utcnow() - timedelta(hours=1))
    link.refresh_from_db()
    result = link.resolve()
    assert_eq(result, None, "expired link should resolve to None")


# ---------------------------------------------------------------------------
# Unit: resolve and hit_count
# ---------------------------------------------------------------------------

@th.django_unit_test("ShortLink: resolve returns URL and increments hit_count")
def test_resolve_valid(opts):
    from mojo.apps.shortlink.models import ShortLink

    link = ShortLink.objects.get(pk=opts.link_id)
    url = link.resolve()
    assert_eq(url, "https://example.com/long/path?token=abc123", "should return the destination URL")
    link.refresh_from_db()
    assert_eq(link.hit_count, 1, "hit_count should be 1 after one resolve")


@th.django_unit_test("ShortLink: resolve increments hit_count on each call")
def test_resolve_increments(opts):
    from mojo.apps.shortlink.models import ShortLink

    link = ShortLink.create(url="https://example.com/counter", source="test")
    link.resolve()
    link.resolve()
    link.resolve()
    link.refresh_from_db()
    assert_eq(link.hit_count, 3, f"hit_count should be 3, got {link.hit_count}")


@th.django_unit_test("ShortLink: resolve missing code returns None")
def test_resolve_missing(opts):
    from mojo.apps.shortlink.models import ShortLink

    link = ShortLink(code="ZZZZZZZ", url="https://nope.com", is_active=False)
    result = link.resolve()
    assert_eq(result, None, "inactive link should resolve to None")


# ---------------------------------------------------------------------------
# Unit: metadata and OG
# ---------------------------------------------------------------------------

@th.django_unit_test("ShortLink: custom metadata stored and retrievable")
def test_metadata(opts):
    from mojo.apps.shortlink.models import ShortLink

    link = ShortLink.create(
        url="https://example.com/meta",
        source="test",
        metadata={"og:title": "Test Title", "og:description": "Test Desc"},
    )
    og = link.get_og_metadata()
    assert_eq(og.get("og:title"), "Test Title", "og:title should match")
    assert_eq(og.get("og:description"), "Test Desc", "og:description should match")


@th.django_unit_test("ShortLink: custom metadata overrides scraped metadata")
def test_metadata_override(opts):
    from mojo.apps.shortlink.models import ShortLink

    link = ShortLink.create(
        url="https://example.com/override",
        source="test",
        metadata={
            "og:title": "Custom Title",
            "_scraped": {"og:title": "Scraped Title", "og:image": "https://img.com/pic.jpg"},
        },
    )
    og = link.get_og_metadata()
    assert_eq(og.get("og:title"), "Custom Title", "custom should override scraped")
    assert_eq(og.get("og:image"), "https://img.com/pic.jpg", "scraped keys should fill gaps")


# ---------------------------------------------------------------------------
# Unit: bot detection
# ---------------------------------------------------------------------------

@th.django_unit_test("ShortLink: bot user-agent detection")
def test_bot_detection(opts):
    from mojo.apps.shortlink.models import is_bot_user_agent

    assert_true(is_bot_user_agent("Slackbot-LinkExpanding 1.0"), "Slackbot should be detected")
    assert_true(is_bot_user_agent("facebookexternalhit/1.1"), "Facebook bot should be detected")
    assert_true(is_bot_user_agent("WhatsApp/2.23"), "WhatsApp should be detected")
    assert_true(is_bot_user_agent("com.google.android.apps.messaging"), "Android Messages should be detected")
    assert_true(not is_bot_user_agent("Mozilla/5.0 (iPhone; CPU iPhone OS)"), "normal browser should not be bot")
    assert_true(not is_bot_user_agent(""), "empty UA should not be bot")


# ---------------------------------------------------------------------------
# Unit: click tracking
# ---------------------------------------------------------------------------

@th.django_unit_test("ShortLink: track_clicks=False creates no click record")
def test_no_click_tracking(opts):
    from mojo.apps.shortlink.models import ShortLink, ShortLinkClick
    from testit.helpers import get_mock_request

    link = ShortLink.create(url="https://example.com/no-track", source="test", track_clicks=False)
    request = get_mock_request()
    request.META = {"HTTP_USER_AGENT": "TestBrowser", "HTTP_REFERER": ""}
    request.ip = "127.0.0.1"
    result = link.log_click(request)
    assert_eq(result, None, "should return None when tracking disabled")
    count = ShortLinkClick.objects.filter(shortlink=link).count()
    assert_eq(count, 0, "no click records should exist")


@th.django_unit_test("ShortLink: track_clicks=True logs click with details")
def test_click_tracking(opts):
    from mojo.apps.shortlink.models import ShortLink, ShortLinkClick
    from testit.helpers import get_mock_request

    link = ShortLink.create(url="https://example.com/tracked", source="test", track_clicks=True)
    request = get_mock_request()
    request.META = {"HTTP_USER_AGENT": "TestBrowser/1.0", "HTTP_REFERER": "https://google.com"}
    request.ip = "192.168.1.1"
    click = link.log_click(request)
    assert_true(click is not None, "should return click record")
    assert_eq(click.ip, "192.168.1.1", "ip should match")
    assert_true("TestBrowser" in click.user_agent, "user_agent should contain TestBrowser")
    assert_eq(click.referer, "https://google.com", "referer should match")
    assert_eq(click.is_bot, False, "should not be a bot")


# ---------------------------------------------------------------------------
# Unit: bot_passthrough flag
# ---------------------------------------------------------------------------

@th.django_unit_test("ShortLink: bot_passthrough defaults to False")
def test_bot_passthrough_default(opts):
    from mojo.apps.shortlink.models import ShortLink

    link = ShortLink.create(url="https://example.com/normal", source="test")
    assert_eq(link.bot_passthrough, False, "bot_passthrough should default to False")


@th.django_unit_test("ShortLink: bot_passthrough=True is stored")
def test_bot_passthrough_true(opts):
    from mojo.apps.shortlink.models import ShortLink

    link = ShortLink.create(url="https://example.com/passthrough", source="test", bot_passthrough=True)
    assert_eq(link.bot_passthrough, True, "bot_passthrough should be True")


# ---------------------------------------------------------------------------
# Unit: scraper helpers
# ---------------------------------------------------------------------------

@th.django_unit_test("Scraper: private IP detection blocks internal URLs")
def test_scraper_private_ip(opts):
    from mojo.apps.shortlink.services.scraper import _is_private_url

    assert_true(_is_private_url("http://127.0.0.1/secret"), "localhost should be private")
    assert_true(_is_private_url("http://192.168.1.1/internal"), "RFC1918 should be private")
    assert_true(_is_private_url("http://10.0.0.1/admin"), "10.x should be private")
    assert_true(not _is_private_url("https://example.com/page"), "public domain should not be private")


@th.django_unit_test("Scraper: OG parser extracts meta tags from inline HTML")
def test_og_parser(opts):
    from mojo.apps.shortlink.services.scraper import _OGParser

    html = """<html><head>
    <meta property="og:title" content="Test Page">
    <meta property="og:description" content="A test page">
    <meta name="twitter:card" content="summary">
    </head><body></body></html>"""

    parser = _OGParser()
    parser.feed(html)
    assert_eq(parser.og_tags.get("og:title"), "Test Page", "should extract og:title")
    assert_eq(parser.og_tags.get("og:description"), "A test page", "should extract og:description")
    assert_eq(parser.og_tags.get("twitter:card"), "summary", "should extract twitter:card")


@th.django_unit_test("Scraper: fetch OG tags from GitHub (real HTTP)")
def test_scraper_fetch_github(opts):
    from mojo.apps.shortlink.services.scraper import _fetch_og_tags

    tags = _fetch_og_tags("https://github.com")
    assert_true(len(tags) > 0, f"GitHub should return OG tags, got empty dict")
    assert_true("og:title" in tags, f"GitHub should have og:title, got keys: {list(tags.keys())}")
    assert_true("og:description" in tags, f"GitHub should have og:description, got keys: {list(tags.keys())}")


@th.django_unit_test("Scraper: fetch OG tags from YouTube video (real HTTP)")
def test_scraper_fetch_youtube(opts):
    from mojo.apps.shortlink.services.scraper import _fetch_og_tags

    # YouTube homepage has OG tags
    tags = _fetch_og_tags("https://www.youtube.com")
    assert_true(len(tags) > 0, f"YouTube should return OG tags, got empty dict")
    assert_true("og:title" in tags, f"YouTube should have og:title, got keys: {list(tags.keys())}")


@th.django_unit_test("Scraper: non-HTML URL returns empty tags")
def test_scraper_fetch_non_html(opts):
    from mojo.apps.shortlink.services.scraper import _fetch_og_tags

    # JSON endpoint — not HTML, should return empty
    tags = _fetch_og_tags("https://api.github.com")
    assert_eq(len(tags), 0, "non-HTML response should return empty dict")


# ---------------------------------------------------------------------------
# REST: redirect endpoint
# ---------------------------------------------------------------------------

@th.django_unit_test("REST: /s/<code> redirects with 302 for normal user-agent")
def test_rest_redirect(opts):
    from mojo.apps.shortlink.models import ShortLink

    link = ShortLink.create(url=REAL_URL_A, source="test")
    resp = opts.client.get(f"/s/{link.code}", allow_redirects=False)
    assert_eq(resp.status_code, 302, f"should return 302, got {resp.status_code}")


@th.django_unit_test("REST: /s/<code> returns OG HTML for bot user-agent")
def test_rest_bot_og(opts):
    from mojo.apps.shortlink.models import ShortLink

    link = ShortLink.create(
        url=REAL_URL_A,
        source="test",
        metadata={
            "og:title": "OpenAI on GitHub",
            "og:description": "Browse public repositories.",
            "og:image": "https://opengraph.githubassets.com/1/openai/openai-python",
        },
    )
    bot_agents = [
        "Slackbot-LinkExpanding 1.0",
        "Twitterbot/1.0",
        "WhatsApp/2.23",
    ]
    for user_agent in bot_agents:
        opts.client.headers["User-Agent"] = user_agent
        resp = opts.client.get(f"/s/{link.code}", allow_redirects=False)
        opts.client.headers.pop("User-Agent", None)
        assert_eq(resp.status_code, 200, f"bot user-agent should receive OG HTML, got {resp.status_code}")
        body = resp.response if isinstance(resp.response, str) else str(resp.response)
        assert_true("<meta property=\"og:title\" content=\"OpenAI on GitHub\">" in body, "missing og:title meta")
        assert_true("<meta property=\"og:description\" content=\"Browse public repositories.\">" in body, "missing og:description meta")
        assert_true("<meta property=\"og:image\" content=\"https://opengraph.githubassets.com/1/openai/openai-python\">" in body, "missing og:image meta")
        assert_true("<meta http-equiv=\"refresh\"" in body, "missing meta refresh redirect")


@th.django_unit_test("REST: /s/<code> with bot_passthrough=True always redirects bots")
def test_rest_bot_passthrough(opts):
    from mojo.apps.shortlink.models import ShortLink

    link = ShortLink.create(
        url=REAL_URL_B,
        source="test",
        bot_passthrough=True,
        metadata={"og:title": "Should Not Appear"},
    )
    opts.client.headers["User-Agent"] = "Slackbot-LinkExpanding 1.0"
    resp = opts.client.get(f"/s/{link.code}", allow_redirects=False)
    opts.client.headers.pop("User-Agent", None)
    assert_eq(resp.status_code, 302, f"bot_passthrough should always 302, got {resp.status_code}")


@th.django_unit_test("REST: /s/invalid returns 302 to fallback")
def test_rest_redirect_missing(opts):
    resp = opts.client.get("/s/ZZZZZZZ", allow_redirects=False)
    assert_eq(resp.status_code, 302, f"missing code should return 302 to fallback, got {resp.status_code}")


@th.django_unit_test("REST: /s/<expired> returns 302 to fallback")
def test_rest_redirect_expired(opts):
    from mojo.apps.shortlink.models import ShortLink
    from mojo.helpers import dates

    link = ShortLink.create(url=REAL_URL_B, source="test")
    ShortLink.objects.filter(pk=link.pk).update(expires_at=dates.utcnow() - timedelta(hours=1))
    resp = opts.client.get(f"/s/{link.code}", allow_redirects=False)
    assert_eq(resp.status_code, 302, f"expired should return 302, got {resp.status_code}")


@th.django_unit_test("REST: /api/shortlink/link/create creates a short URL from request.DATA")
def test_rest_link_create_endpoint(opts):
    from mojo.apps.shortlink.models import ShortLink

    opts.client.login(TEST_USER, TEST_PWORD)
    assert_true(opts.client.is_authenticated, "authentication failed")

    payload = {
        "url": REAL_URL_A,
        "source": "test",
        "expire_days": 1,
        "expire_hours": 0,
        "metadata": {
            "og:title": "OpenAI GitHub",
            "og:description": "Open source repositories",
        },
        "track_clicks": True,
        "bot_passthrough": False,
        "is_protected": True,
        "base_url": "https://itf.io",
    }
    resp = opts.client.post("/api/shortlink/link/create", payload)
    assert_eq(resp.status_code, 200, f"expected 200 from /api/shortlink/link/create, got {resp.status_code}")
    assert_true(resp.response.status, "link/create response status should be true")
    assert_true(resp.response.data.short_link.startswith("https://itf.io/s/"), "short_link should use provided base_url")
    assert_eq(resp.response.data.original_url, REAL_URL_A, "original_url should match request url")

    link = ShortLink.objects.filter(url=REAL_URL_A, source="test").order_by("-id").first()
    assert_true(link is not None, "link/create should persist a shortlink row")
    assert_eq(link.track_clicks, True, "track_clicks should be saved")
    assert_eq(link.is_protected, True, "is_protected should be saved")


@th.django_unit_test("REST: /api/shortlink/link list endpoint returns items")
def test_rest_link_list_endpoint(opts):
    from mojo.apps.shortlink.models import ShortLink

    opts.client.login(TEST_USER, TEST_PWORD)
    assert_true(opts.client.is_authenticated, "authentication failed")

    ShortLink.create(url=REAL_URL_A, source="test", user=opts.user)
    ShortLink.create(url=REAL_URL_B, source="test", user=opts.user)

    resp = opts.client.get("/api/shortlink/link")
    assert_eq(resp.status_code, 200, f"expected 200 from /api/shortlink/link, got {resp.status_code}")
    assert_true(resp.response.status, "list response status should be true")
    assert_true(isinstance(resp.response.data, list), "list endpoint should return list data")
    assert_true(len(resp.response.data) > 0, "list endpoint should return at least one row")


@th.django_unit_test("REST: /api/shortlink/history and /history/<id> return click records")
def test_rest_history_endpoints(opts):
    from mojo.apps.shortlink.models import ShortLink

    opts.client.login(TEST_USER, TEST_PWORD)
    assert_true(opts.client.is_authenticated, "authentication failed")

    link = ShortLink.create(url=REAL_URL_A, source="test", track_clicks=True, user=opts.user)
    redirect = opts.client.get(f"/s/{link.code}", allow_redirects=False)
    assert_eq(redirect.status_code, 302, f"expected 302 redirect for click generation, got {redirect.status_code}")

    resp = opts.client.get("/api/shortlink/history", params={"shortlink": link.pk})
    assert_eq(resp.status_code, 200, f"expected 200 from /api/shortlink/history, got {resp.status_code}")
    assert_true(resp.response.status, "history list response status should be true")
    assert_true(isinstance(resp.response.data, list), "history list should return list data")
    assert_true(len(resp.response.data) > 0, "history list should include at least one click")

    click_id = resp.response.data[0].id
    detail = opts.client.get(f"/api/shortlink/history/{click_id}")
    assert_eq(detail.status_code, 200, f"expected 200 from /api/shortlink/history/<id>, got {detail.status_code}")
    assert_true(detail.response.status, "history detail response status should be true")
    assert_eq(detail.response.data.id, click_id, "history detail should return requested record")


# ---------------------------------------------------------------------------
# Unit: shorten() public API
# ---------------------------------------------------------------------------

@th.django_unit_test("shorten() returns full URL with /s/ and code")
def test_shorten_api(opts):
    from mojo.apps.shortlink import shorten

    url = shorten("https://example.com/api-test", source="test", base_url="https://itf.io")
    assert_true("/s/" in url, f"short URL should contain /s/, got {url}")
    assert_true(url.startswith("https://itf.io/s/"), f"should start with base_url/s/, got {url}")
    # Code should be 7 chars after /s/
    code = url.split("/s/")[1]
    assert_eq(len(code), 7, f"code should be 7 chars, got {len(code)}")


@th.django_unit_test("shorten() requires url or file")
def test_shorten_requires_url_or_file(opts):
    from mojo.apps.shortlink import shorten

    try:
        shorten()
        assert_true(False, "should raise ValueError when no url or file")
    except ValueError:
        assert_true(True, "raised ValueError as expected")


# ---------------------------------------------------------------------------
# Unit: is_protected flag
# ---------------------------------------------------------------------------

@th.django_unit_test("ShortLink: is_protected defaults to False")
def test_is_protected_default(opts):
    from mojo.apps.shortlink.models import ShortLink

    link = ShortLink.create(url="https://example.com/unprotected", source="test")
    assert_eq(link.is_protected, False, "is_protected should default to False")


@th.django_unit_test("ShortLink: is_protected=True is stored")
def test_is_protected_true(opts):
    from mojo.apps.shortlink.models import ShortLink

    link = ShortLink.create(url="https://example.com/protected", source="test", is_protected=True)
    assert_eq(link.is_protected, True, "is_protected should be True")


# ---------------------------------------------------------------------------
# Unit: prune expired shortlinks async job
# ---------------------------------------------------------------------------

@th.django_unit_test("Prune: deletes expired unprotected links past grace period")
def test_prune_deletes_expired(opts):
    from mojo.apps.shortlink.models import ShortLink
    from mojo.apps.shortlink.asyncjobs import prune_expired_shortlinks
    from mojo.helpers import dates
    from objict import objict

    # Create a link and force-expire it 10 days ago
    link = ShortLink.create(url="https://example.com/old", source="test")
    ShortLink.objects.filter(pk=link.pk).update(
        expires_at=dates.utcnow() - timedelta(days=10)
    )
    job = objict(payload={})
    result = prune_expired_shortlinks(job)
    assert_true("deleted=" in result, f"should report deletions, got {result}")
    assert_eq(ShortLink.objects.filter(pk=link.pk).count(), 0, "expired link should be deleted")


@th.django_unit_test("Prune: skips protected expired links")
def test_prune_skips_protected(opts):
    from mojo.apps.shortlink.models import ShortLink
    from mojo.apps.shortlink.asyncjobs import prune_expired_shortlinks
    from mojo.helpers import dates
    from objict import objict

    link = ShortLink.create(url="https://example.com/keeper", source="test", is_protected=True)
    ShortLink.objects.filter(pk=link.pk).update(
        expires_at=dates.utcnow() - timedelta(days=10)
    )
    job = objict(payload={})
    prune_expired_shortlinks(job)
    assert_eq(ShortLink.objects.filter(pk=link.pk).count(), 1, "protected link should not be deleted")


@th.django_unit_test("Prune: skips links within grace period")
def test_prune_skips_grace_period(opts):
    from mojo.apps.shortlink.models import ShortLink
    from mojo.apps.shortlink.asyncjobs import prune_expired_shortlinks
    from mojo.helpers import dates
    from objict import objict

    link = ShortLink.create(url="https://example.com/recent-expiry", source="test")
    # Expired 2 days ago — within 7-day grace period
    ShortLink.objects.filter(pk=link.pk).update(
        expires_at=dates.utcnow() - timedelta(days=2)
    )
    job = objict(payload={})
    prune_expired_shortlinks(job)
    assert_eq(ShortLink.objects.filter(pk=link.pk).count(), 1, "recently expired link should survive grace period")


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

@th.django_unit_setup()
def cleanup_shortlink(opts):
    from mojo.apps.shortlink.models import ShortLink, ShortLinkClick

    ShortLinkClick.objects.filter(shortlink__source="test").delete()
    ShortLink.objects.filter(source="test").delete()
