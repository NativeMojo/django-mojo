"""
Register-form schema resolution.

Single source of truth for which fields the bouncer-hosted register form
collects and which fields the server-side `on_register` accepts. The same
list drives both the template render and the server validator so the two
cannot drift.

Public surface:

    resolve_fields(group=None) -> list[dict]
        Canonical list of `{"name", "required", "verify"}` dicts. Pulls
        `registration.fields` from the group's resolved auth config
        (see auth_config), falls back to DEFAULT_FIELDS, filters unknown
        names. `password`, when present, is always required; a schema may
        omit it entirely for passwordless registration.

    resolve_identity_field(fields, group=None) -> str
        "email" or "phone". Honors `AUTH_REGISTER_IDENTITY_FIELD` if set,
        else auto-picks (email > phone). Never returns None — the schema
        must always have at least one of email or phone as required.

    resolve_min_age(group=None) -> int | None
        `AUTH_MIN_AGE_YEARS` setting, or None when unset.

    validate_payload(fields, payload, identity_field, min_age) -> dict
        Strict server-side validator. Returns a sanitized dict with
        normalized values (lowercased email, normalized phone, parsed
        date). Raises ValueException with a specific message on failure.

    field_rows(fields) -> list[list[dict]]
        Group adjacent first_name + last_name into a 2-column row for the
        template; everything else is a 1-element row. Lets the template
        render a single loop without per-field positional logic.
"""
import datetime
import json
import re

from mojo import errors as merrors


# Extra-field names become DOM ids (reg-extra-<name>), getElementById args, and
# register-payload / metadata keys — restrict to a simple identifier so a
# group-configured name can never carry HTML/selector-special characters.
_EXTRA_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
from mojo.helpers import test_mode as _tm


# Closed set of canonical field names. Anything outside this set is silently
# dropped from AUTH_REGISTER_FIELDS — consumer-specific data belongs in
# REGISTRATION_EXTRA_FIELDS (existing extras allowlist).
CANONICAL_FIELDS = ("first_name", "last_name", "email", "phone", "dob", "password")


# Default config preserves today's email-based form when AUTH_REGISTER_FIELDS
# is unset. Backwards compatibility is non-negotiable.
DEFAULT_FIELDS = [
    {"name": "first_name", "required": False, "verify": None},
    {"name": "last_name",  "required": False, "verify": None},
    {"name": "email",      "required": True,  "verify": "email"},
    {"name": "password",   "required": True,  "verify": None},
]


def _normalize_entry(entry):
    """Coerce one schema entry into the canonical dict form, or None to drop it."""
    if isinstance(entry, str):
        entry = {"name": entry}
    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    if name not in CANONICAL_FIELDS:
        return None
    required = bool(entry.get("required", False))
    verify = entry.get("verify") or None
    # Password, when present in the schema, is always required — there is no
    # "optional password" state. A schema may omit `password` entirely for
    # passwordless registration (see validate_fields_config). Password has no
    # verify channel.
    if name == "password":
        required = True
        verify = None
    return {"name": name, "required": required, "verify": verify}


def _read_test_header(request, header_name):
    if request is None:
        return None
    if not _tm.is_test_request(request):
        return None
    key = "HTTP_" + header_name.upper().replace("-", "_")
    return request.META.get(key)


def _normalize_field_list(raw):
    """Normalize a raw field-config list into canonical `{name, required,
    verify}` dicts. Drops unknown/duplicate entries, ensures `password` is
    present, and falls back to a copy of DEFAULT_FIELDS when `raw` is empty
    or unusable."""
    if not raw or not isinstance(raw, (list, tuple)):
        return [dict(f) for f in DEFAULT_FIELDS]
    normalized = []
    seen = set()
    for entry in raw:
        norm = _normalize_entry(entry)
        if norm is None:
            continue
        if norm["name"] in seen:
            continue
        seen.add(norm["name"])
        normalized.append(norm)
    if not normalized:
        return [dict(f) for f in DEFAULT_FIELDS]
    # `password` is NOT auto-added — a schema may legitimately omit it for
    # passwordless registration (see validate_fields_config).
    return normalized


