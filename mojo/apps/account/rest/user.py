import time
from django.db import transaction
from mojo import decorators as md
from mojo.apps.account.utils.jwtoken import JWToken
# from django.http import JsonResponse
from mojo.helpers.response import JsonResponse
from django.shortcuts import render
from django.http import HttpResponseRedirect
from mojo.apps.account.models.user import User
from mojo.apps.account.services import extensions as account_extensions
from mojo.apps.account.services import auth_config
from mojo.apps.account.utils import tokens
from mojo.apps.account.utils.webapp_url import build_token_url
from mojo.apps.shortlink import maybe_shorten_url
from mojo.helpers import dates, crypto
from mojo import errors as merrors
from mojo.helpers.settings import settings


@md.URL('user')
@md.URL('user/<int:pk>')
def on_user(request, pk=None):
    return User.on_rest_request(request, pk)


@md.URL('user/me')
@md.URL('account/user/me')
@md.requires_auth()
def on_user_me(request):
    if not hasattr(request.user, "is_request_user"):
        raise merrors.PermissionDeniedException("not valid user", 401, 401)
    return User.on_rest_request(request, request.user.pk)


@md.POST('auth/manage/clear_rate_limit')
@md.requires_global_perms("users", "manage_users")
def on_clear_rate_limit(request):
    """Clear rate limit counters for an IP, device, client, or user account.

    Optional body params:
      ip       — clear all srl/rl keys for this IP (optionally scoped via key)
      key      — limit bucket name (e.g. "login"); required for duid/muid/account
      duid     — clear the device counter (requires key)
      muid     — clear the client cookie counter (requires key)
      user_id  — clear the per-account counter for this user (requires key)
      username — resolve to user_id and clear the per-account counter (requires key)
    """
    from mojo.decorators.limits import clear_rate_limits
    ip = request.DATA.get("ip")
    key = request.DATA.get("key")
    duid = request.DATA.get("duid")
    muid = request.DATA.get("muid")
    user_id = request.DATA.get("user_id")
    username = request.DATA.get("username")

    account_id = None
    if user_id:
        try:
            account_id = int(user_id)
        except (TypeError, ValueError):
            raise merrors.ValueException("Invalid user_id")
    elif username:
        target = User.objects.filter(username=username).first()
        if target is None:
            raise merrors.ValueException("Unknown username")
        account_id = target.pk

    # account-scope clears need a key bucket to target the right counter
    if account_id is not None and not key:
        key = "login"

    deleted = clear_rate_limits(ip=ip, key=key, duid=duid, muid=muid, account_id=account_id)
    return JsonResponse({"status": True, "data": {"deleted": deleted}})


@md.GET('auth/manage/throttle')
@md.requires_global_perms("users", "manage_users")
def on_read_throttle(request):
    """Read the per-account login attempt counter for support tooling.

    Query params:
      user_id  — resolve by user id
      username — resolve by username (alternative to user_id)
      key      — limit bucket name (default "login"; only "login" supported in v1)

    Returns: {count, limit, window, retry_after_seconds}.
    Reading does not affect the counter — use clear_rate_limit to reset.
    """
    from mojo.decorators.limits import read_account_attempt

    key = request.DATA.get("key", "login")
    if key != "login":
        raise merrors.ValueException("only key='login' is supported")

    user_id = request.DATA.get("user_id")
    username = request.DATA.get("username")

    if user_id:
        try:
            account_id = int(user_id)
        except (TypeError, ValueError):
            raise merrors.ValueException("Invalid user_id")
        if not User.objects.filter(pk=account_id).exists():
            raise merrors.ValueException("Unknown user_id")
    elif username:
        target = User.objects.filter(username=username).first()
        if target is None:
            raise merrors.ValueException("Unknown username")
        account_id = target.pk
    else:
        raise merrors.ValueException("user_id or username is required")

    limit = settings.get("LOGIN_USERNAME_LIMIT", 10, kind="int")
    window = settings.get("LOGIN_USERNAME_WINDOW", 900, kind="int")
    data = read_account_attempt(key, account_id, limit=limit, window=window)
    return JsonResponse({"status": True, "data": data})


@md.POST('refresh_token')
@md.POST('token/refresh')
@md.POST("auth/token/refresh")
@md.POST('account/jwt/refresh')
@md.rate_limit("refresh_token", ip_limit=30)
@md.requires_params("refresh_token")
def on_refresh_token(request):
    user, error = User.validate_jwt(request.DATA.refresh_token)
    if error is not None:
        raise merrors.PermissionDeniedException(error, 401, 401)
    # future look at keeping the refresh token the same but updating the access_token
    # TODO add device id to the token as well
    # user.touch()
    keys = dict(uid=user.id)
    # Carry the ORIGINAL auth_time forward unchanged — a refresh is NOT a fresh
    # authentication. Resetting it would defeat the step-up freshness gate; dropping
    # it would force a needless re-auth. Absent on legacy refresh tokens => omit.
    prior = JWToken().decode(request.DATA.refresh_token, validate=False)
    if prior.get("auth_time") is not None:
        keys["auth_time"] = prior.get("auth_time")
    token_package = JWToken(user.get_auth_key()).create(**keys)
    return JsonResponse(dict(status=True, data=token_package))


@md.POST("login")
@md.POST("auth/login")
@md.POST('account/jwt/login')
@md.strict_rate_limit("login", ip_limit=100,
                      muid_limit=10, muid_window=300,
                      duid_limit=10, duid_window=300)
@md.endpoint_metrics("login_attempts", by=["ip", "muid"])
@md.requires_params("password")
@md.requires_bouncer_token('login')
@md.requires_geofence(scope="auth", after_auth=True)
def on_user_login(request):
    from mojo.decorators.limits import check_account_attempt, clear_rate_limits

    username = request.DATA.username
    password = request.DATA.password

    # UX-only per-group method gate. No-op unless a group_uuid on the request
    # resolves a group whose auth config disables password login.
    auth_config.assert_login_method(
        "password", auth_config.resolve_group_from_request(request))

    user, source = User.lookup_from_request_with_source(request, phone_as_username=settings.get("ALLOW_PHONE_LOGIN", False, kind="bool"))
    if user is None:
        User.class_report_incident(
            f"login attempt with unknown username {username}",
            event_type="login:unknown",
            level=8,
            request=request)
        raise merrors.PermissionDeniedException("Invalid username or password", 401, 401)

    # Per-account sliding-window throttle — bypass-resistant. Key on user.id so
    # IP/duid/muid rotation does not let an attacker keep guessing one account.
    acct_limit = settings.get("LOGIN_USERNAME_LIMIT", 10, kind="int")
    acct_window = settings.get("LOGIN_USERNAME_WINDOW", 900, kind="int")
    _, blocked = check_account_attempt("login", user.pk, acct_limit, acct_window, request=request)
    if blocked is not None:
        return blocked

    if not user.check_password(password):
        # level=5 feeds the invalid_password ruleset for IP-block escalation.
        user.report_incident(
            f"{user.username} enter an invalid password",
            "invalid_password",
            level=5)
        raise merrors.PermissionDeniedException("Invalid username or password", 401, 401)

    # Successful password — clear the per-account counter so prior bad attempts
    # do not penalise this session. Done before MFA gate too, since the password
    # check passed and a stolen-password retry would not get this far.
    clear_rate_limits(key="login", account_id=user.pk)

    # Verification gate: source here is the lookup channel ("email"/"phone_number"/
    # "username") used to find the account. The gate enforces REQUIRE_VERIFIED_EMAIL
    # / REQUIRE_VERIFIED_PHONE only for the matching channel.
    _check_verification_gate(user, source)

    mfa_methods = get_mfa_methods(user)
    if mfa_methods:
        # Post-credential geofence (DM-043): the MFA branch never reaches
        # jwt_login, so enforce here — a blocked user must not receive an
        # mfa_token. Non-MFA logins are checked once, inside jwt_login.
        from mojo.apps.account.services.geofence import enforcement
        blocked = enforcement.enforce(request, scope="auth", user=user)
        if blocked is not None:
            return blocked
        return mfa_required_response(user, mfa_methods)
    return jwt_login(request, user, "account/jwt/login" in request.path, source="password")


