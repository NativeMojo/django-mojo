"""
Register-form schema resolution.

Single source of truth for which fields the bouncer-hosted register form
collects and which fields the server-side `on_register` accepts. The same
list drives both the template render and the server validator so the two
cannot drift.

Public surface:

    resolve_fields(group=None) -> list[dict]
        Canonical list of `{"name", "required", "verify"}` dicts. Pulls
        `AUTH_REGISTER_FIELDS` from settings (group-scoped), falls back to
        DEFAULT_FIELDS, filters unknown names, forces password to required.

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

from mojo import errors as merrors
from mojo.helpers import test_mode as _tm
from mojo.helpers.settings import settings


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
    # Password is always required — passwordless register is a separate flow
    # and out of scope; treating "password optional" as a config bug avoids
    # creating User rows with no usable credential.
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


def resolve_fields(group=None, request=None):
    """Resolve the register field schema.

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
        raw = settings.get("AUTH_REGISTER_FIELDS", None, group=group)
    if not raw:
        return [dict(f) for f in DEFAULT_FIELDS]
    if not isinstance(raw, (list, tuple)):
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
    # Always ensure password is in the schema, even if the operator forgot.
    if "password" not in seen:
        normalized.append({"name": "password", "required": True, "verify": None})
    return normalized


def resolve_identity_field(fields, group=None):
    explicit = (settings.get("AUTH_REGISTER_IDENTITY_FIELD", "", group=group) or "").strip()
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
    """Resolve AUTH_MIN_AGE_YEARS. Accepts an X-Mojo-Test-Min-Age-Years
    header override when the test-mode gate passes."""
    header_value = _read_test_header(request, "X-Mojo-Test-Min-Age-Years")
    raw = header_value if header_value is not None else settings.get(
        "AUTH_MIN_AGE_YEARS", None, group=group)
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

    # password is always required and always validated
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