def resolve_fields(group=None, request=None):
    """Resolve the register field schema.

    Fields come from the group's resolved auth config
    (`registration.fields`); see `mojo.apps.account.services.auth_config`.

    `request` enables the test-mode header override
    (`X-Mojo-Test-Register-Fields`, a JSON list). The override is
    only honored when the test-mode gate passes (loopback + flag + no
    proxy chain), so production traffic can't influence the schema.
    """
    raw = None
    header_value = _read_test_header(request, "X-Mojo-Test-Register-Fields")
    if header_value is not None:
        try:
            parsed = json.loads(header_value)
            if isinstance(parsed, list):
                raw = parsed
        except (json.JSONDecodeError, TypeError):
            raw = None
    if raw is None:
        from mojo.apps.account.services import auth_config
        cfg = auth_config.resolve_auth_config(group=group, request=request)
        raw = cfg.registration.fields
    return _normalize_field_list(raw)


def validate_fields_config(raw):
    """Validate an auth-config `registration.fields` list. Raises ValueException on
    bad config. Unknown field names are dropped (existing lenient contract);
    the result must still resolve a usable identity field.

    A schema may omit `password` for passwordless registration — but only when
    it includes an SMS-verified phone, so the account always has a working
    login path (the SMS code)."""
    if not isinstance(raw, (list, tuple)):
        raise merrors.ValueException("registration.fields must be a list")
    fields = _normalize_field_list(raw)
    by_name = {f["name"]: f for f in fields}
    if "email" not in by_name and "phone" not in by_name:
        raise merrors.ValueException(
            "registration.fields must include 'email' or 'phone'")
    if "password" not in by_name:
        phone = by_name.get("phone")
        if not phone or phone.get("verify") != "sms":
            raise merrors.ValueException(
                "registration.fields without 'password' must include 'phone' "
                "with verify='sms' — passwordless accounts log in by SMS code")
    return fields


# ---------------------------------------------------------------------------
# Extra (non-canonical) registration fields.
#
# Consumer-specific fields — promo codes, referral/tracking tokens — that are
# NOT canonical User columns. They are declared per-group in
# `auth_config.registration.extra_fields`, rendered on the hosted register page
# (silently captured from a matching URL query param when present, asked for as
# a plain text input otherwise), and captured server-side into the `extra` dict
# passed to USER_REGISTERED_HANDLER and persisted at `user.metadata["registration"]`.
# Default is an empty list — no extra fields, no behavior change.
# ---------------------------------------------------------------------------

def _normalize_extra_entry(entry):
    """Coerce one extra-field config entry into `{name, label, required}`, or
    None to drop it. Names that collide with a canonical field are dropped —
    canonical fields belong in `registration.fields`."""
    if isinstance(entry, str):
        entry = {"name": entry}
    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    if not isinstance(name, str):
        return None
    name = name.strip()
    if not name or name in CANONICAL_FIELDS or not _EXTRA_NAME_RE.match(name):
        return None
    label = entry.get("label")
    if not isinstance(label, str) or not label.strip():
        label = name.replace("_", " ").title()
    else:
        label = label.strip()
    return {"name": name, "label": label, "required": bool(entry.get("required", False))}


def _normalize_extra_field_list(raw):
    """Normalize a raw extra-fields config list into `{name, label, required}`
    dicts. Drops unknown/duplicate/canonical-colliding entries. Returns `[]`
    when `raw` is empty or unusable (the default — no extra fields)."""
    if not raw or not isinstance(raw, (list, tuple)):
        return []
    out = []
    seen = set()
    for entry in raw:
        norm = _normalize_extra_entry(entry)
        if norm is None:
            continue
        if norm["name"] in seen:
            continue
        seen.add(norm["name"])
        out.append(norm)
    return out