# -----------------------------------------------------------------
# Cross-origin auth handoff (authorization-code style)
# -----------------------------------------------------------------

@md.POST("auth/handoff")
@md.requires_auth()
@md.rate_limit("auth_handoff", ip_limit=30)
@md.requires_geofence(scope="auth")
def on_auth_handoff(request):
    """
    Issue a short-lived, single-use handoff code for the authenticated user.
    The auth-origin page calls this when redirecting to a different-origin app
    so the app can exchange the code for a JWT without the JWT touching the URL.
    """
    from mojo.apps.account.services import auth_handoff
    code = auth_handoff.create_handoff_code(request.user, ip=request.ip)
    return JsonResponse({
        "status": True,
        "data": {
            "code": code,
            "expires_in": auth_handoff.get_ttl(),
        },
    })


@md.POST("auth/exchange")
@md.public_endpoint()
@md.strict_rate_limit("auth_exchange", ip_limit=20, ip_window=60)
@md.requires_geofence(scope="auth", after_auth=True)
@md.requires_params("code")
def on_auth_exchange(request):
    """
    Exchange a handoff code for an access + refresh token pair. Public so the
    consuming app can call it without an existing JWT, single-use, rate-limited.
    """
    from mojo.apps.account.services import auth_handoff
    data = auth_handoff.consume_handoff_code(request.DATA.get("code"))
    if not data:
        raise merrors.PermissionDeniedException("Invalid or expired handoff code", 401, 401)
    user = User.objects.filter(pk=data.get("uid")).first()
    if user is None:
        raise merrors.PermissionDeniedException("Invalid or expired handoff code", 401, 401)
    if not user.is_active:
        raise merrors.PermissionDeniedException("Account is disabled", 403, 403)
    return jwt_login(request, user, source="handoff")


