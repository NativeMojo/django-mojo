"""
Django-rendered page views for the bouncer gate.

Login page (BOUNCER_LOGIN_PATH, default 'access'):
  1. Check Redis signature cache (IP/subnet/UA) → serve decoy immediately
  2. Check pass cookie → skip challenge, render full login page
  3. Run server-side pre-screen signals → if clearly bot → serve decoy
  4. Otherwise → render randomized challenge page

Decoy pages (common bot paths: /login, /signin):
  Always serve the honeypot login — looks identical to the real thing but
  POSTs to a dead endpoint that logs and returns a plausible error.
"""
import secrets

from django.shortcuts import render

from mojo import decorators as md
from mojo.helpers import logit
from mojo.helpers.settings import settings
from mojo.helpers.response import JsonResponse
from mojo.apps import metrics
from mojo.apps.account.rest.bouncer.assess import (
    _geolocate, _report_bouncer_event, verify_pass_cookie,
)

logger = logit.get_logger('bouncer', 'bouncer.log')

DISABLE_LOGIN = settings.get_static('DISABLE_LOGIN', False)
_LOGIN_PATH = settings.get_static('BOUNCER_LOGIN_PATH', 'auth')
# Absolute path (leading /) so it registers at the root, not under /api/
_ABS_LOGIN_PATH = f'/{_LOGIN_PATH}'
_ABS_LOGIN_PATH2 = f'/{_LOGIN_PATH}/'

# ---------------------------------------------------------------------------
# Real login page (gated)
# ---------------------------------------------------------------------------

@md.GET(_ABS_LOGIN_PATH)
@md.GET(_ABS_LOGIN_PATH2)
@md.public_endpoint("Bouncer-gated login page")
def on_login_page(request):
    """
    Server-side bot gate. Serves challenge page, full login, or decoy based
    on pre-screen results and pass cookie.
    """
    from mojo.apps.account.services.bouncer.learner import check_signature_cache
    from mojo.apps.account.services.bouncer.environment import EnvironmentService
    from mojo.apps.account.services.bouncer.scoring import RiskScorer, ScoringContext

    if DISABLE_LOGIN:
        from django.http import Http404
        raise Http404("Page not found")

    ua = request.user_agent
    fingerprint_id = request.DATA.get('fp', '')

    # 1. Redis signature cache — fastest check, no scoring
    matched, sig_type, sig_value = check_signature_cache(request.ip, ua, fingerprint_id)
    if matched:
        logger.info(f"bouncer: pre-screen blocked by signature {sig_type}:{sig_value} ip={request.ip}")
        try:
            metrics.record("bouncer:pre_screen_blocks", category="bouncer")
        except Exception:
            pass
        return _serve_decoy(request)

    # 2. Pass cookie — known good device, skip challenge
    pass_cookie = request.COOKIES.get('mbp', '')
    if pass_cookie:
        cookie_muid = verify_pass_cookie(pass_cookie, request.ip)
        if cookie_muid:
            return _serve_login(request)

    # 3. Server-side pre-screen (headers + geo)
    geo_ip = _geolocate(request.ip)
    server_signals = EnvironmentService.analyze_request(request, geo_ip)
    # Pre-screen scoring: server signals only (headers + geo).
    # Don't pass request — IdentityAnalyzer would penalize missing cookies
    # that haven't been set yet (first visit). Identity signals are for the
    # assess API call after JS has run, not the page view.
    context = ScoringContext(
        client_signals={},
        server_signals=server_signals,
        device_session=None,
        page_type='login',
        request=None,
    )
    result = RiskScorer.score(context)

    if result.decision == 'block':
        logger.info(f"bouncer: pre-screen blocked ip={request.ip} score={result.score}")
        try:
            metrics.record("bouncer:pre_screen_blocks", category="bouncer")
        except Exception:
            pass
        return _serve_decoy(request)

    # 4. Serve challenge page — tier based on pre-screen risk
    #    Tier 1 (low/unknown): static button, fixed position
    #    Tier 2 (medium):      button shifts to a few predefined spots
    #    Tier 3 (high):        floating/moving button
    if result.score >= 40:
        challenge_tier = 3
    elif result.score >= 20:
        challenge_tier = 2
    else:
        challenge_tier = 1
    return _serve_challenge(request, challenge_tier, page_type='login')