def resolve_extra_fields(group=None, request=None):
    """Resolve the per-group extra (non-canonical) register fields.

    Reads `registration.extra_fields` from the group's resolved auth config;
    defaults to an empty list. `request` enables the test-mode header override
    (`X-Mojo-Test-Register-Extra-Fields`, a JSON list), honored only when the
    test-mode gate passes (loopback + flag + no proxy chain).
    """
    raw = None
    header_value = _read_test_header(request, "X-Mojo-Test-Register-Extra-Fields")
    if header_value is not None:
        try:
            parsed = json.loads(header_value)
            if isinstance(parsed, list):
                raw = parsed
        except (json.JSONDecodeError, TypeError):
            raw = None
    if raw is None:
        from mojo.apps.account.services import auth_config
        cfg = auth_config.resolve_auth_config(group=group, request=request)
        raw = cfg.registration.extra_fields
    return _normalize_extra_field_list(raw)


def extra_field_names(extra_fields):
    """The list of declared extra-field names — used by on_register to extend
    the capture allowlist with what a group has declared."""
    return [ef["name"] for ef in extra_fields]


def validate_extra_fields_config(raw):
    """Validate an auth-config `registration.extra_fields` list. Raises
    ValueException on bad config. Returns the normalized list."""
    if not isinstance(raw, (list, tuple)):
        raise merrors.ValueException("registration.extra_fields must be a list")
    for entry in raw:
        if isinstance(entry, str):
            name = entry
        elif isinstance(entry, dict):
            name = entry.get("name")
        else:
            raise merrors.ValueException(
                "registration.extra_fields entries must be objects with a 'name'")
        if not isinstance(name, str) or not name.strip():
            raise merrors.ValueException(
                "each registration.extra_fields entry needs a non-empty 'name'")
        if name.strip() in CANONICAL_FIELDS:
            raise merrors.ValueException(
                f"registration.extra_fields name '{name}' collides with a canonical "
                f"field — declare canonical fields in registration.fields instead")
        if not _EXTRA_NAME_RE.match(name.strip()):
            raise merrors.ValueException(
                f"registration.extra_fields name '{name}' must be a simple identifier "
                f"(a letter followed by letters, digits, or underscores)")
        if isinstance(entry, dict):
            if "label" in entry and not isinstance(entry.get("label"), str):
                raise merrors.ValueException(
                    "registration.extra_fields 'label' must be a string")
            if "required" in entry and not isinstance(entry.get("required"), bool):
                raise merrors.ValueException(
                    "registration.extra_fields 'required' must be a boolean")
    return _normalize_extra_field_list(raw)


def resolve_identity_field(fields, group=None):
    from mojo.apps.account.services import auth_config
    cfg = auth_config.resolve_auth_config(group=group)
    explicit = (cfg.registration.identity_field or "").strip()
    if explicit in ("email", "phone"):
        return explicit
    by_name = {f["name"]: f for f in fields}
    if "email" in by_name and by_name["email"]["required"]:
        return "email"
    if "phone" in by_name and by_name["phone"]["required"]:
        return "phone"
    if "email" in by_name:
        return "email"
    if "phone" in by_name:
        return "phone"
    # Schema with neither email nor phone is invalid — surface immediately so
    # operators see the config bug rather than a confusing downstream error.
    raise merrors.ValueException(
        "AUTH_REGISTER_FIELDS must include either 'email' or 'phone'")


def resolve_min_age(group=None, request=None):
    """Resolve the registration minimum-age gate from the group's auth
    config (`registration.min_age`). Accepts an X-Mojo-Test-Min-Age-Years
    header override when the test-mode gate passes."""
    header_value = _read_test_header(request, "X-Mojo-Test-Min-Age-Years")
    if header_value is not None:
        raw = header_value
    else:
        from mojo.apps.account.services import auth_config
        cfg = auth_config.resolve_auth_config(group=group, request=request)
        raw = cfg.registration.min_age
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _parse_dob(value):
    if isinstance(value, datetime.date):
        return value
    if not isinstance(value, str):
        raise merrors.ValueException("Invalid date of birth")
    try:
        return datetime.date.fromisoformat(value.strip())
    except ValueError:
        raise merrors.ValueException("Invalid date of birth")