@md.POST("auth/register")
@md.public_endpoint()
@md.strict_rate_limit("register", ip_limit=5, ip_window=300)
@md.requires_bouncer_token('registration')
@md.requires_geofence(scope="auth")
def on_register(request):
    """
    Create a new user account.

    Gated by the ALLOW_USER_REGISTRATION setting (default False).

    Field set is driven by AUTH_REGISTER_FIELDS (group-scoped); see
    `mojo.apps.account.services.register_schema`. Default config (when the
    setting is unset) preserves the legacy email-based form: required email
    + password, optional first_name + last_name.

    Required body params: every field marked required in the configured
    schema. `password` is required only when it is part of the schema — a
    schema that omits `password` creates a passwordless account (the schema
    must then include an SMS-verified phone, and the user logs in by SMS
    code). The identity field (email or phone, auto-picked) is always required.

    Optional body params: anything not required in the schema, plus any
    keys listed in REGISTRATION_EXTRA_FIELDS, plus `group_uuid`.

    When the schema requires phone with verify="sms", the request must
    include `verified_phone_token` minted by /auth/phone/register/verify.

    Phone identity: when the SMS-verified phone already belongs to an
    account, the requester has proven phone ownership, so they are signed
    into that existing account instead of creating a duplicate — the
    submitted profile fields are ignored. If a `group_uuid` is supplied and
    the account is not yet a member of that group, it is added and
    USER_REGISTERED_HANDLER fires for the group (per-group setup still runs).

    Extension points (unchanged):
      PRE_REGISTER_VALIDATOR, USER_REGISTERED_HANDLER, USER_LOGIN_HANDLER.
    """
    from mojo.apps.account.services import register_schema
    from mojo.apps.account.services import phone_register

    allow_registration = account_extensions.bool_setting_with_header(
        request, "X-Mojo-Test-Allow-User-Registration", "ALLOW_USER_REGISTRATION", False)
    if not allow_registration:
        raise merrors.PermissionDeniedException("Registration is not enabled", 403, 403)

    # ---- Resolve group first so schema lookups can be group-scoped ---------
    group = None
    group_uuid = (request.DATA.get("group_uuid") or "").strip()
    require_group = account_extensions.bool_setting_with_header(
        request, "X-Mojo-Test-Require-Group-On-Registration",
        "REQUIRE_GROUP_ON_REGISTRATION", False)
    if group_uuid:
        from mojo.apps.account.models.group import Group
        group = Group.objects.filter(uuid=group_uuid).first()
        if group is None:
            raise merrors.ValueException("Unknown group")
        # Effective activeness (DM-048): a deactivated ancestor darkens the
        # group too — registration against any group of a suspended subtree
        # behaves exactly like an inactive group.
        if not group.is_effectively_active():
            raise merrors.ValueException("Group is not active")
    elif require_group:
        raise merrors.ValueException("group_uuid is required")

    # ---- Per-group registration toggle -------------------------------------
    # Layered on the global ALLOW_USER_REGISTRATION kill-switch above: a group
    # can disable signup for its own app via auth config.
    if not auth_config.resolve_auth_config(
            group=group, request=request).registration.enabled:
        raise merrors.PermissionDeniedException("Registration is not enabled", 403, 403)

    # ---- Resolve schema + identity for this group --------------------------
    # Test-mode override mirrors the existing X-Mojo-Test-* pattern so per-
    # request configs don't require a server reload.
    fields = register_schema.resolve_fields(group=group, request=request)
    identity_field = register_schema.resolve_identity_field(fields, group=group)
    min_age = register_schema.resolve_min_age(group=group, request=request)
    by_name = {f["name"]: f for f in fields}

    # ---- Passwordless registration guard -----------------------------------
    # A schema that omits `password` creates an account with no usable
    # password — it must provide an SMS-verified phone so the account still
    # has a working login path. validate_fields_config enforces this when a
    # group's auth config is saved, but the deployment-wide AUTH_CONFIG setting
    # and the X-Mojo-Test-Register-Fields header bypass that — re-check here.
    has_password = "password" in by_name
    if not has_password:
        phone_field = by_name.get("phone")
        if not phone_field or phone_field.get("verify") != "sms":
            raise merrors.ValueException(
                "Passwordless registration requires a phone with SMS verification")

    # ---- Build payload dict from request.DATA (only canonical fields) ------
    raw_payload = {}
    for name in register_schema.CANONICAL_FIELDS:
        if name in request.DATA:
            raw_payload[name] = request.DATA.get(name)

    # Allowlisted extras — silent-drop unknown keys (existing contract).
    # Resolved up-front so both the new-user path and the existing-account
    # path below hand the same `extra` dict to USER_REGISTERED_HANDLER.
    # Allowlist = legacy global REGISTRATION_EXTRA_FIELDS ∪ the names this group
    # declares in auth_config.registration.extra_fields. The group config both
    # renders the input and authorizes its capture; the global setting keeps
    # existing deployments working with no migration.
    extras_allow = account_extensions.list_setting_with_header(
        request, "X-Mojo-Test-Registration-Extra-Fields",
        "REGISTRATION_EXTRA_FIELDS", [])
    group_extra_fields = register_schema.resolve_extra_fields(group=group, request=request)
    allow = set(extras_allow) | set(register_schema.extra_field_names(group_extra_fields))
    extra = {key: request.DATA.get(key) for key in allow if key in request.DATA}

    # ---- Existing-account short-circuit (phone identity) -------------------
    # Detect an account that already owns this phone BEFORE full payload
    # validation, so a returning user can finish with just phone + verified
    # token — no profile fields. The SMS-verified token proves they control
    # the number (same proof as SMS login), so they are signed in. Without
    # SMS verification ownership is unproven → reject as a duplicate.
    if identity_field == "phone":
        raw_phone = raw_payload.get("phone")
        norm_phone = User.normalize_phone(str(raw_phone)) if raw_phone else None
        existing = (User.objects.filter(phone_number=norm_phone).first()
                    if norm_phone else None)
        if existing is not None:
            phone_sms_verified = (
                "phone" in by_name and by_name["phone"].get("verify") == "sms")
            if not phone_sms_verified:
                raise merrors.ValueException(
                    "An account with this phone number already exists")
            verified_token = request.DATA.get("verified_phone_token", "")
            if not verified_token:
                raise merrors.ValueException("Phone verification required")
            if not phone_register.consume(verified_token, norm_phone):
                raise merrors.ValueException("Invalid or expired phone verification")
            try:
                if not existing.is_active:
                    raise merrors.PermissionDeniedException("Account is disabled", 403, 403)
                if not existing.is_phone_verified:
                    existing.is_phone_verified = True
                    existing.save(update_fields=["is_phone_verified", "modified"])
                # Joining a group the user is not yet a member of is a
                # registration *into that group*: create the GroupMember and fire
                # USER_REGISTERED_HANDLER so per-group setup runs. Already a member
                # (or no group_uuid) → pure login, no handler. Atomic so a raising
                # handler does not leave a dangling membership.
                from mojo.apps.account.models.member import GroupMember
                if group is not None and not GroupMember.objects.filter(
                        user=existing, group=group).exists():
                    with transaction.atomic():
                        GroupMember.objects.get_or_create(user=existing, group=group)
                        account_extensions.fire_user_registered(
                            user=existing, request=request, group=group,
                            source="sms", extra=extra)
            except Exception:
                # The single-use token was consumed above. If the work failed
                # (e.g. a per-group register handler raised), restore it so the user
                # can retry without re-verifying their phone. report_incident +
                # jwt_login stay OUTSIDE this guard: a post-handler failure must keep
                # the token consumed (no double-fire of the handler on retry).
                phone_register.restore(verified_token, norm_phone)
                raise
            existing.report_incident(
                f"{existing.username} signed in via the register flow "
                f"(phone already registered)",
                "register:existing_account_login")
            # Looks like a registration to the caller; it is really a login
            # into the existing account — submitted profile fields are ignored.
            return jwt_login(request, existing, source="sms", is_new_user=False)

    # ---- Validate the payload for a NEW registration ----------------------
    sanitized = register_schema.validate_payload(
        fields, raw_payload, identity_field=identity_field, min_age=min_age)

    email = sanitized.get("email", "")
    phone = sanitized.get("phone", "")
    password = sanitized.get("password")
    first_name = sanitized.get("first_name", "")
    last_name = sanitized.get("last_name", "")
    dob = sanitized.get("dob")

    # An existing email is a hard duplicate — email ownership is not proven at
    # registration time, so we cannot sign them in (unlike the phone path).
    if identity_field == "email" and email and User.objects.filter(email=email).exists():
        raise merrors.ValueException("An account with this email already exists")

    # PRE_REGISTER_VALIDATOR — may raise ValueException → 400.
    # Strip plaintext password from request.DATA before calling the validator
    # so a consumer-written handler literally cannot reach it (defense-in-depth
    # beyond "we don't pass it as a kwarg"). Restored in finally so downstream
    # code that re-reads request.DATA still works.
    _password_pop = request.DATA.pop("password", None)
    try:
        account_extensions.run_pre_register_validator(
            email=email, group=group, request=request, extra=extra)
    finally:
        if _password_pop is not None:
            request.DATA["password"] = _password_pop

    # Strength check (still framework-side; outside atomic for clarity).
    # Skipped for passwordless registration — there is no password.
    if has_password:
        User(email=email or None).check_password_strength(password)

    # ---- Phone-verify consumption (BEFORE atomic block) --------------------
    # If the schema marks phone with verify="sms", a valid verified-phone
    # token must accompany the register POST. Consumed here, not inside the
    # atomic block, so a misplaced retry can't roll back a real user row.
    phone_was_verified = False
    if "phone" in by_name and by_name["phone"].get("verify") == "sms":
        verified_token = request.DATA.get("verified_phone_token", "")
        if not verified_token:
            raise merrors.ValueException("Phone verification required")
        if not phone_register.consume(verified_token, phone):
            raise merrors.ValueException("Invalid or expired phone verification")
        phone_was_verified = True

    # ---- Atomic: user + GroupMember + register-handler ---------------------
    # Verify-email send and jwt_login run OUTSIDE this block. An SMTP hiccup
    # or JWT-issuance failure must NOT roll back a user whose register-handler
    # has already fired side effects in consumer downstream systems.
    try:
        with transaction.atomic():
            user = User(email=email or None)
            if first_name:
                user.first_name = first_name
            if last_name:
                user.last_name = last_name
            if phone:
                user.phone_number = phone
            if dob is not None:
                user.dob = dob
            if phone_was_verified:
                user.is_phone_verified = True
            if identity_field == "email" and email:
                user.username = user.generate_username_from_email()
            else:
                # Phone-as-identity: prefer first.last (lowercased + sanitized,
                # numeric suffix on collision) so the username is human-readable.
                # Falls back to the normalized phone when no names are supplied
                # or every candidate collides.
                user.username = user.generate_username_from_names(fallback=phone)
            if has_password:
                user.set_password(password)
            else:
                # Passwordless account — no usable password; the user logs in by
                # SMS code (or an enrolled passkey).
                user.set_unusable_password()
            # on_rest_pre_save / on_rest_created don't fire on direct .save(),
            # so mirror their profile setup explicitly: infer first/last from a
            # business email, backfill display_name, then run the same content
            # guard the REST path would (blocks profanity in name fields).
            user.infer_names_from_email()
            if not user.display_name:
                user.display_name = user.generate_display_name()
            user.validate_name_fields({}, created=True)
            # Persist captured extras (promo/ref/tracking/etc.) under a dedicated
            # metadata namespace. Merged so other metadata keys are untouched.
            # USER_REGISTERED_HANDLER still receives `extra` below as well.
            if extra:
                meta = user.metadata or {}
                reg = meta.get("registration") or {}
                reg.update(extra)
                meta["registration"] = reg
                user.metadata = meta
            user.save()

            if group is not None:
                from mojo.apps.account.models.member import GroupMember
                GroupMember.objects.get_or_create(user=user, group=group)

            account_extensions.fire_user_registered(
                user=user, request=request, group=group,
                source="password", extra=extra)
    except Exception:
        # The single-use token was consumed at line ~453, before this block. If
        # user creation / the register handler failed, the user row rolled back —
        # restore the token so the user can retry. (Post-atomic failures below keep
        # the token consumed: the user exists, a retry must not duplicate it.)
        if phone_was_verified:
            phone_register.restore(verified_token, phone)
        raise

    # ---- Side effects (outside atomic) -------------------------------------
    # Email-verify only fires when email is configured. Phone-only registers
    # don't trigger an email send and don't return the requires_verification
    # response either — the verified-phone-token flow has already proven
    # ownership of the contact channel.
    email_in_schema = "email" in by_name and bool(email)
    if email_in_schema:
        token = tokens.generate_email_verify_token(user)
        try:
            user.send_template_email("email_verify", context=dict(token=token))
        except Exception as exc:
            user.report_incident(
                f"verify-email send failed during register: {exc}",
                "register:email_send_failed", level=4)
    user.report_incident(
        f"{user.username} registered via {identity_field}",
        "register:success")

    # ---- Auth handoff ------------------------------------------------------
    require_verified = settings.get("REQUIRE_VERIFIED_EMAIL", False, kind="bool")
    if email_in_schema and require_verified:
        return JsonResponse(dict(
            status=True,
            requires_verification=True,
            message="Account created. Please check your email to verify your account before logging in."
        ))
    return jwt_login(request, user, source="password", is_new_user=True)


def get_mfa_methods(user):
    """Return list of enabled MFA methods for a user, or empty if MFA not required."""
    if not user.requires_mfa:
        return []
    methods = []
    from mojo.apps.account.models.totp import UserTOTP
    if UserTOTP.objects.filter(user=user, is_enabled=True).exists():
        methods.append("totp")
    if user.phone_number and user.is_phone_verified:
        methods.append("sms")
    from mojo.apps.account.models.pkey import Passkey
    if Passkey.objects.filter(user=user, is_enabled=True).exists():
        methods.append("passkey")
    return methods


