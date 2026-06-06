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
from urllib.parse import urlencode

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

_DEFAULT_CHALLENGE_LOGO = 'https://mojoverify.com/logo.svg'
_DEFAULT_CHALLENGE_BRAND = 'MOJO VERIFY'


def _resolve_group(request):
    """Detect the operator group from the request for white-label branding.

    Detection order:
    1. Hostname → Group.auth_domain lookup (trusted signal, cached in Redis)
    2. ?group_uuid=<uuid> query param (fallback for platforms without custom domains)

    The framework dispatcher reserves `?group=` for integer ID lookup and
    400s on any non-integer value before this view runs, so the UUID path
    must use `?group_uuid=` to be reachable.

    Returns Group instance or None.
    """
    from mojo.apps.account.models import Group

    # 1. Hostname lookup
    try:
        hostname = request.get_host().split(':')[0]  # strip port
        group = Group.resolve_by_auth_domain(hostname)
        if group:
            return group
    except Exception:
        pass

    # 2. Query param fallback
    group_uuid = request.GET.get('group_uuid', '')
    if group_uuid:
        try:
            return Group.objects.filter(uuid=group_uuid, is_active=True).first()
        except Exception:
            pass

    return None
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

    group = _resolve_group(request)
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
            return _serve_login(request, group=group)

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
    return _serve_challenge(request, challenge_tier, page_type='login', group=group)


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

    group = _resolve_group(request)
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
            return _serve_login(request, page_mode='register', group=group)

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
    return _serve_challenge(request, challenge_tier, page_type='registration', group=group)


# ---------------------------------------------------------------------------
# Passkey enrollment page — reusable post-auth page
# ---------------------------------------------------------------------------

_PASSKEY_PATH = settings.get_static('BOUNCER_PASSKEY_PATH', 'passkey')
_ABS_PASSKEY_PATH = f'/{_PASSKEY_PATH}'


@md.GET(_ABS_PASSKEY_PATH)
@md.public_endpoint("Passkey enrollment page (post-registration / account settings)")
def on_passkey_enroll_page(request):
    """Reusable passkey enrollment page.

    Not bouncer-gated — the visitor is already authenticated (the page reads
    the access token from localStorage and runs the WebAuthn registration
    round-trip). Themed by the request's resolved auth config. The hosted
    register page redirects here after signup when `passkey_prompt != off`;
    it is also linkable standalone from account settings.
    """
    if DISABLE_LOGIN:
        from django.http import Http404
        raise Http404("Page not found")

    group = _resolve_group(request)
    ctx = _auth_context(request, group=group)
    ctx['page_mode'] = 'passkey'
    ctx['page_title'] = 'Secure Your Account'
    ctx['subtitle'] = 'Add a passkey'
    # The visitor already holds a session — suppress auth_base's session-check
    # auto-redirect so they land on the enrollment step instead of being
    # bounced straight to success_redirect.
    ctx['skip_session_check'] = True
    return render(request, 'account/passkey_enroll.html', ctx)


