"""Typed, bounded RuleSet policy validation and safe runtime evaluation.

The Admin Security authority, Assistant tools and ticket approvals all use
this module.  Model rows created before the authority shipped remain readable
and can still be deactivated or deleted, but an invalid aggregate cannot be
activated until it is replaced with a complete policy accepted here.
"""

import re
from urllib.parse import parse_qs, urlencode, urlparse

from mojo.apps.incident.models.rule import BundleBy, MatchBy


SCHEMA_VERSION = 1
HANDLER_JOB_SCHEMA = "incident.governed_handler"
HANDLER_JOB_SCHEMA_VERSION = 1
MAX_RULES = 32
MAX_HANDLERS = 8
MAX_NAME = 160
MAX_CATEGORY = 124
MAX_PATTERN = 256
MAX_VALUE = 512
MAX_SUBJECT = 4096

_CATEGORY_RE = re.compile(r"^[A-Za-z0-9_*][A-Za-z0-9_.:*-]{0,123}$")
_FIELD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_HANDLER_CATEGORY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")


class RuleValidationError(ValueError):
    """A policy input failed the public, safe validation contract."""

    def __init__(self, message, code="invalid_policy", path=""):
        self.code = code
        self.path = path
        super().__init__(message)


# These are the event facts the governed writer may turn into an action.  The
# legacy evaluator can still read a syntactically safe metadata key so existing
# installations do not silently lose protection; only a full governed write is
# restricted to this positively allowlisted, typed roster.
FIELD_SCHEMAS = {
    "level": "int",
    "scope": "str",
    "category": "str",
    "source_ip": "str",
    "hostname": "str",
    "uid": "int",
    "country_code": "str",
    "title": "str",
    "details": "str",
    "model_name": "str",
    "model_id": "int",
    "group_id": "int",
    "http_url": "str",
    "path": "str",
    "message": "str",
    "rule_id": "int",
    "risk_score": "int",
    "severity": "int",
    "ip_recent_attack_events": "int",
    "ip_recent_distinct_targets": "int",
    "ip_recent_distinct_devices": "int",
}

OPERATORS = {
    "int": ("==", "eq", ">", ">=", "<", "<="),
    "float": ("==", "eq", ">", ">=", "<", "<="),
    "bool": ("==", "eq"),
    "str": ("==", "eq", "contains", "regex"),
}

HANDLER_SCHEMAS = {
    "notify": {"permission": ("manage_security", "security")},
    "email": {"permission": ("manage_security", "security")},
    "sms": {"permission": ("manage_security", "security")},
    "block": {"ttl_seconds": [300, 604800], "fleet_wide": True},
    "ticket": {
        "priority": [1, 10], "status": ("open", "new"),
        "board_id": [1, 2147483647],
        "category": {
            "type": "string", "max_length": 80,
            "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$",
        },
        "maestro": {"type": "boolean"},
    },
    "resolve": {
        "status": ("resolved", "closed"),
        "note": {"type": "string", "max_length": 160},
    },
    "ignore": {},
}