def mfa_required_response(user, methods):
    """Return an MFA challenge response instead of a full JWT."""
    from mojo.apps.account.services import mfa as mfa_service
    token = mfa_service.create_mfa_token(user, methods)
    return JsonResponse({
        "status": True,
        "data": {
            "mfa_required": True,
            "mfa_token": token,
            "mfa_methods": methods,
            "expires_in": settings.get("MFA_TOKEN_TTL", 300, kind="int"),
        },
    })


def _check_verification_gate(user, source=None):
    """
    Raises PermissionDeniedException with error='email_not_verified' or
    'phone_not_verified' if the relevant verification setting is enabled
    and the user's channel is not yet verified.

    Only enforced when the login source matches the verification channel:
    - REQUIRE_VERIFIED_EMAIL blocks email-based login only
    - REQUIRE_VERIFIED_PHONE blocks phone-based login only
    Username login is never blocked by verification gates.

    Settings are read at call time (not cached at import time) so that
    Django's override_settings works correctly in tests.
    """
    require_verified_email = settings.get("REQUIRE_VERIFIED_EMAIL", False)
    require_verified_phone = settings.get("REQUIRE_VERIFIED_PHONE", False)
    is_verified = user.is_email_verified or user.is_phone_verified
    if is_verified:
        return
    if require_verified_email and source == "email" and not user.is_email_verified:
        raise merrors.PermissionDeniedException(
            "email_not_verified", 403, 403
        )
    if require_verified_phone and source == "phone_number" and not user.is_phone_verified:
        raise merrors.PermissionDeniedException(
            "phone_not_verified", 403, 403
        )


# jwt_login sources that skip post-credential geofencing (DM-043): authed
# re-issues of an existing session, not logins — a user in a blocked geo must
# still be able to revoke their own sessions / confirm an email change. Every
# OTHER source (including future ones) is geofenced by default (fail-closed).
GEOFENCE_EXEMPT_JWT_SOURCES = ("sessions_revoke", "email_change")


def jwt_login(request, user, legacy=False, source=None, extra=None, is_new_user=False):
    """Issue an access+refresh JWT pair for the given user.

    Args:
        source: auth-flow identifier passed through to USER_LOGIN_HANDLER and
            recorded on the login event. Examples: "password", "magic", "oauth",
            "email_verify", "invite", "password_reset", "totp_mfa", "passkey",
            "sessions_revoke", "handoff", "sms", "sms_mfa", "totp", "totp_recovery",
            "email_change". Callers requiring REQUIRE_VERIFIED_EMAIL/_PHONE
            enforcement against a *lookup* channel (e.g. password login) must
            call _check_verification_gate themselves before invoking this.
        is_new_user: True only for the first login of a freshly-created account.

    Post-credential geofence (DM-043): the check runs FIRST — before
    last_login / UserLoginEvent / USER_LOGIN_HANDLER — so a blocked login
    records no success side effects. Evaluated with the verified `user`, so
    `bypass_geofence` holders pass and block evidence carries the user.
    """
    if source not in GEOFENCE_EXEMPT_JWT_SOURCES:
        from mojo.apps.account.services.geofence import enforcement
        blocked = enforcement.enforce(request, scope="auth", user=user)
        if blocked is not None:
            return blocked
    user.last_login = dates.utcnow()
    user.track()
    # Record login event with geo data — must not break login on failure
    try:
        from mojo.apps.account.models.login_event import UserLoginEvent
        UserLoginEvent.track(request, user, device=request.device, source=source)
    except Exception:
        from mojo.helpers import logit
        logit.exception("Failed to record login event")
    # auth_time stamps WHEN the user genuinely authenticated (this login). It is
    # carried forward unchanged across silent refreshes (see on_refresh_token) and
    # read by the step-up freshness gate (services.fresh_auth). Stamped always —
    # enforcement is gated separately by FRESH_AUTH_WINDOW.
    keys = dict(uid=user.id, ip=request.ip, auth_time=int(time.time()))
    if request.device:
        keys['device'] = request.device.id
    access_token_expiry = settings.get("JWT_TOKEN_EXPIRY", 21600, kind="int")
    refresh_token_expiry = settings.get("JWT_REFRESH_TOKEN_EXPIRY", 604800, kind="int")
    if user.org:
        access_token_expiry = user.org.metadata.get("access_token_expiry", access_token_expiry)
        refresh_token_expiry = user.org.metadata.get("refresh_token_expiry", refresh_token_expiry)
    if legacy:
        keys.update(dict(user_id=user.id, device_id=request.DATA.get(["device_id", "deviceID"], request.device.id)))
    token_package = JWToken(
        user.get_auth_key(),
        access_token_expiry=access_token_expiry,
        refresh_token_expiry=refresh_token_expiry).create(**keys)
    token_package['user'] = user.to_dict("basic")
    # track webapp origin for multi-tenant URL resolution
    webapp_url = request.DATA.get("webapp_base_url") or request.META.get("HTTP_ORIGIN")
    if webapp_url:
        if not user.get_protected_metadata("orig_webapp_url"):
            user.set_protected_metadata("orig_webapp_url", webapp_url)
        else:
            user.set_protected_metadata("last_webapp_url", webapp_url)
    # USER_LOGIN_HANDLER — wrapped internally; runtime errors never block login
    account_extensions.fire_user_login(
        user=user, request=request, source=source, is_new_user=is_new_user)
    if legacy:
        return {
            "status": True,
            "data": {
                "access": token_package.access_token,
                "refresh": token_package.refresh_token,
                "id": user.id
            }
        }
    response_data = dict(token_package)
    if extra:
        response_data.update(extra)
    return JsonResponse(dict(status=True, data=response_data))


@md.POST("auth/forgot")
@md.strict_rate_limit("auth_forgot", ip_limit=5, ip_window=300)
@md.public_endpoint()
@md.requires_geofence(scope="auth")
def on_user_forgot(request):
    """
    Start a password-reset flow. Accepts an identifier via either the
    `email` or `phone` body field (`username` is also accepted and routed
    by shape — `@` => email, else phone).

    Method routing:
      method=code  + channel=sms (or user has no email) → SMS the 6-digit code
      method=code  (default)                            → email the 6-digit code
      method=link / email                               → email a reset link
                                                          (link mode is email-only)
    """
    from mojo.apps import phonehub

    user = User.lookup_from_request(request, phone_as_username=True)
    method = (request.DATA.get("method") or "code").lower().strip()
    channel = (request.DATA.get("channel") or "").lower().strip()

    if user is None:
        User.class_report_incident(
            f"reset password with details {request.DATA.username} - {request.DATA.email} - {request.DATA.phone_number}",
            event_type="reset:unknown",
            level=8,
            request=request)
    else:
        user.report_incident(f"{user.username} requested a password reset", "password_reset")
        # Auto-dispatch SMS when the user has no email on file. Operators
        # can also force SMS via channel=sms when both contact channels exist.
        wants_sms = (
            method == "code"
            and (channel == "sms" or (not user.email and bool(user.phone_number)))
        )
        if wants_sms:
            # Always perform the DB writes regardless of phone presence so the
            # response timing for "user has phone" vs "user has no phone" is
            # dominated by the same set_secret + save work — closes a
            # latency-based attribute-enumeration side channel.
            #
            # Residual gap: phonehub.send_sms is a network call only made when
            # the user actually has a phone. A determined attacker measuring
            # response latency could still distinguish has-phone from
            # no-phone in the tail. Move the SMS dispatch onto the jobs
            # channel to fully close this; tracked separately.
            code = crypto.random_string(6, True, False, False)
            user.set_secret("password_reset_code", code)
            user.set_secret("password_reset_code_ts", int(dates.utcnow().timestamp()))
            user.save()
            if user.phone_number:
                try:
                    phonehub.send_sms(
                        user.phone_number,
                        f"Your password reset code is: {code}")
                except Exception as exc:
                    user.report_incident(
                        f"SMS reset code send failed: {exc}",
                        "password_reset:sms_send_failed", level=4)
            else:
                user.report_incident(
                    f"{user.username} requested SMS reset but has no phone on file",
                    "password_reset:no_phone", level=4)
        elif method == "code":
            code = crypto.random_string(6, True, False, False)
            user.set_secret("password_reset_code", code)
            user.set_secret("password_reset_code_ts", int(dates.utcnow().timestamp()))
            user.save()
            user.send_template_email("password_reset_code", dict(code=code))
        elif method in ("link", "email"):
            token = tokens.generate_password_reset_token(user)
            token_url = build_token_url("password_reset", token, request=request, user=user, group=getattr(request, "group", None))
            token_url = maybe_shorten_url(token_url, source="password_reset", user=user, expire_hours=1)
            user.send_template_email("password_reset_link", dict(token=token, token_url=token_url))
        else:
            raise merrors.ValueException("Invalid method")
    return JsonResponse(dict(status=True, message="If the account is in our system a reset code was sent."))