# ---------------------------------------------------------------------------
# Registration page (gated) — same bouncer flow, different page mode
# ---------------------------------------------------------------------------

_REGISTER_PATH = settings.get_static('BOUNCER_REGISTER_PATH', 'register')
_ABS_REGISTER_PATH = f'/{_REGISTER_PATH}'


@md.GET(_ABS_REGISTER_PATH)
@md.public_endpoint("Bouncer-gated registration page")
def on_register_page(request):
    """Same bouncer gate as login — serves challenge, full page, or decoy."""
    from mojo.apps.account.services.bouncer.learner import check_signature_cache
    from mojo.apps.account.services.bouncer.environment import EnvironmentService
    from mojo.apps.account.services.bouncer.scoring import RiskScorer, ScoringContext

    if DISABLE_LOGIN:
        from django.http import Http404
        raise Http404("Page not found")

    ua = request.user_agent
    fingerprint_id = request.DATA.get('fp', '')

    matched, sig_type, sig_value = check_signature_cache(request.ip, ua, fingerprint_id)
    if matched:
        logger.info(f"bouncer: pre-screen blocked by signature {sig_type}:{sig_value} ip={request.ip}")
        try:
            metrics.record("bouncer:pre_screen_blocks", category="bouncer")
        except Exception:
            pass
        return _serve_decoy(request)

    pass_cookie = request.COOKIES.get('mbp', '')
    if pass_cookie:
        cookie_muid = verify_pass_cookie(pass_cookie, request.ip)
        if cookie_muid:
            return _serve_login(request, page_mode='register')

    geo_ip = _geolocate(request.ip)
    server_signals = EnvironmentService.analyze_request(request, geo_ip)
    context = ScoringContext(
        client_signals={},
        server_signals=server_signals,
        device_session=None,
        page_type='registration',
        request=None,
    )
    result = RiskScorer.score(context)

    if result.decision == 'block':
        logger.info(f"bouncer: pre-screen blocked ip={request.ip} score={result.score}")
        try:
            metrics.record("bouncer:pre_screen_blocks", category="bouncer")
        except Exception:
            pass
        return _serve_decoy(request)

    if result.score >= 40:
        challenge_tier = 3
    elif result.score >= 20:
        challenge_tier = 2
    else:
        challenge_tier = 1
    return _serve_challenge(request, challenge_tier, page_type='registration')


def _auth_context(request):
    """Build the shared template context for auth pages from settings.

    Uses settings.get() (DB-backed with file fallback) so all values are
    configurable at runtime via the Setting model / admin portal.
    """
    login_path = settings.get_static('BOUNCER_LOGIN_PATH', 'auth')
    register_path = settings.get_static('BOUNCER_REGISTER_PATH', 'register')
    return {
        'api_base': settings.get('AUTH_API_BASE', ''),
        'success_redirect': settings.get('AUTH_SUCCESS_REDIRECT', '/'),
        'logo_url': settings.get('AUTH_LOGO_URL', 'https://mojo-verify.s3.amazonaws.com/signatures/14e7aab75c2749cb846f7d57298691ac/mojo_logo_f97e2d0a.png'),
        'favicon_url': settings.get('AUTH_FAVICON_URL', ''),
        'brand_name': settings.get('AUTH_APP_TITLE', 'DJANGO MOJO'),
        'hero_image_url': settings.get('AUTH_HERO_IMAGE_URL', 'https://mojo-verify.s3.amazonaws.com/signatures/14e7aab75c2749cb846f7d57298691ac/purple_dunes_lake_2_bd730023.png'),
        'hero_headline': settings.get('AUTH_HERO_HEADLINE', 'Welcome back'),
        'hero_subheadline': settings.get('AUTH_HERO_SUBHEADLINE', 'Admin Portal'),
        'back_to_website_url': settings.get('AUTH_BACK_TO_WEBSITE_URL', ''),
        'enable_google': settings.get('AUTH_ENABLE_GOOGLE', False, kind='bool'),
        'enable_apple': settings.get('AUTH_ENABLE_APPLE', False, kind='bool'),
        'enable_passkeys': settings.get('AUTH_ENABLE_PASSKEYS', False, kind='bool'),
        'auth_url': f'/{login_path}',
        'register_url': f'/{register_path}',
        'auth_layout': settings.get('AUTH_LAYOUT', 'card'),
        'terms_url': settings.get('AUTH_TERMS_URL', ''),
        'custom_css_url': settings.get('AUTH_CUSTOM_CSS_URL', ''),
        'custom_css': settings.get('AUTH_CUSTOM_CSS', ''),
    }


