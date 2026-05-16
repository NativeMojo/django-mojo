"""Geofence rule DSL — parser, validator, and matcher.

Rule shape:
    {
        "country": {"in": ["US", "CA"]} | {"not_in": [...]} | {"eq": "US"},
        "region":  {"in": ["US-FL", "US-NJ"]} | {"not_in": [...]} | {"eq": "US-CA"},
        "abuse":   {"tor": false, "vpn": false, "datacenter": false, "proxy": false}
    }

For `abuse` flags:
    false → the IP's flag must be False (block when True)
    true  → the IP's flag must be True (rare)
    null / absent → don't care

Empty rule `{}` matches everything (allowed=True). Validation raises a clear
ValueError at config-load time, not at request time.
"""


_VALID_TOP_KEYS = {"country", "region", "abuse"}
_VALID_OPS = {"in", "not_in", "eq"}
_VALID_ABUSE_KEYS = {"tor", "vpn", "datacenter", "proxy"}


def validate_rule(rule):
    """Raise ValueError if the rule shape is malformed. Returns None on success."""
    if not isinstance(rule, dict):
        raise ValueError(f"geofence rule must be a dict, got {type(rule).__name__}")
    for key, body in rule.items():
        if key not in _VALID_TOP_KEYS:
            raise ValueError(
                f"geofence rule: unknown top-level key {key!r}; "
                f"valid keys are {sorted(_VALID_TOP_KEYS)}"
            )
        if not isinstance(body, dict):
            raise ValueError(
                f"geofence rule: {key!r} body must be a dict, got {type(body).__name__}"
            )
        if key in ("country", "region"):
            _validate_matcher(key, body)
        else:  # abuse
            _validate_abuse(body)


def _validate_matcher(field, body):
    for op, operand in body.items():
        if op not in _VALID_OPS:
            raise ValueError(
                f"geofence rule: {field!r} has unknown operator {op!r}; "
                f"valid operators are {sorted(_VALID_OPS)}"
            )
        if op in ("in", "not_in"):
            if not isinstance(operand, (list, tuple)):
                raise ValueError(
                    f"geofence rule: {field!r}.{op!r} operand must be a list, "
                    f"got {type(operand).__name__}"
                )
            if not all(isinstance(v, str) for v in operand):
                raise ValueError(
                    f"geofence rule: {field!r}.{op!r} operand must contain strings only"
                )
        elif op == "eq":
            if not isinstance(operand, str):
                raise ValueError(
                    f"geofence rule: {field!r}.eq operand must be a string, "
                    f"got {type(operand).__name__}"
                )


def _validate_abuse(body):
    for flag, expected in body.items():
        if flag not in _VALID_ABUSE_KEYS:
            raise ValueError(
                f"geofence rule: abuse has unknown flag {flag!r}; "
                f"valid flags are {sorted(_VALID_ABUSE_KEYS)}"
            )
        if expected is not None and not isinstance(expected, bool):
            raise ValueError(
                f"geofence rule: abuse.{flag} must be true/false/null, "
                f"got {type(expected).__name__}"
            )


def evaluate_rule(rule, geo):
    """Apply a single rule dict to a geo dict.

    Returns (allowed: bool, reason: str|None) where reason is one of:
      country_not_allowed, region_not_allowed,
      tor_detected, vpn_detected, datacenter_detected, proxy_detected,
      or None on pass.

    Empty rule → (True, None). Caller is responsible for validate_rule on load.
    """
    if not rule:
        return True, None

    # country
    cc_rule = rule.get("country")
    if cc_rule:
        cc = (geo.get("country_code") or "").upper()
        if not _matcher_passes(cc_rule, cc):
            return False, "country_not_allowed"

    # region (ISO 3166-2)
    rc_rule = rule.get("region")
    if rc_rule:
        rc = (geo.get("region_code") or "").upper()
        if not _matcher_passes(rc_rule, rc):
            return False, "region_not_allowed"

    # abuse flags
    abuse_rule = rule.get("abuse")
    if abuse_rule:
        for flag, expected in abuse_rule.items():
            if expected is None:
                continue
            actual = bool(geo.get(f"is_{flag}", False))
            if actual != bool(expected):
                # Build a specific reason for the flag that tripped
                return False, f"{flag}_detected"

    return True, None


def _matcher_passes(matcher, value):
    """Single-matcher eval. Last operator wins if multiple are present
    (Python dict insertion order). Documented in the request file's
    open-questions section."""
    for op, operand in matcher.items():
        if op == "in":
            allowed = {v.upper() for v in operand}
            if value not in allowed:
                return False
        elif op == "not_in":
            blocked = {v.upper() for v in operand}
            if value in blocked:
                return False
        elif op == "eq":
            if value != operand.upper():
                return False
    return True