@md.POST("auth/password/reset/code")
@md.strict_rate_limit("password_reset_code", ip_limit=5, ip_window=300)
@md.public_endpoint()
@md.requires_geofence(scope="auth", after_auth=True)
@md.requires_params("code", "new_password")
def on_user_password_reset_code(request):
    code = request.DATA.get("code")
    new_password = request.DATA.get("new_password")
    user = User.lookup_from_request(request, phone_as_username=True)
    if user is None:
        User.class_report_incident(
            f"invalid reset password code with details {request.DATA.username} - {request.DATA.email} - {request.DATA.phone_number}",
            event_type="reset:unknown",
            level=8,
            request=request)
        raise merrors.ValueException("Invalid code")

    sec_code = user.get_secret("password_reset_code")
    code_ts = int(user.get_secret("password_reset_code_ts") or 0)
    now_ts = int(dates.utcnow().timestamp())
    if len(code or "") != 6 or code != (sec_code or ""):
        user.report_incident(f"{user.username} invalid password reset code", "password_reset")
        raise merrors.ValueException("Invalid code")
    if now_ts - code_ts > settings.get("PASSWORD_RESET_CODE_TTL", 600, kind="int"):
        user.report_incident(f"{user.username} expired password reset code", "password_reset")
        raise merrors.ValueException("Expired code")
    user.check_password_strength(new_password)
    user.set_password(new_password)
    user.set_secret("password_reset_code", None)
    user.set_secret("password_reset_code_ts", None)
    user.save()
    return jwt_login(request, user, source="password_reset")


@md.POST("auth/password/reset/token")
@md.custom_security("requires valid token")
@md.requires_geofence(scope="auth", after_auth=True)
@md.requires_params("token", "new_password")
def on_user_password_reset_token(request):
    token = request.DATA.get("token")
    new_password = request.DATA.get("new_password")
    if token.startswith("iv:"):
        user = tokens.verify_invite_token(token)
        user.is_email_verified = True
    elif token.startswith("pr:"):
        user = tokens.verify_password_reset_token(token)
        # If the user has never logged in, this token was consumed via an invite link —
        # the fact they received and clicked it proves email ownership.
        if user.last_login is None:
            user.is_email_verified = True
    else:
        raise merrors.ValueException("Invalid token kind")
    user.check_password_strength(new_password)
    user.set_password(new_password)
    user.save()
    return jwt_login(request, user, source="password_reset")


@md.POST("auth/magic/send")
@md.strict_rate_limit("magic_login_send", ip_limit=5, ip_window=300)
@md.public_endpoint()
@md.requires_geofence(scope="auth")
def on_magic_login_send(request):
    """Send a magic login link via email (default) or SMS (method=sms)."""
    # UX-only per-group method gate (no-op without a resolving group_uuid).
    auth_config.assert_login_method(
        "magic", auth_config.resolve_group_from_request(request))

    channel = request.DATA.get("method", "email")
    if channel not in ("email", "sms"):
        channel = "email"

    user = User.lookup_from_request(request, phone_as_username=True)

    if user is None:
        User.class_report_incident(
            f"magic login attempt with unknown identifier {request.DATA.username} - {request.DATA.email} - {request.DATA.phone_number}",
            event_type="magic:unknown",
            level=8,
            request=request)
    else:
        user.report_incident(f"{user.username} requested a magic login link via {channel}", "magic_login")
        magic_token = tokens.generate_magic_login_token(user, channel=channel)
        group = getattr(request, "group", None)
        token_url = build_token_url("magic_login", magic_token, request=request, user=user, group=group)
        if channel == "sms":
            from mojo.apps import phonehub
            if user.phone_number:
                login_url = maybe_shorten_url(token_url, source="magic_login_sms", user=user, expire_hours=1)
                phonehub.send_sms(user.phone_number, f"Your login link: {login_url}")
        else:
            token_url = maybe_shorten_url(token_url, source="magic_login", user=user, expire_hours=1)
            user.send_template_email("magic_login_link", dict(token=magic_token, token_url=token_url))
    return JsonResponse(dict(status=True, message="If account is in our system a login link was sent."))


@md.POST("auth/magic/login")
@md.strict_rate_limit("magic_login", ip_limit=10, ip_window=300)
@md.custom_security("requires valid magic login token")
@md.requires_geofence(scope="auth", after_auth=True)
@md.requires_params("token")
def on_magic_login_complete(request):
    """Exchange a magic login token for a JWT — logs the user in."""
    token = request.DATA.get("token")
    user, channel = tokens.verify_magic_login_token(token)
    if channel == "sms" and not user.is_phone_verified:
        user.is_phone_verified = True
        user.save(update_fields=["is_phone_verified", "modified"])
    elif channel == "email" and not user.is_email_verified:
        user.is_email_verified = True
        user.save(update_fields=["is_email_verified", "modified"])
    return jwt_login(request, user, source="magic")


# -----------------------------------------------------------------
# Email verification
# -----------------------------------------------------------------

@md.POST("auth/email/verify/send")
@md.strict_rate_limit("email_verify_send", ip_limit=5, ip_window=300)
@md.public_endpoint()
@md.requires_geofence(scope="auth")
def on_email_verify_send(request):
    """Send an email verification link. Accepts username or email."""
    from mojo.apps.account.utils import tokens as tok_utils
    user = User.lookup_from_request(request)
    if user is None or not user.is_active:
        # No enumeration — always return success regardless of existence or active state
        return JsonResponse({"status": True, "message": "If the account exists, a verification email was sent."})
    if user.is_email_verified:
        return JsonResponse({"status": True, "message": "Email is already verified."})
    token = tok_utils.generate_email_verify_token(user)
    token_url = build_token_url("email_verify", token, request=request, user=user, group=getattr(request, "group", None))
    token_url = maybe_shorten_url(token_url, source="email_verify", user=user, expire_hours=24)
    user.send_template_email("email_verify_link", dict(token=token, token_url=token_url))
    return JsonResponse({"status": True, "message": "If the account exists, a verification email was sent."})


@md.POST("auth/email/verify")
@md.strict_rate_limit("email_verify", ip_limit=10, ip_window=300)
@md.custom_security("requires valid email verify token")
@md.requires_geofence(scope="auth", after_auth=True)
@md.requires_params("token")
def on_email_verify(request):
    """Exchange an email verify token — marks email verified and logs the user in."""
    from mojo.apps.account.utils import tokens as tok_utils
    token = request.DATA.get("token")
    user = tok_utils.verify_email_verify_token(token)
    if not user.is_active:
        raise merrors.PermissionDeniedException("Account is disabled", 403, 403)
    user.is_email_verified = True
    user.save(update_fields=["is_email_verified", "modified"])
    _send_account_realtime_event(user, "account:email:verified", {"email": user.email})
    return jwt_login(request, user, source="email_verify")