def public_schema():
    """Return the JSON-safe policy vocabulary owned by the server."""
    fields = []
    for name, value_type in FIELD_SCHEMAS.items():
        fields.append({
            "name": name,
            "value_type": value_type,
            "operators": list(OPERATORS[value_type]),
        })
    handlers = []
    for name, arguments in HANDLER_SCHEMAS.items():
        handlers.append({"type": name, "arguments": arguments})
    aggregate = {
        "type": "object",
        "additional_properties": False,
        "required": ["name", "category"],
        "rejected_properties": {
            "description": "RuleSet has no separately persisted description; use name",
            "match_type": "use the canonical match_by integer choice",
            "handler": "raw handler strings are never accepted; use handlers",
        },
        "properties": {
            "name": {"type": "string", "min_length": 1,
                     "max_length": MAX_NAME},
            "category": {"type": "string", "min_length": 1,
                         "max_length": MAX_CATEGORY,
                         "pattern": _CATEGORY_RE.pattern},
            "priority": {"type": "integer", "minimum": 0,
                         "maximum": 10000, "default": 50},
            "bundle_minutes": {"type": ["integer", "null"],
                               "minimum": 0, "maximum": 10080,
                               "default": 30},
            "bundle_by": {"type": "integer",
                          "enum": [value for value, _label in BundleBy.CHOICES],
                          "default": BundleBy.SOURCE_IP},
            "bundle_by_rule_set": {"type": "boolean", "default": True},
            "match_by": {"type": "integer",
                         "enum": [value for value, _label in MatchBy.CHOICES],
                         "default": MatchBy.ALL},
            "trigger_count": {"type": ["integer", "null"],
                              "minimum": 1, "maximum": 1000000,
                              "default": None},
            "trigger_window": {"type": ["integer", "null"],
                               "minimum": 1, "maximum": 10080,
                               "default": None,
                               "requires": "trigger_count"},
            "retrigger_every": {"type": ["integer", "null"],
                                "minimum": 1, "maximum": 1000000,
                                "default": None},
            "handlers": {"type": "array", "max_items": MAX_HANDLERS,
                         "items_from": "handlers", "default": []},
            "rules": {"type": "array", "max_items": MAX_RULES,
                      "items": {
                          "type": "object", "additional_properties": False,
                          "required": ["field", "operator", "value"],
                          "properties": {
                              "name": {"type": "string", "max_length": MAX_NAME},
                              "field": {"type": "string", "enum_from": "fields"},
                              "operator": {"type": "string",
                                           "enum_from": "field.operators"},
                              "value": {"max_length": MAX_VALUE},
                              "value_type": {"type": "string",
                                             "enum_from": "value_types"},
                              "is_required": {"type": "boolean", "default": False},
                          },
                      }, "default": []},
            "delete_on_resolution": {"type": "boolean", "default": False},
            "is_active": {"type": "boolean", "governed_write_value": False,
                          "activation_action": "ruleset.activate",
                          "default": False},
        },
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "aggregate": aggregate,
        "fields": fields,
        "value_types": list(OPERATORS),
        "operators": {key: list(value) for key, value in OPERATORS.items()},
        "bundle_by": [
            {"value": value, "label": label}
            for value, label in BundleBy.CHOICES
        ],
        "match_by": [
            {"value": value, "label": label}
            for value, label in MatchBy.CHOICES
        ],
        "handlers": handlers,
        "governance": {
            "revision_source": "modified",
            "revision_input": "expected_modified",
            "create_confirmation": "CREATE RULESET",
            "action_confirmation": "{ACTION} RULESET {id}",
            "catch_all_activation_confirmation": (
                "ACTIVATE CATCH-ALL RULESET {id}"),
            "replacement_requires_inactive": True,
        },
        "limits": {
            "rules": MAX_RULES,
            "handlers": MAX_HANDLERS,
            "pattern_chars": MAX_PATTERN,
            "value_chars": MAX_VALUE,
            "subject_chars": MAX_SUBJECT,
        },
    }


def _error(message, code="invalid_policy", path=""):
    raise RuleValidationError(message, code=code, path=path)


def _integer(value, name, minimum=None, maximum=None, allow_none=False):
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        _error(f"{name} must be an integer", path=name)
    if minimum is not None and value < minimum:
        _error(f"{name} must be at least {minimum}", path=name)
    if maximum is not None and value > maximum:
        _error(f"{name} must be at most {maximum}", path=name)
    return value


def _text(value, name, maximum, required=False):
    if not isinstance(value, str):
        _error(f"{name} must be a string", path=name)
    value = value.strip()
    if required and not value:
        _error(f"{name} is required", path=name)
    if len(value) > maximum:
        _error(f"{name} must be at most {maximum} characters", path=name)
    return value


def _repeat_atom_domain(parsed):
    """Return a conservative character domain for one repeated atom."""
    try:
        from re import _constants
    except Exception:
        return None
    if len(parsed) != 1:
        return None
    operation, argument = parsed[0]
    if operation == _constants.LITERAL:
        return frozenset({chr(argument).casefold()})
    if operation != _constants.IN:
        return None
    domain = set()
    for child_operation, child_argument in argument:
        if child_operation == _constants.LITERAL:
            domain.add(chr(child_argument).casefold())
        elif child_operation == _constants.RANGE:
            start, end = child_argument
            if end - start > 256:
                return None
            domain.update(chr(value).casefold() for value in range(start, end + 1))
        else:
            # Categories, negation and Unicode classes may overlap any
            # following atom. Treat them as unknown and reject conservatively.
            return None
    return frozenset(domain)