def _serve_login(request, page_mode='login'):
    ctx = _auth_context(request)
    ctx['page_mode'] = page_mode
    if page_mode == 'register':
        ctx['page_title'] = 'Create Account'
        ctx['subtitle'] = 'Create your account'
        return render(request, 'account/register.html', ctx)
    ctx['page_title'] = 'Sign In'
    ctx['subtitle'] = 'Sign in to your account'
    return render(request, 'account/login.html', ctx)


def _serve_challenge(request, challenge_tier=1, page_type='login'):
    render_ctx = {
        'css_nonce': secrets.token_hex(6),
        'hp_field': secrets.token_hex(6),
        'btn_seed': secrets.token_hex(6),
        'challenge_tier': challenge_tier,
    }
    api_base = settings.get_static('BOUNCER_API_BASE', '')
    # After challenge, redirect back to the page that sent them here
    if page_type == 'registration':
        redirect_path = settings.get_static('BOUNCER_REGISTER_PATH', 'register')
    else:
        redirect_path = settings.get_static('BOUNCER_LOGIN_PATH', 'auth')
    return render(request, 'account/bouncer_challenge.html', {
        'render_ctx': render_ctx,
        'api_base': api_base,
        'login_url': f'/{redirect_path}',
        'page_type': page_type,
        'logo_url': 'https://mojoverify.com/logo.svg',
    })


def _serve_decoy(request):
    api_base = settings.get_static('BOUNCER_API_BASE', '')
    return render(request, 'account/bouncer_decoy.html', {
        'api_base': api_base,
        'logo_url': settings.get_static('BOUNCER_LOGO_URL', ''),
        'accent_color': settings.get_static('BOUNCER_ACCENT_COLOR', ''),
    })


# ---------------------------------------------------------------------------
# Decoy honeypot pages — registered at common bot-scanned paths
# ---------------------------------------------------------------------------

@md.GET('/login')
@md.GET('/signin')
@md.GET('/signup')
@md.public_endpoint("Bouncer honeypot decoy page at common bot-scanned paths")
def on_decoy_page(request):
    if DISABLE_LOGIN:
        from django.http import Http404
        raise Http404("Page not found")

    return _serve_decoy(request)


# ---------------------------------------------------------------------------
# Decoy dead endpoint — receives form POSTs from the honeypot page.
# Registered at the same paths the bot scanned to find the page.
# Always returns a plausible-looking failure with a realistic delay.
# ---------------------------------------------------------------------------

@md.POST('/login')
@md.POST('/signin')
@md.POST('/signup')
@md.public_endpoint("Bouncer honeypot dead endpoint — logs credential attempts")
def on_decoy_post(request):
    if DISABLE_LOGIN:
        from django.http import Http404
        raise Http404("Page not found")

    import time
    muid = request.muid or ''
    duid = request.DATA.get('duid') or request.duid or ''
    username = request.DATA.get('username', '')

    try:
        metrics.record("bouncer:honeypot_catches", category="bouncer")
    except Exception:
        pass
    _report_bouncer_event(
        'security:bouncer:honeypot_post',
        f"Honeypot POST received: username={username} muid={muid} ip={request.ip}",
        level=9, request=request,
        muid=muid, duid=duid, username=username,
    )

    time.sleep(0.3)
    return JsonResponse({'status': False, 'error': 'Invalid credentials'}, status=401)