def _auth_context(request, group=None):
    """Build the shared template context for auth pages from the group's
    resolved auth config.

    Theme, registration, and login config all come from
    `auth_config.resolve_auth_config(group)` — code defaults, overlaid
    by the AUTH_CONFIG setting, overlaid by `group.metadata["auth_config"]`
    down the parent chain. Single-tenant deployments (group=None) get the
    deployment default.
    """
    from mojo.apps.account.services import register_schema
    from mojo.apps.account.services import auth_config

    cfg = auth_config.resolve_auth_config(group=group, request=request)
    theme = cfg.theme
    login_methods = list(cfg.login.methods or [])
    registration_methods = list(cfg.registration.methods or [])

    login_path = settings.get_static('BOUNCER_LOGIN_PATH', 'auth')
    register_path = settings.get_static('BOUNCER_REGISTER_PATH', 'register')
    passkey_path = settings.get_static('BOUNCER_PASSKEY_PATH', 'passkey')
    group_uuid = group.uuid if group else ''
    # Preserve ?group_uuid= plus the post-auth forwarding params on the
    # switcher links so a user who lands at /auth?redirect=/x and clicks
    # "Create one" carries the redirect onto /register. Whitelisted keys
    # only — OAuth callback (`code`, `state`), magic-link `token`, and
    # reset tokens must NOT bleed across the switch. Mirrors the precedent
    # in _serve_challenge (group_uuid + redirect + back through urlencode).
    fwd_params = {}
    if group_uuid:
        fwd_params['group_uuid'] = group_uuid
    for key in ('redirect', 'next', 'returnTo', 'back'):
        val = request.DATA.get(key) if hasattr(request, 'DATA') else request.GET.get(key)
        if val:
            fwd_params[key] = val
    group_qs = f'?{urlencode(fwd_params)}' if fwd_params else ''

    # Schema-driven register form. The same schema drives the server-side
    # validator, so what the form collects matches what the API will accept.
    register_fields = register_schema.resolve_fields(group=group, request=request)
    register_field_rows = register_schema.field_rows(register_fields)
    # Per-group extra (non-canonical) fields — promo/ref/tracking/etc. Default
    # empty: no extra inputs, no behavior change. Rendered after the canonical
    # fields; the template's JS captures a matching URL query param silently or
    # asks for the value as a plain text input.
    register_extra_fields = register_schema.resolve_extra_fields(group=group, request=request)
    try:
        identity_field = register_schema.resolve_identity_field(register_fields, group=group)
    except Exception:
        identity_field = 'email'
    # When phone is the identity, the forgot-password subview must collect
    # phone (not email) and use the SMS channel.
    forgot_channel = 'sms' if identity_field == 'phone' else 'email'

    # Stepped flow partitioning. When phone has verify="sms" the template
    # renders three .mat-view step containers (identity → SMS code → profile).
    # Otherwise step2_active is False and the template falls back to the
    # single-pane register_field_rows render path.
    step1_fields, step2_active, step3_field_rows = register_schema.partition_for_stepped_flow(
        register_fields)

    return {
        'api_base': theme.api_base or '',
        'success_redirect': theme.success_redirect or '/',
        'logo_url': theme.logo_url or '',
        'favicon_url': theme.favicon_url or '',
        'brand_name': theme.app_title or 'DJANGO MOJO',
        'hero_image_url': theme.hero_image_url or '',
        'hero_headline': theme.hero_headline or 'Welcome back',
        'hero_subheadline': theme.hero_subheadline or 'Admin Portal',
        'back_to_website_url': theme.back_to_website_url or '',
        'login_methods': login_methods,
        # Which view the login page opens on: the SMS phone-entry form when
        # there is no password method (passwordless), else the sign-in form.
        'login_primary': (
            'sms' if ('password' not in login_methods and 'sms' in login_methods)
            else 'signin'),
        'registration_methods': registration_methods,
        'registration_enabled': bool(cfg.registration.enabled),
        'passkey_prompt': cfg.registration.passkey_prompt or 'off',
        'auth_url': f'/{login_path}{group_qs}',
        'register_url': f'/{register_path}{group_qs}',
        'passkey_url': f'/{passkey_path}{group_qs}',
        'auth_layout': theme.layout or 'card',
        'terms_url': theme.terms_url or '',
        'custom_css_url': theme.custom_css_url or '',
        'custom_css': theme.custom_css or '',
        'group_uuid': group_uuid,
        'register_fields': register_fields,
        'register_field_rows': register_field_rows,
        'register_extra_fields': register_extra_fields,
        'register_step1_fields': step1_fields,
        'register_step2_active': step2_active,
        'register_step3_field_rows': step3_field_rows,
        'identity_field': identity_field,
        'forgot_channel': forgot_channel,
    }


def _serve_login(request, page_mode='login', group=None):
    ctx = _auth_context(request, group=group)
    ctx['page_mode'] = page_mode
    if page_mode == 'register':
        ctx['page_title'] = 'Create Account'
        ctx['subtitle'] = 'Create your account'
        return render(request, 'account/register.html', ctx)
    ctx['page_title'] = 'Sign In'
    ctx['subtitle'] = 'Sign in to your account'
    return render(request, 'account/login.html', ctx)