def _contains_repeat(parsed, inside_repeat=False):
    """Reject nested repetition and assertion/backreference regex features."""
    try:
        from re import _constants, _parser

        repeats = {
            _constants.MAX_REPEAT,
            _constants.MIN_REPEAT,
            getattr(_constants, "POSSESSIVE_REPEAT", object()),
        }
        forbidden = {
            _constants.ASSERT,
            _constants.ASSERT_NOT,
            _constants.GROUPREF,
            _constants.GROUPREF_EXISTS,
        }
        subpattern = _constants.SUBPATTERN
        branch = _constants.BRANCH
        in_class = _constants.IN
    except Exception:
        return True
    previous_repeat_domain = False
    for operation, argument in parsed:
        if operation in forbidden:
            return True
        if operation in repeats:
            if inside_repeat:
                return True
            # Repetition over a group/alternation is the classic ambiguous
            # backtracking shape (`(a|aa)*`). Repetition of one literal/class
            # such as `\d+` remains useful and has a linear subject cap.
            if any(child_op in (subpattern, branch)
                   for child_op, _child_arg in argument[-1]):
                return True
            if _contains_repeat(argument[-1], inside_repeat=True):
                return True
            domain = _repeat_atom_domain(argument[-1])
            if (previous_repeat_domain is not False and
                    (previous_repeat_domain is None or domain is None or
                     previous_repeat_domain & domain)):
                # Adjacent repetitions over an overlapping alphabet create a
                # large family of partitions (`a*a*a*a*a*b`) even without a
                # nested quantifier. Refuse unknown domains too.
                return True
            previous_repeat_domain = domain
        elif operation == subpattern:
            previous_repeat_domain = False
            # Capturing parentheses must not hide a repeated atom from the
            # adjacency guard (`(a*)(a*)b`). Groups with no quantifier remain
            # available for ordinary capture/alternation.
            if _contains_repeat(argument[-1], inside_repeat=True):
                return True
        elif operation == branch:
            previous_repeat_domain = False
            for child in argument[1]:
                if _contains_repeat(child, inside_repeat=inside_repeat):
                    return True
        elif operation == in_class:
            previous_repeat_domain = False
            continue
        else:
            previous_repeat_domain = False
    return False


def validate_regex(pattern):
    pattern = _text(pattern, "rule.value", MAX_PATTERN, required=True)
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
        from re import _parser
        parsed = _parser.parse(pattern, re.IGNORECASE)
    except (TypeError, ValueError, re.error):
        _error("rule.value must be a valid safe regular expression",
               code="unsafe_regex", path="rule.value")
    if _contains_repeat(parsed):
        _error("rule.value uses a regular-expression feature that is not allowed",
               code="unsafe_regex", path="rule.value")
    return compiled


def _bool_value(value, path):
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no"):
            return False
    _error(f"{path} must be a boolean", path=path)


def convert_value(value, value_type, path="rule.value"):
    try:
        if value_type == "int":
            if isinstance(value, bool):
                raise ValueError()
            return int(value)
        if value_type == "float":
            if isinstance(value, bool):
                raise ValueError()
            return float(value)
        if value_type == "bool":
            return _bool_value(value, path)
        if value_type == "str":
            if isinstance(value, (dict, list, tuple, set)):
                raise ValueError()
            text = str(value)
            if len(text) > MAX_VALUE:
                raise ValueError()
            return text
    except (TypeError, ValueError, OverflowError):
        _error(f"{path} is not a valid {value_type}", path=path)
    _error(f"unknown value type {value_type!r}", path=path)