@md.POST("auth/invite/accept")
@md.strict_rate_limit("invite_accept", ip_limit=10, ip_window=300)
@md.custom_security("requires valid invite token")
@md.requires_geofence(scope="auth", after_auth=True)
@md.requires_params("token")
def on_invite_accept(request):
    """
    Accept an invite token.
    Marks email as verified and issues a JWT.
    If the user has no password yet, the client should prompt them to set one
    via POST /api/auth/password/reset/token (the invite token is the same shape).
    """
    from mojo.apps.account.utils import tokens as tok_utils
    token = request.DATA.get("token")
    user = tok_utils.verify_invite_token(token)
    if not user.is_active:
        raise merrors.PermissionDeniedException("Account is disabled", 403, 403)
    user.is_email_verified = True
    user.save(update_fields=["is_email_verified", "modified"])
    return jwt_login(request, user, source="invite")


# -----------------------------------------------------------------
# Email change (self-service, verify-then-commit)
# -----------------------------------------------------------------

@md.POST("auth/email/change/request")
@md.requires_auth()
@md.requires_fresh_auth()
@md.strict_rate_limit("email_change_request", ip_limit=5, ip_window=3600)
@md.requires_params("email")
def on_email_change_request(request):
    """
    Begin a self-service email change. current_password is optional.
    If provided and non-empty, it is validated; otherwise the password check
    is skipped (supports OAuth/passkey-only users).

    Optional body param:
      method: "link" (default) — send a confirmation link (ec: token) to the new address
      method: "code"           — send a 6-digit OTP to the new address instead

    In both cases a notification is sent to the OLD address alerting them of
    the change request. The current email is NOT changed until the confirm
    step is completed.
    """
    if not settings.get("ALLOW_EMAIL_CHANGE", True):
        raise merrors.PermissionDeniedException("Email change is not allowed")

    import re
    from mojo.apps.account.utils import tokens as tok_utils

    user = request.user
    new_email = request.DATA.get("email", "").lower().strip()
    current_password = request.DATA.get("current_password", "")

    if current_password:
        if not user.check_password(current_password):
            user.report_incident("Invalid password on email change request", "email_change:bad_password")
            raise merrors.PermissionDeniedException("Incorrect password", 401, 401)
    if not new_email or not re.match(r"[^@]+@[^@]+\.[^@]+", new_email):
        raise merrors.ValueException("Invalid email address")
    if new_email == str(user.email).lower():
        raise merrors.ValueException("New email must be different from current email")
    if User.objects.filter(email=new_email).exclude(pk=user.pk).exists():
        raise merrors.ValueException("Email already in use")

    method = request.DATA.get("method", "link")

    if method == "code":
        otp = tok_utils.generate_email_change_otp(user, new_email)
        _send_email_change_code(user, new_email, otp)
        user.send_template_email("email_change_notify", dict(new_email=new_email))
        user.report_incident(f"{user.username} requested email change to {new_email} (code)", "email_change:requested_code")
        return JsonResponse({"status": True, "message": "A verification code has been sent to your new email address."})

    token = tok_utils.generate_email_change_token(user, new_email)

    # Confirmation link sent to the NEW address — resolve the mailbox the same way
    # send_template_email does internally, since that method always sends to self.email.
    _send_email_change_confirm(user, new_email, token)

    # Notification to the OLD address — no cancel token (single-JTI design means issuing
    # a second ec: token would immediately invalidate the first). The user can cancel via
    # POST /api/auth/email/change/cancel while authenticated, or simply let the 1h link expire.
    user.send_template_email("email_change_notify", dict(new_email=new_email))

    user.report_incident(f"{user.username} requested email change to {new_email}", "email_change:requested")
    return JsonResponse({"status": True, "message": "A confirmation link has been sent to your new email address."})


def _send_email_change_confirm(user, new_email, token):
    """
    Send the email-change confirmation link to the NEW address.
    Uses the same mailbox-resolution logic as user.send_template_email but
    overrides the recipient so the message goes to new_email, not user.email.
    """
    from mojo.apps.aws.models import Mailbox

    mailbox = None
    if user.org and hasattr(user.org, "metadata"):
        domain = user.org.metadata.get("domain")
        if domain:
            mailbox = Mailbox.get_domain_default(domain)
            if not mailbox:
                mailbox = Mailbox.objects.filter(
                    domain__name__iexact=domain,
                    allow_outbound=True,
                ).first()
    if not mailbox:
        mailbox = Mailbox.get_system_default()

    if not mailbox:
        user.report_incident(
            "No mailbox available to send email change confirmation",
            "email:no_mailbox",
            level=6,
        )
        return

    context = {
        "user": user.to_dict("basic"),
        "token": token,
        "new_email": new_email,
    }
    try:
        mailbox.send_template_email(
            to=new_email,
            template_name="email_change_confirm",
            context=context,
            allow_unverified=True,
        )
    except Exception as e:
        user.report_incident(
            f"email change confirm send failed: {e}",
            "email:send_failed",
            level=6,
        )


def _send_email_change_code(user, new_email, otp):
    """
    Send the email-change OTP code to the NEW address.
    Uses identical mailbox resolution to _send_email_change_confirm.
    """
    from mojo.apps.aws.models import Mailbox

    mailbox = None
    if user.org and hasattr(user.org, "metadata"):
        domain = user.org.metadata.get("domain")
        if domain:
            mailbox = Mailbox.get_domain_default(domain)
            if not mailbox:
                mailbox = Mailbox.objects.filter(
                    domain__name__iexact=domain,
                    allow_outbound=True,
                ).first()
    if not mailbox:
        mailbox = Mailbox.get_system_default()

    if not mailbox:
        user.report_incident(
            "No mailbox available to send email change code",
            "email:no_mailbox",
            level=6,
        )
        return

    context = {
        "user": user.to_dict("basic"),
        "code": otp,
        "new_email": new_email,
    }
    try:
        mailbox.send_template_email(
            to=new_email,
            template_name="email_change_code",
            context=context,
            allow_unverified=True,
        )
    except Exception as e:
        user.report_incident(
            f"email change code send failed: {e}",
            "email:send_failed",
            level=6,
        )


def _send_account_realtime_event(user, event, data):
    """
    Fire-and-forget realtime event to all of a user's active WebSocket connections.
    Silently swallows errors — realtime delivery is best-effort, never blocking.
    """
    try:
        from mojo.apps import realtime
        realtime.send_to_user("user", user.pk, {"event": event, "data": data})
    except Exception:
        pass


def _render_confirm(request, template, ctx):
    """
    Render an account confirmation template.
    If ?redirect=<url> is present and success=True, honour it:
      - immediately if redirect_delay is falsy / zero
      - after a short delay via <meta http-equiv=refresh> otherwise
    Downstream projects can override the templates by placing their own versions
    under templates/account/<name>.html with higher priority in TEMPLATES.DIRS.
    """
    redirect_url = request.DATA.get("redirect") or request.GET.get("redirect", "")
    redirect_delay = 3 if ctx.get("success") else 0

    if redirect_url and ctx.get("success") and not redirect_delay:
        return HttpResponseRedirect(redirect_url)

    ctx["redirect_url"] = redirect_url
    ctx["redirect_delay"] = redirect_delay
    return render(request, f"account/{template}", ctx)