def _age_years(dob, today=None):
    today = today or datetime.date.today()
    years = today.year - dob.year
    if (today.month, today.day) < (dob.month, dob.day):
        years -= 1
    return years


def validate_payload(fields, payload, identity_field, min_age=None):
    """Server-side validator. Strict: doesn't trust the client's render.

    Returns a sanitized dict with normalized values. Raises ValueException
    on the first failure.
    """
    from mojo.apps.account.models import User

    by_name = {f["name"]: f for f in fields}
    out = {}

    # Identity field must be one of the configured fields, and must be required.
    if identity_field not in by_name:
        raise merrors.ValueException(
            f"Identity field '{identity_field}' is not configured in AUTH_REGISTER_FIELDS")

    # password — required only when it is part of the configured schema.
    # A schema without `password` is a passwordless registration.
    if "password" in by_name:
        password = payload.get("password")
        if not password:
            raise merrors.ValueException("password is required")
        out["password"] = password

    for f in fields:
        name = f["name"]
        required = f["required"]
        raw = payload.get(name)
        # Skip password — handled above.
        if name == "password":
            continue
        if raw in (None, ""):
            if required or name == identity_field:
                raise merrors.ValueException(f"{name} is required")
            continue
        if name == "email":
            value = str(raw).lower().strip()
            if "@" not in value:
                raise merrors.ValueException("Invalid email")
            out["email"] = value
        elif name == "phone":
            normalized = User.normalize_phone(str(raw))
            if not normalized:
                raise merrors.ValueException("Invalid phone number")
            out["phone"] = normalized
        elif name == "dob":
            dob = _parse_dob(raw)
            today = datetime.date.today()
            if dob > today:
                raise merrors.ValueException("Invalid date of birth")
            if min_age is not None and _age_years(dob, today) < min_age:
                raise merrors.ValueException(f"Must be at least {min_age} years old")
            out["dob"] = dob
        elif name in ("first_name", "last_name"):
            out[name] = str(raw).strip()

    return out


def field_rows(fields):
    """Group adjacent first_name + last_name into a single 2-column row.

    Template renders one loop over rows; each row is either 1 or 2 fields.
    """
    rows = []
    i = 0
    while i < len(fields):
        f = fields[i]
        if (
            f["name"] == "first_name"
            and i + 1 < len(fields)
            and fields[i + 1]["name"] == "last_name"
        ):
            rows.append([f, fields[i + 1]])
            i += 2
        else:
            rows.append([f])
            i += 1
    return rows


def partition_for_stepped_flow(fields):
    """Split the schema into three step buckets for the phone-first stepped
    register UX.

    Returns a 3-tuple `(step1_fields, step2_active, step3_field_rows)`:
        step1_fields       — list with the identity field shown in step 1
                             (`phone` when present, else `email`)
        step2_active       — True iff the schema requires SMS verify (phone
                             with verify="sms"). Step 2 is rendered
                             statically; the template doesn't need fields.
        step3_field_rows   — every remaining field, name-pair-row-grouped
                             via field_rows() so the template can reuse
                             the existing layout helper.

    When step2_active is False the stepped flow doesn't engage — caller
    should fall back to rendering `field_rows(fields)` in a single pane.
    """
    by_name = {f["name"]: f for f in fields}
    has_sms_verify = (
        "phone" in by_name
        and by_name["phone"].get("verify") == "sms"
    )

    if not has_sms_verify:
        return [], False, field_rows(fields)

    # The identity field shown in step 1 is whichever channel needs verify —
    # which is `phone` here (we only get to this branch when phone.verify=="sms").
    step1 = [by_name["phone"]]
    step3 = [f for f in fields if f["name"] != "phone"]
    return step1, True, field_rows(step3)