def _normalized_rule(row, index):
    if not isinstance(row, dict):
        _error("each rule must be an object", path=f"rules.{index}")
    allowed = {"name", "field", "field_name", "operator", "comparator",
               "value", "value_type", "is_required"}
    unknown = sorted(set(row) - allowed)
    if unknown:
        _error(f"unknown rule fields: {', '.join(unknown)}",
               path=f"rules.{index}")
    field = row.get("field", row.get("field_name"))
    if ("field" in row and "field_name" in row and
            row["field"] != row["field_name"]):
        _error("rule.field and rule.field_name disagree",
               path=f"rules.{index}.field")
    if not isinstance(field, str):
        _error("rule.field is required", path=f"rules.{index}.field")
    if field.startswith("metadata."):
        field = field[9:]
    if field not in FIELD_SCHEMAS:
        _error(f"rule field {field!r} is not allowed",
               code="field_not_allowed", path=f"rules.{index}.field")
    value_type = FIELD_SCHEMAS[field]
    supplied_type = row.get("value_type")
    if supplied_type is not None and supplied_type != value_type:
        _error(f"rule field {field!r} requires value_type {value_type}",
               path=f"rules.{index}.value_type")
    if ("operator" in row and "comparator" in row and
            row["operator"] != row["comparator"]):
        _error("rule.operator and rule.comparator disagree",
               path=f"rules.{index}.operator")
    operator = row.get("operator", row.get("comparator", "=="))
    if operator not in OPERATORS[value_type]:
        _error(f"operator {operator!r} is not allowed for {value_type}",
               code="operator_not_allowed", path=f"rules.{index}.operator")
    value = convert_value(row.get("value"), value_type,
                          path=f"rules.{index}.value")
    if operator == "regex":
        validate_regex(value)
    name = _text(row.get("name", ""), f"rules.{index}.name", MAX_NAME)
    required = row.get("is_required", False)
    if required not in (False, True, 0, 1):
        _error("rule.is_required must be a boolean",
               path=f"rules.{index}.is_required")
    return {
        "name": name,
        "index": index,
        "field_name": field,
        "comparator": operator,
        "value": "true" if value is True else "false" if value is False else str(value),
        "value_type": value_type,
        "is_required": 1 if required in (True, 1) else 0,
    }


def _handler_query(values):
    return urlencode([(key, str(value)) for key, value in values if value not in (None, "")])


def normalize_handlers(rows):
    if rows in (None, ""):
        return ""
    if not isinstance(rows, list):
        _error("handlers must be an array", path="handlers")
    if len(rows) > MAX_HANDLERS:
        _error(f"handlers may contain at most {MAX_HANDLERS} entries", path="handlers")
    specs = []
    for index, row in enumerate(rows):
        path = f"handlers.{index}"
        if not isinstance(row, dict):
            _error("each handler must be an object", path=path)
        kind = row.get("type")
        if kind not in HANDLER_SCHEMAS:
            _error(f"handler type {kind!r} is not allowed",
                   code="handler_not_allowed", path=f"{path}.type")
        if kind == "ignore":
            if len(rows) != 1 or set(row) != {"type"}:
                _error("ignore must be the only handler and takes no arguments", path=path)
            return "ignore"
        if kind in ("notify", "email", "sms"):
            if set(row) - {"type", "permission"}:
                _error("notification handler contains unknown arguments", path=path)
            permission = row.get("permission", "manage_security")
            if permission not in HANDLER_SCHEMAS[kind]["permission"]:
                _error("notification permission is not allowed", path=f"{path}.permission")
            specs.append(f"{kind}://perm@{permission}")
            continue
        if kind == "block":
            if set(row) - {"type", "ttl_seconds", "fleet_wide"}:
                _error("block handler contains unknown arguments", path=path)
            ttl = _integer(row.get("ttl_seconds"), f"{path}.ttl_seconds", 300, 604800)
            fleet_wide = row.get("fleet_wide", True)
            if fleet_wide is not True:
                _error("fleet_wide must be literal true", path=f"{path}.fleet_wide")
            specs.append("block://?" + _handler_query((
                ("ttl", ttl), ("fleet_wide", 1))))
            continue
        if kind == "ticket":
            if set(row) - {
                    "type", "priority", "status", "category", "maestro",
                    "board_id"}:
                _error("ticket handler contains unknown arguments", path=path)
            priority = _integer(row.get("priority", 5), f"{path}.priority", 1, 10)
            status = row.get("status", "open")
            if status not in HANDLER_SCHEMAS[kind]["status"]:
                _error("ticket status is not allowed", path=f"{path}.status")
            category = row.get("category", "incident")
            if not isinstance(category, str) or not _HANDLER_CATEGORY_RE.fullmatch(category):
                _error("ticket category is invalid", path=f"{path}.category")
            maestro = row.get("maestro", False)
            if not isinstance(maestro, bool):
                _error("ticket maestro must be a boolean", path=f"{path}.maestro")
            board_id = _integer(
                row.get("board_id"), f"{path}.board_id", 1, 2147483647,
                allow_none=True)
            if maestro and board_id is not None:
                _error("ticket maestro and board_id are mutually exclusive",
                       path=path)
            specs.append("ticket://?" + _handler_query((
                ("priority", priority), ("status", status),
                ("category", category), ("maestro", 1 if maestro else None),
                ("board", board_id))))
            continue
        if kind == "resolve":
            if set(row) - {"type", "status", "note"}:
                _error("resolve handler contains unknown arguments", path=path)
            status = row.get("status", "resolved")
            if status not in HANDLER_SCHEMAS[kind]["status"]:
                _error("resolve status is not allowed", path=f"{path}.status")
            note = _text(row.get("note", ""), f"{path}.note", 160)
            specs.append("resolve://?" + _handler_query((("status", status), ("note", note))))
    return ",".join(specs)