@md.POST("auth/email/change/confirm")
@md.strict_rate_limit("email_change_confirm", ip_limit=10, ip_window=3600)
@md.custom_security("requires valid email change token, or authenticated session with valid OTP code")
def on_email_change_confirm(request):
    """
    Complete an email change.

    Accepts either:
      { "token": "ec:..." }   — existing link flow; no auth required (token is the credential)
      { "code": "123456" }    — code flow; requires authentication (Bearer token)

    In both cases: commits the new email, marks it verified, rotates auth_key
    (invalidates all other sessions), and issues a fresh JWT.
    """
    import uuid
    from mojo.apps.account.utils import tokens as tok_utils

    token = request.DATA.get("token")
    code = request.DATA.get("code")

    if not token and not code:
        raise merrors.ValueException("token or code is required")

    if code:
        # Code path — user must be authenticated; identity comes from the JWT
        if not request.user or not request.user.is_authenticated:
            raise merrors.PermissionDeniedException("Authentication required", 401, 401)
        user = request.user
        new_email = tok_utils.verify_email_change_otp(user, code)
    else:
        # Link/token path — token is the credential; no active session required
        user, new_email = tok_utils.verify_email_change_token(token)

    if not user.is_active:
        raise merrors.PermissionDeniedException("Account is disabled", 403, 403)

    # Confirm new email is still available (another account may have claimed it in the interim)
    if User.objects.filter(email=new_email).exclude(pk=user.pk).exists():
        raise merrors.ValueException("Email address is no longer available")

    old_email = str(user.email)

    # Commit the change — bypass the REST guard by updating directly
    User.objects.filter(pk=user.pk).update(
        email=new_email,
        is_email_verified=True,
        auth_key=uuid.uuid4().hex,  # invalidate all other active sessions
    )
    # Update username too if it mirrored the old email
    if str(user.username).lower() == old_email.lower():
        User.objects.filter(pk=user.pk).update(username=new_email)

    user.refresh_from_db()
    user.log(kind="email:changed", log=f"{old_email} to {new_email}")

    # Notify any other open sessions — they should refresh their profile
    # (auth_key was just rotated so their JWTs are already invalid, but the
    # event gives them a clean signal to re-prompt login rather than silently failing)
    _send_account_realtime_event(user, "account:email:changed", {"email": new_email})

    return jwt_login(request, user, source="email_change")


@md.GET("auth/email/change/confirm")
@md.strict_rate_limit("email_change_confirm", ip_limit=10, ip_window=3600)
@md.public_endpoint()
def on_email_change_confirm_get(request):
    """
    GET handler for the email-change confirmation link clicked from the user's inbox.

    Validates the ec: token, commits the change, then renders a Django template
    page (account/email_change_confirm.html).  If a ?redirect=<url> param is
    present it will be used as a "Continue" button target and as an automatic
    redirect (after a brief delay) on success.

    Downstream projects can override the template by placing their own version at
    templates/account/email_change_confirm.html with higher priority in TEMPLATES.DIRS.
    """
    import uuid
    from mojo.apps.account.utils import tokens as tok_utils

    token = request.DATA.get("token") or request.GET.get("token", "")
    if not token:
        return _render_confirm(request, "email_change_confirm.html", {
            "success": False,
            "error_title": "Link invalid",
            "error_message": "No token was provided. Please use the link from your confirmation email.",
        })

    try:
        user, new_email = tok_utils.verify_email_change_token(token)
    except Exception:
        return _render_confirm(request, "email_change_confirm.html", {
            "success": False,
            "error_title": "Link invalid or expired",
            "error_message": "This email change link is invalid or has already been used. Links expire after 1 hour and can only be used once.",
        })

    if not user.is_active:
        return _render_confirm(request, "email_change_confirm.html", {
            "success": False,
            "error_title": "Account disabled",
            "error_message": "This account has been disabled. Please contact support.",
        })

    if User.objects.filter(email=new_email).exclude(pk=user.pk).exists():
        return _render_confirm(request, "email_change_confirm.html", {
            "success": False,
            "error_title": "Address no longer available",
            "error_message": f"The address {new_email} has been registered by another account since this link was issued. Please request a new email change.",
        })

    old_email = str(user.email)

    User.objects.filter(pk=user.pk).update(
        email=new_email,
        is_email_verified=True,
        auth_key=uuid.uuid4().hex,
    )
    if str(user.username).lower() == old_email.lower():
        User.objects.filter(pk=user.pk).update(username=new_email)

    user.refresh_from_db()
    user.log(kind="email:changed", log=f"{old_email} to {new_email}")
    _send_account_realtime_event(user, "account:email:changed", {"email": new_email})

    return _render_confirm(request, "email_change_confirm.html", {
        "success": True,
        "new_email": new_email,
    })


@md.POST("auth/email/change/cancel")
@md.requires_auth()
@md.strict_rate_limit("email_change_cancel", ip_limit=10, ip_window=3600)
def on_email_change_cancel(request):
    """
    Cancel a pending email change. Clears the stored pending_email so the
    outstanding confirmation link becomes useless even before it expires.
    Requires authentication (the real owner cancels via their active session).
    """
    import mojo.apps.account.utils.tokens as tok_module

    user = request.user
    pending = user.get_secret("pending_email")
    if not pending:
        return JsonResponse({"status": True, "message": "No pending email change to cancel."})
    user.set_secret("pending_email", None)
    # Clear the link-flow JTI so any outstanding ec: token is immediately dead
    user.set_secret(tok_module._JTI_KEYS[tok_module.KIND_EMAIL_CHANGE], None)
    # Clear the code-flow OTP so any outstanding code is immediately dead
    user.set_secret("email_change_otp", None)
    user.set_secret("email_change_otp_ts", None)
    user.save(update_fields=["mojo_secrets", "modified"])
    user.report_incident(f"{user.username} cancelled pending email change to {pending}", "email_change:cancelled")
    return JsonResponse({"status": True, "message": "Pending email change has been cancelled."})


# -----------------------------------------------------------------
# Phone number change (self-service, OTP-verify-then-commit)
# -----------------------------------------------------------------

@md.POST("auth/phone/change/request")
@md.requires_auth()
@md.requires_fresh_auth()
@md.strict_rate_limit("phone_change_request", ip_limit=5, ip_window=3600)
@md.requires_params("phone_number")
def on_phone_change_request(request):
    """
    Begin a self-service phone number change. current_password is optional.
    If provided and non-empty, it is validated; otherwise the password check is skipped.
    Sends a 6-digit OTP to the NEW number via SMS.
    The phone number is NOT changed until the user submits the correct OTP in /confirm.
    """
    from mojo.apps.account.utils import tokens as tok_utils
    from mojo.apps import phonehub

    if not settings.get("ALLOW_PHONE_CHANGE", True):
        raise merrors.PermissionDeniedException("Phone number change is not allowed")

    user = request.user
    current_password = request.DATA.get("current_password")
    if current_password:
        if not user.check_password(current_password):
            user.report_incident("Invalid password on phone change request", "phone_change:bad_password")
            raise merrors.PermissionDeniedException("Incorrect password", 401, 401)

    new_phone_raw = request.DATA.get("phone_number", "").strip()
    normalized = user.normalize_phone(new_phone_raw)
    if not normalized:
        raise merrors.ValueException("Invalid phone number format")

    if normalized == user.phone_number:
        raise merrors.ValueException("New phone number must be different from current phone number")

    if User.objects.filter(phone_number=normalized).exclude(pk=user.pk).exists():
        raise merrors.ValueException("Phone number already in use")

    session_token, otp = tok_utils.generate_phone_change_token(user, normalized)

    sms = phonehub.send_sms(normalized, f"Your phone change verification code is: {otp}")
    if sms and sms.status == "failed":
        # Clear the pending state so the user can retry cleanly
        user.set_secret("pending_phone", None)
        user.set_secret("phone_change_otp", None)
        user.set_secret("phone_change_otp_ts", None)
        user.save(update_fields=["mojo_secrets", "modified"])
        raise merrors.ValueException("Failed to send SMS to the new number — check the number and try again")

    # Notify the OLD phone number so the real owner knows a change was requested
    if user.phone_number:
        try:
            phonehub.send_sms(user.phone_number, "A request was made to change your phone number. If this wasn't you, secure your account immediately.")
        except Exception:
            pass

    user.report_incident(
        f"{user.username} requested phone number change to {normalized}",
        "phone_change:requested")
    return JsonResponse({
        "status": True,
        "session_token": session_token,
        "message": "A verification code has been sent to your new phone number.",
    })