def _serve_challenge(request, challenge_tier=1, page_type='login', group=None):
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
    elif page_type == 'public_message':
        redirect_path = settings.get_static('BOUNCER_CONTACT_PATH', 'contact')
    else:
        redirect_path = settings.get_static('BOUNCER_LOGIN_PATH', 'auth')
    # Preserve group, redirect, and back params through the challenge redirect.
    # Use `group_uuid` (not `group`) because the framework dispatcher reserves
    # `?group=` for integer IDs and rejects UUID values with 400.
    group_uuid = group.uuid if group else ''
    fwd_params = {}
    if group_uuid:
        fwd_params['group_uuid'] = group_uuid
    redirect_val = request.DATA.get('redirect') or request.DATA.get('next') or request.DATA.get('returnTo') or ''
    if redirect_val:
        fwd_params['redirect'] = redirect_val
    back_val = request.DATA.get('back') or ''
    if back_val:
        fwd_params['back'] = back_val
    group_qs = f'?{urlencode(fwd_params)}' if fwd_params else ''
    # Challenge page: default branding from settings, opt-in override per group
    logo_url = settings.get('BOUNCER_CHALLENGE_LOGO_URL', _DEFAULT_CHALLENGE_LOGO, group=group)
    brand_name = settings.get('BOUNCER_CHALLENGE_BRAND', _DEFAULT_CHALLENGE_BRAND, group=group)
    return render(request, 'account/bouncer_challenge.html', {
        'render_ctx': render_ctx,
        'api_base': api_base,
        'login_url': f'/{redirect_path}{group_qs}',
        'page_type': page_type,
        'logo_url': logo_url,
        'brand_name': brand_name,
        'group_uuid': group_uuid,
    })


def _serve_decoy(request):
    api_base = settings.get_static('BOUNCER_API_BASE', '')
    return render(request, 'account/bouncer_decoy.html', {
        'api_base': api_base,
        'logo_url': settings.get_static('BOUNCER_LOGO_URL', ''),
        'accent_color': settings.get_static('BOUNCER_ACCENT_COLOR', ''),
    })


# ---------------------------------------------------------------------------
# Contact / Support page (gated) — reuses bouncer challenge + token flow
# ---------------------------------------------------------------------------

_CONTACT_PATH = settings.get_static('BOUNCER_CONTACT_PATH', 'contact')
_ABS_CONTACT_PATH = f'/{_CONTACT_PATH}'


@md.GET(_ABS_CONTACT_PATH)
@md.public_endpoint("Bouncer-gated public message (contact/support) page")
def on_contact_page(request):
    """Same bouncer gate as login/register — serves challenge, full page, or decoy."""
    from mojo.apps.account.services.bouncer.learner import check_signature_cache
    from mojo.apps.account.services.bouncer.environment import EnvironmentService
    from mojo.apps.account.services.bouncer.scoring import RiskScorer, ScoringContext

    if DISABLE_LOGIN:
        from django.http import Http404
        raise Http404("Page not found")

    group = _resolve_group(request)
    ua = request.user_agent
    fingerprint_id = request.DATA.get('fp', '')
    kind = request.DATA.get('kind', '')

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
            return _serve_contact(request, kind=kind, group=group)

    geo_ip = _geolocate(request.ip)
    server_signals = EnvironmentService.analyze_request(request, geo_ip)
    context = ScoringContext(
        client_signals={},
        server_signals=server_signals,
        device_session=None,
        page_type='public_message',
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
    return _serve_challenge(request, challenge_tier, page_type='public_message', group=group)


def _serve_contact(request, kind='', group=None):
    from mojo.apps.account.services import public_message as svc

    ctx = _auth_context(request, group=group)
    kind_ctx = svc.render_context_for_kind(kind)
    ctx.update(kind_ctx)
    ctx['page_title'] = kind_ctx['kind_title']
    ctx['subtitle'] = kind_ctx['kind_subtitle']
    ctx['contact_submit_url'] = 'account/bouncer/message'
    return render(request, 'account/contact.html', ctx)


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