def parse_handlers(raw):
    """Return typed handlers for a safe stored chain, or ``None`` for legacy."""
    if raw in (None, ""):
        return []
    if not isinstance(raw, str) or len(raw) > 2048:
        return None
    if raw.strip().rstrip(":/") == "ignore":
        return [{"type": "ignore"}]
    rows = []
    specs = re.split(r",(?=(?:notify|email|sms|ticket|block|resolve)://)", raw.strip())
    if len(specs) > MAX_HANDLERS:
        return None
    try:
        for spec in specs:
            parsed = urlparse(spec)
            kind = parsed.scheme
            if kind not in HANDLER_SCHEMAS or kind == "ignore":
                return None
            query = parse_qs(parsed.query, keep_blank_values=True)
            if parsed.fragment or parsed.params or any(
                    len(values) != 1 for values in query.values()):
                return None
            if kind in ("notify", "email", "sms"):
                if query or parsed.path or not parsed.netloc.startswith("perm@"):
                    return None
                row = {"type": kind, "permission": parsed.netloc[5:]}
            elif kind == "block":
                if (parsed.netloc or parsed.path or
                        set(query) - {"ttl", "fleet_wide"} or
                        query.get("fleet_wide", ["1"])[0] != "1"):
                    return None
                row = {
                    "type": kind,
                    "ttl_seconds": int(query.get("ttl", [""])[0]),
                    "fleet_wide": True,
                }
            elif kind == "ticket":
                if (parsed.netloc or parsed.path or
                        set(query) - {
                            "priority", "status", "category", "maestro",
                            "board"} or
                        query.get("maestro", ["0"])[0] not in ("0", "1")):
                    return None
                row = {
                    "type": kind,
                    "priority": int(query.get("priority", ["5"])[0]),
                    "status": query.get("status", ["open"])[0],
                    "category": query.get("category", ["incident"])[0],
                    "maestro": query.get("maestro", ["0"])[0] == "1",
                    "board_id": (
                        int(query["board"][0]) if "board" in query else None),
                }
            else:
                if parsed.netloc or parsed.path or set(query) - {"status", "note"}:
                    return None
                row = {
                    "type": kind,
                    "status": query.get("status", ["resolved"])[0],
                    "note": query.get("note", [""])[0],
                }
            rows.append(row)
        if normalize_handlers(rows) != raw:
            # Canonical differences are harmless (query order, omitted
            # defaults), but re-parse the canonical form to prove the values.
            normalize_handlers(rows)
        return rows
    except (RuleValidationError, TypeError, ValueError, OverflowError):
        return None