@md.POST("auth/phone/change/confirm")
@md.strict_rate_limit("phone_change_confirm", ip_limit=10, ip_window=3600)
@md.requires_auth()
@md.requires_params("session_token", "code")
def on_phone_change_confirm(request):
    """
    Complete a phone number change by submitting the session token and OTP.
    Commits the new phone number and resets is_phone_verified to True.
    The user stays logged in — no session rotation needed (phone is not used
    for auth_key signing the way email is).
    """
    from mojo.apps.account.utils import tokens as tok_utils

    session_token = request.DATA.get("session_token")
    code = request.DATA.get("code")

    # Must be authenticated AND the token must belong to the same user —
    # verify_phone_change_token checks JTI / TTL / OTP; we additionally
    # confirm the resolved user matches the session to prevent token-swap attacks.
    token_user, new_phone = tok_utils.verify_phone_change_token(session_token, code)
    if token_user.pk != request.user.pk:
        raise merrors.PermissionDeniedException("Session mismatch")

    user = request.user

    if not user.is_active:
        raise merrors.PermissionDeniedException("Account is disabled", 403, 403)

    # Re-check availability — another account may have claimed the number in the window
    if User.objects.filter(phone_number=new_phone).exclude(pk=user.pk).exists():
        raise merrors.ValueException("Phone number is no longer available")

    old_phone = str(user.phone_number or "")

    # Commit directly — bypass on_rest_pre_save guard (which blocks direct phone changes)
    User.objects.filter(pk=user.pk).update(
        phone_number=new_phone,
        is_phone_verified=True,
    )

    user.refresh_from_db()
    user.log(kind="phone:changed", log=f"{old_phone} to {new_phone}")
    user.report_incident(
        f"{user.username} phone number changed to {new_phone}",
        "phone_change:confirmed")

    return JsonResponse({"status": True, "message": "Phone number updated successfully."})


@md.POST("auth/phone/change/cancel")
@md.requires_auth()
@md.strict_rate_limit("phone_change_cancel", ip_limit=10, ip_window=3600)
def on_phone_change_cancel(request):
    """
    Cancel a pending phone number change. Clears the stored pending_phone and OTP
    so the outstanding session token is immediately dead even before the TTL expires.
    Idempotent — returns 200 if there is no pending change.
    """
    import mojo.apps.account.utils.tokens as tok_module

    user = request.user
    pending = user.get_secret("pending_phone")
    if not pending:
        return JsonResponse({"status": True, "message": "No pending phone change to cancel."})

    user.set_secret("pending_phone", None)
    user.set_secret("phone_change_otp", None)
    user.set_secret("phone_change_otp_ts", None)
    # Kill the session token JTI so any outstanding pc: token is immediately invalid
    user.set_secret(tok_module._JTI_KEYS[tok_module.KIND_PHONE_CHANGE], None)
    user.save(update_fields=["mojo_secrets", "modified"])
    user.report_incident(
        f"{user.username} cancelled pending phone change to {pending}",
        "phone_change:cancelled")
    return JsonResponse({"status": True, "message": "Pending phone number change has been cancelled."})


# -----------------------------------------------------------------
# Username change
# -----------------------------------------------------------------

@md.POST("auth/username/change")
@md.requires_auth()
@md.requires_fresh_auth()
@md.requires_params("username")
def on_username_change(request):
    """Self-service username change.

    Ownership is proven by the authenticated session; freshness is enforced by
    the step-up gate (no current_password — passwordless passkey/SMS accounts
    must be able to change their username too).
    """
    if not settings.get("ALLOW_USERNAME_CHANGE", True):
        raise merrors.PermissionDeniedException("Username change is not allowed")

    user = request.user

    new_username = request.DATA.get("username", "").lower().strip()
    if not new_username:
        raise merrors.ValueException("Username is required")

    if new_username == user.username:
        raise merrors.ValueException("New username must be different from current username")

    # Set on the user instance so validate_username() reads self.username
    old_username = user.username
    user.username = new_username
    try:
        user.validate_username()
    except Exception:
        user.username = old_username
        raise

    # Uniqueness check (exclude self)
    if User.objects.filter(username=new_username).exclude(pk=user.pk).exists():
        user.username = old_username
        raise merrors.ValueException("Username already taken")

    user.save(update_fields=["username", "modified"])
    user.log(f"Username changed from {old_username} to {new_username}", "username:changed")

    return JsonResponse({
        "status": True,
        "data": {"username": user.username},
    })


# -----------------------------------------------------------------
# Session revoke (log out everywhere)
# -----------------------------------------------------------------

@md.POST("auth/sessions/revoke")
@md.requires_auth()
@md.requires_fresh_auth()
@md.rate_limit("sessions_revoke", ip_limit=5, ip_window=300)
def on_sessions_revoke(request):
    """
    Rotate auth_key to invalidate all active sessions. Returns a fresh JWT
    for the calling session so the user stays logged in.

    Ownership is proven by the authenticated session; freshness is enforced by
    the step-up gate (no current_password — passwordless accounts must work too).
    """
    import uuid

    user = request.user

    # Rotate auth_key — immediately invalidates every other JWT
    user.auth_key = uuid.uuid4().hex
    user.save(update_fields=["auth_key", "modified"])

    user.report_incident(f"{user.username} revoked all sessions", "sessions:revoked")

    # Issue fresh JWT signed with the new key
    return jwt_login(request, user, source="sessions_revoke")


# -----------------------------------------------------------------
# Account deactivation
# -----------------------------------------------------------------

@md.POST("account/deactivate")
@md.requires_auth()
@md.requires_fresh_auth()
@md.rate_limit("account_deactivate", ip_limit=5, ip_window=300)
def on_account_deactivate(request):
    """
    Step 1: Send a confirmation email with a short-lived dv: token.
    The account is NOT deactivated until the token is confirmed.
    """
    if not settings.get("ALLOW_SELF_DEACTIVATION", True):
        raise merrors.PermissionDeniedException("Account deactivation is not allowed")

    user = request.user
    token = tokens.generate_deactivate_token(user)

    try:
        user.send_template_email("account_deactivate_confirm", {"token": token})
    except Exception:
        pass

    user.report_incident(f"{user.username} requested account deactivation", "account:deactivate_requested")

    return JsonResponse({
        "status": True,
        "message": "A confirmation email has been sent. Follow the link to complete deactivation.",
    })


@md.POST("account/deactivate/confirm")
@md.requires_params("token")
@md.public_endpoint()
def on_account_deactivate_confirm(request):
    """
    Step 2: Validate the dv: token and call pii_anonymize().
    Public endpoint — the token is the credential.
    """
    raw_token = request.DATA.get("token", "")
    user = tokens.verify_deactivate_token(raw_token)

    if not user.is_active:
        return JsonResponse({"status": True, "message": "Your account has been deactivated."})

    # Log BEFORE anonymisation so username is still readable
    user.report_incident(f"{user.username} account deactivated", "account:deactivated", uid=user.pk)

    user.pii_anonymize()

    return JsonResponse({"status": True, "message": "Your account has been deactivated."})


# -----------------------------------------------------------------
# Security events log
# -----------------------------------------------------------------

_SECURITY_CATEGORY_PREFIXES = [
    "login",
    "invalid_password",
    "password_reset",
    "totp:",
    "email_change:",
    "email_verify:",
    "phone_change:",
    "phone_verify:",
    "username:",
    "oauth",
    "passkey:",
    "account:deactivat",
    "sessions:",
    "api_key:",
    "magic_login",
]


@md.GET("account/security-events")
@md.requires_auth()
def on_account_security_events(request):
    """
    Return auth-relevant audit events for the authenticated user.
    Scoped unconditionally to request.user — no cross-user access.
    Uses the Event model's 'security' graph for serialization and the
    framework's built-in date-range filtering, sorting, and pagination.
    """
    from django.db.models import Q
    from mojo.apps.incident.models.event import Event

    # Cap page size at 100
    raw_size = request.DATA.get("size", 25)
    capped = min(int(raw_size or 25), 100)
    request.DATA["size"] = capped

    # Force the restricted security graph and default sort
    request.DATA["graph"] = "security"
    if not request.DATA.get("sort"):
        request.DATA["sort"] = "-created"

    # Build category filter scoped to the authenticated user
    q = Q()
    for prefix in _SECURITY_CATEGORY_PREFIXES:
        q |= Q(category__startswith=prefix)

    qs = Event.objects.filter(q, uid=request.user.pk)

    # Delegate to framework — handles date range, sorting, pagination, serialization
    return Event.on_rest_list(request, qs)