def normalize_queued_handler(handler_spec, schema, schema_version):
    """Revalidate one durable handler job against the current safe schema."""
    if (schema != HANDLER_JOB_SCHEMA or
            schema_version != HANDLER_JOB_SCHEMA_VERSION):
        _error("handler job schema is missing or stale",
               code="stale_handler_job", path="handler_schema")
    rows = parse_handlers(handler_spec)
    if rows is None or len(rows) != 1 or rows[0].get("type") == "ignore":
        _error("queued handler is outside the governed schema",
               code="handler_not_allowed", path="handler_spec")
    return normalize_handlers(rows)


def normalize_ruleset(payload):
    """Validate and normalize one complete governed RuleSet aggregate."""
    if not isinstance(payload, dict):
        _error("ruleset must be an object", path="ruleset")
    allowed = {
        "name", "category", "priority", "bundle_minutes", "bundle_by",
        "bundle_by_rule_set", "match_by", "trigger_count", "trigger_window",
        "retrigger_every", "handlers", "rules", "delete_on_resolution",
        "is_active",
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        _error(f"unknown ruleset fields: {', '.join(unknown)}", path="ruleset")
    name = _text(payload.get("name"), "ruleset.name", MAX_NAME, required=True)
    category = _text(payload.get("category"), "ruleset.category", MAX_CATEGORY,
                     required=True)
    if not _CATEGORY_RE.fullmatch(category):
        _error("ruleset.category contains unsupported characters", path="ruleset.category")
    priority = _integer(payload.get("priority", 50), "ruleset.priority", 0, 10000)
    bundle_by = _integer(payload.get("bundle_by", BundleBy.SOURCE_IP),
                         "ruleset.bundle_by", 0, 13)
    if bundle_by not in dict(BundleBy.CHOICES):
        _error("ruleset.bundle_by is not a recognized choice", path="ruleset.bundle_by")
    bundle_minutes = _integer(payload.get("bundle_minutes", 30),
                              "ruleset.bundle_minutes", 0, 10080,
                              allow_none=True)
    match_by = _integer(payload.get("match_by", MatchBy.ALL),
                        "ruleset.match_by", 0, 1)
    trigger_count = _integer(payload.get("trigger_count"),
                             "ruleset.trigger_count", 1, 1000000,
                             allow_none=True)
    trigger_window = _integer(payload.get("trigger_window"),
                              "ruleset.trigger_window", 1, 10080,
                              allow_none=True)
    retrigger_every = _integer(payload.get("retrigger_every"),
                               "ruleset.retrigger_every", 1, 1000000,
                               allow_none=True)
    if trigger_window is not None and trigger_count is None:
        _error("ruleset.trigger_window requires trigger_count",
               path="ruleset.trigger_window")
    if trigger_count is not None and trigger_count > 1:
        if bundle_by == BundleBy.NONE:
            _error("ruleset.trigger_count greater than one requires bundling",
                   path="ruleset.bundle_by")
        if not bundle_minutes or (trigger_window and bundle_minutes < trigger_window):
            _error("ruleset bundle window must cover the trigger window",
                   path="ruleset.bundle_minutes")
    bundle_by_rule_set = payload.get("bundle_by_rule_set", True)
    if not isinstance(bundle_by_rule_set, bool):
        _error("ruleset.bundle_by_rule_set must be a boolean",
               path="ruleset.bundle_by_rule_set")
    active = payload.get("is_active", False)
    if not isinstance(active, bool):
        _error("ruleset.is_active must be a boolean", path="ruleset.is_active")
    delete_on_resolution = payload.get("delete_on_resolution", False)
    if not isinstance(delete_on_resolution, bool):
        _error("ruleset.delete_on_resolution must be a boolean",
               path="ruleset.delete_on_resolution")
    rules = payload.get("rules", [])
    if not isinstance(rules, list):
        _error("ruleset.rules must be an array", path="ruleset.rules")
    if len(rules) > MAX_RULES:
        _error(f"ruleset.rules may contain at most {MAX_RULES} entries",
               path="ruleset.rules")
    return {
        "name": name,
        "category": category,
        "priority": priority,
        "bundle_minutes": bundle_minutes,
        "bundle_by": bundle_by,
        "bundle_by_rule_set": bundle_by_rule_set,
        "match_by": match_by,
        "trigger_count": trigger_count,
        "trigger_window": trigger_window,
        "retrigger_every": retrigger_every,
        "handler": normalize_handlers(payload.get("handlers", [])),
        "metadata": ({"delete_on_resolution": True}
                     if delete_on_resolution else {}),
        "is_active": active,
        "rules": [_normalized_rule(row, index) for index, row in enumerate(rules)],
    }


def ruleset_payload(rule_set):
    """Build the public aggregate shape from a stored RuleSet."""
    handlers = parse_handlers(rule_set.handler)
    if handlers is None:
        _error("stored handler chain is outside the governed schema",
               code="legacy_handler", path="ruleset.handlers")
    stored_rules = getattr(rule_set, "_admin_security_rules", None)
    if stored_rules is None:
        stored_rules = list(
            rule_set.rules.order_by("index", "id")[:MAX_RULES + 1])
    return {
        "name": rule_set.name or "",
        "category": rule_set.category,
        "priority": rule_set.priority,
        "bundle_minutes": rule_set.bundle_minutes,
        "bundle_by": rule_set.bundle_by,
        "bundle_by_rule_set": rule_set.bundle_by_rule_set,
        "match_by": rule_set.match_by,
        "trigger_count": rule_set.trigger_count,
        "trigger_window": rule_set.trigger_window,
        "retrigger_every": rule_set.retrigger_every,
        "handlers": handlers,
        "delete_on_resolution": bool(
            isinstance(rule_set.metadata, dict)
            and rule_set.metadata.get("delete_on_resolution") is True),
        "is_active": rule_set.is_active,
        "rules": [
            {
                "name": row.name or "",
                "field": row.field_name,
                "operator": row.comparator,
                "value": row.value,
                "value_type": row.value_type,
                "is_required": bool(row.is_required),
            }
            for row in stored_rules[:MAX_RULES + 1]
        ],
    }


def validate_existing(rule_set):
    payload = ruleset_payload(rule_set)
    if len(payload["rules"]) > MAX_RULES:
        _error("stored ruleset exceeds the rule limit",
               code="legacy_rule_count", path="ruleset.rules")
    return normalize_ruleset(payload)


def validation_summary(rule_set):
    try:
        normalized = validate_existing(rule_set)
        return {"status": "valid", "legacy": False,
                "handlers": parse_handlers(normalized["handler"])}
    except RuleValidationError as error:
        return {
            "status": "replacement_required",
            "legacy": True,
            "handlers": [],
            "issues": [{"code": error.code, "path": error.path}],
        }


def is_catch_all(rule_set):
    return rule_set.category == "*" or not rule_set.rules.exists()


def _legacy_field_value(rule, event):
    field_name = rule.field_name or ""
    if field_name.startswith("metadata."):
        field_name = field_name[9:]
    if not _FIELD_RE.fullmatch(field_name) or field_name.startswith("_"):
        return None
    metadata = event.metadata if isinstance(
        getattr(event, "metadata", None), dict) else {}
    # Model columns are the authoritative facts. Metadata is a fallback for
    # computed detector fields; it must not be able to shadow level/category
    # or another column merely by reusing its key.
    try:
        value = getattr(event, field_name, None)
    except Exception:
        return None
    if value is None:
        value = metadata.get(field_name)
    return value


def evaluate_rule(rule, event):
    """Evaluate any stored rule without allowing malformed legacy data to raise."""
    try:
        field_value = _legacy_field_value(rule, event)
        if field_value is None:
            return False
        value_type = rule.value_type
        operator = rule.comparator
        if value_type not in OPERATORS or operator not in OPERATORS[value_type]:
            return False
        if value_type == "str":
            if isinstance(field_value, (dict, list, tuple, set)):
                return False
            left = str(field_value)
            if len(left) > MAX_SUBJECT:
                return False
        else:
            left = convert_value(field_value, value_type)
        right = convert_value(rule.value, value_type)
        if value_type == "str" and len(right) > MAX_VALUE:
            return False
        if operator == "regex":
            return validate_regex(right).search(left) is not None
        if operator == "contains":
            return right in left
        if operator in ("==", "eq"):
            return left == right
        if operator == ">":
            return left > right
        if operator == ">=":
            return left >= right
        if operator == "<":
            return left < right
        if operator == "<=":
            return left <= right
    except Exception:
        return False
    return False
