"""Version 2 protected health publication and cached-read contract.

Redis is publication storage, never the durable generation authority. Reads
cannot trigger collection and never fall back to a process-local green value.
"""
import copy
import json
import re
import secrets
import time
import uuid
from contextlib import contextmanager, nullcontext
from datetime import timedelta

from mojo.apps.account.services import health_evidence as evidence
from mojo.helpers.settings import settings

CONTRACT = "django-mojo.health_snapshot"
SCHEMA_VERSION = 2
PROFILE_VERSION = 2
STATE_KEY = "MOJO_HEALTH_PUBLICATION_STATE"
MAX_BYTES = 512 * 1024
MAX_COMPONENTS = 29
MAX_SLOTS = 2048
MAX_GENERATION = 2 ** 53 - 1
CACHE_TTL = 48 * 3600
LEASE_SECONDS = 75
READ_SECONDS = 3
RESPONSE_FIELDS = ("contract", "schema_version", "source_id", "source_identity", "publication_generation",
                   "publication_digest", "config_revision", "generated_at", "published_at", "served_at",
                   "roster_revision", "profile_version", "collection_status", "components", "coverage", "failure_evidence")
KINDS = ("api", "database", "redis", "jobman", "fleet", "monitoring")
HEX = re.compile(r"^[a-f0-9]{64}$")
ENVIRONMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class SnapshotError(Exception):
    def __init__(self, code):
        self.error_code = code
        super().__init__(code)


def _read_installation_identity(deadline):
    """Read the frozen pair through a bounded primary transaction, never create."""
    from mojo.apps.account.models import Setting
    from mojo.apps.account.services import system_settings
    with evidence.primary_cursor(deadline, max_queries=4) as cursor:
        cursor.execute('SELECT key, value FROM "%s" WHERE group_id IS NULL AND key IN (%%s, %%s)' % Setting._meta.db_table,
                       [system_settings.INSTALLATION_UUID, system_settings.INSTALLATION_SLUG])
        rows = dict(cursor.fetchall())
    return system_settings._validate_identity_pair(
        rows.get(system_settings.INSTALLATION_UUID), rows.get(system_settings.INSTALLATION_SLUG))


def resolve_identity(*, identity_reader=None, get_static=None, deadline=None):
    """Stable installation/environment identity; missing/conflicting input fails."""
    get_static = get_static or settings.get_static
    try:
        stored = (identity_reader() if identity_reader else
                  _read_installation_identity(deadline or time.monotonic() + 1))
        installation = str(uuid.UUID(stored["uuid"]))
        selected = get_static("MOJO_ENVIRONMENT", None)
        explicit = get_static("HEALTH_SNAPSHOT_ENVIRONMENT_ID", None)
        if selected and explicit and selected != explicit:
            raise ValueError()
        environment = selected or explicit
        if not isinstance(environment, str) or not ENVIRONMENT.fullmatch(environment):
            raise ValueError()
        identity = {"installation_id": installation, "environment_id": environment}
        return {"source_identity": identity, "source_id": evidence.digest([installation, environment])}
    except Exception:
        raise SnapshotError("source_identity_unresolved") from None


def configuration_revision(identity, configuration):
    return evidence.digest([identity["source_id"], PROFILE_VERSION, configuration])


def _validate_state(key, value):
    if not isinstance(value, dict) or len(value) > 64:
        raise ValueError("publication state must contain at most 64 sources")
    for source_id, row in value.items():
        if (not isinstance(source_id, str) or not HEX.fullmatch(source_id)
                or not isinstance(row, dict) or set(row) != {"generation", "config_revision"}
                or type(row["generation"]) is not int or not 0 < row["generation"] <= MAX_GENERATION
                or not isinstance(row["config_revision"], str) or not HEX.fullmatch(row["config_revision"])):
            raise ValueError("publication state is invalid")
    return value


def reserve_generation(source_id, config_revision, deadline, *, key=STATE_KEY, cursor_context=None):
    """Advance the one protected Setting under its primary row lock.

    INSERT ON CONFLICT uses the existing global unique constraint to make the
    initial row race safe. The internal bypass writes only a counter/config
    map, never snapshot history or a Setting cache.
    """
    from mojo.apps.account.models import Setting
    if (not isinstance(source_id, str) or not HEX.fullmatch(source_id)
            or not isinstance(config_revision, str) or not HEX.fullmatch(config_revision)):
        raise SnapshotError("invalid_publication_identity")
    if key != STATE_KEY and not key.startswith("TESTIT_"):
        raise SnapshotError("invalid_internal_state_key")
    table = '"%s"' % Setting._meta.db_table
    context = cursor_context or evidence.primary_cursor
    with context(deadline) as cursor:
        cursor.execute("INSERT INTO %s (key, value, is_secret, group_id, created, modified) "
                       "VALUES (%%s, '{}', false, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
                       "ON CONFLICT (key) WHERE group_id IS NULL DO NOTHING" % table, [key])
        cursor.execute("SELECT id, value, is_secret FROM %s WHERE key=%%s AND group_id IS NULL FOR UPDATE" % table, [key])
        row = cursor.fetchone()
        if not row or row[2]:
            raise SnapshotError("publication_state_invalid")
        try:
            state = json.loads(row[1])
            _validate_state(key, state)
        except (ValueError, TypeError):
            raise SnapshotError("publication_state_invalid") from None
        current = state.get(source_id, {}).get("generation", 0)
        if current >= MAX_GENERATION or (source_id not in state and len(state) >= 64):
            raise SnapshotError("publication_state_limit")
        generation = current + 1
        state[source_id] = {"generation": generation, "config_revision": config_revision}
        cursor.execute("UPDATE %s SET value=%%s, modified=CURRENT_TIMESTAMP WHERE id=%%s" % table,
                       [evidence.canonical(state), row[0]])
    return generation


@contextmanager
def publication_guard(source_id, generation, config_revision, deadline, *, key=STATE_KEY, cursor_context=None, configuration_check=None):
    """Hold the durable latest-generation fence through the Redis transaction.

    A restored Redis backup can resurrect an old lease. Its durable reserved
    generation can never become current again, even if all Redis keys agree.
    """
    from mojo.apps.account.models import Setting
    if key != STATE_KEY and not key.startswith("TESTIT_"):
        raise SnapshotError("invalid_internal_state_key")
    with (cursor_context or evidence.primary_cursor)(deadline) as cursor:
        cursor.execute('SELECT value FROM "%s" WHERE key=%%s AND group_id IS NULL FOR UPDATE' % Setting._meta.db_table, [key])
        row = cursor.fetchone()
        try:
            state = json.loads(row[0]) if row else None
            _validate_state(key, state)
            if state.get(source_id) != {"generation": generation, "config_revision": config_revision}:
                raise SnapshotError("durable_publication_fence_refused")
        except (ValueError, TypeError):
            raise SnapshotError("publication_state_invalid") from None
        if configuration_check is not None:
            configuration_check(cursor)
        yield


from mojo.apps.account.services import system_settings
system_settings.register_protected_setting(STATE_KEY, _validate_state)


def coverage(components, *, reasons=None, omitted=0, denominator_known=True):
    required = [row for row in components if row.get("required")]
    known = denominator_known and not omitted
    return {"expected_total": len(required) + omitted if denominator_known else None,
            "returned_total": len(required),
            "observed_total": sum(row.get("observed_at") is not None for row in required),
            "fresh_total": sum(row.get("coverage", {}).get("complete") is True for row in required),
            "unknown_total": sum(not row.get("coverage", {}).get("complete") for row in required),
            "omitted_total": omitted, "complete": bool(required and known and all(row.get("coverage", {}).get("complete") for row in required)),
            "truncated": bool(omitted), "reasons": list(reasons or [])}


def publication(identity, generation, config_revision, components, *, generated_at,
                published_at, roster_revision, reasons=None):
    value = {"contract": CONTRACT, "schema_version": 2, **copy.deepcopy(identity),
             "publication_generation": generation, "config_revision": config_revision,
             "generated_at": generated_at, "published_at": published_at,
             "roster_revision": roster_revision, "profile_version": PROFILE_VERSION,
             "components": copy.deepcopy(components), "coverage": coverage(components, reasons=reasons),
             "failure_evidence": {"capture_status": "best_effort", "summary_only": True}}
    value["collection_status"] = "complete" if value["coverage"]["complete"] else "partial" if components else "unknown"
    value["publication_digest"] = evidence.digest(value)
    validate_publication(value, identity)
    return value


def validate_publication(value, identity):
    """Validate the bounded immutable wire object before trusting cached facts."""
    def require(valid, code="snapshot_malformed"):
        if not valid:
            raise SnapshotError(code)
    def check_coverage(value):
        require(isinstance(value, dict))
        for name in ("expected_total", "returned_total", "observed_total", "fresh_total", "unknown_total", "omitted_total"):
            number = value.get(name)
            require((name == "expected_total" and number is None)
                    or type(number) is int and 0 <= number <= 100000)
        require(type(value.get("complete")) is bool and type(value.get("truncated")) is bool
                and isinstance(value.get("reasons"), list))
        require(type(value.get("omitted_conditions", 0)) is int and value.get("omitted_conditions", 0) >= 0)
        if value["complete"]:
            require(not value["truncated"] and value["omitted_total"] == 0 and value.get("omitted_conditions", 0) == 0
                    and value["expected_total"] is not None
                    and value["fresh_total"] == value["returned_total"] == value["expected_total"]
                    and value["unknown_total"] == 0, "coverage_omission_inconsistent")
    require(isinstance(value, dict))
    if "publication_digest" in value:
        require(value["publication_digest"] == evidence.digest({key: item for key, item in value.items() if key != "publication_digest"}), "publication_digest_mismatch")
    try:
        require(len(evidence.canonical(value).encode()) <= MAX_BYTES, "snapshot_oversize")
    except (TypeError, ValueError, RecursionError):
        raise SnapshotError("snapshot_malformed") from None
    require(value.get("contract") == CONTRACT and value.get("schema_version") == 2
            and value.get("profile_version") == PROFILE_VERSION, "snapshot_unsupported")
    require(value.get("source_identity") == identity.get("source_identity")
            and value.get("source_id") == identity.get("source_id"), "source_identity_mismatch")
    require(type(value.get("publication_generation")) is int and 0 < value["publication_generation"] <= MAX_GENERATION)
    require(isinstance(value.get("config_revision"), str) and HEX.fullmatch(value["config_revision"]))
    require(evidence.parse_time(value.get("generated_at")) is not None and evidence.parse_time(value.get("published_at")) is not None)
    require(value.get("collection_status") in ("complete", "partial", "unknown"))
    require(isinstance(value.get("roster_revision"), (str, type(None))))
    check_coverage(value.get("coverage"))
    require(isinstance(value.get("failure_evidence"), dict))
    components = value.get("components")
    require(isinstance(components, list) and len(components) <= MAX_COMPONENTS)
    seen, total_slots, total_conditions = set(), 0, 0
    for component in components:
        require(isinstance(component, dict))
        component_id = component.get("component_id")
        require(component.get("kind") in KINDS and isinstance(component.get("resource_id"), str)
                and 0 < len(component["resource_id"]) <= 256)
        require(component_id == component["kind"] + ":" + component["resource_id"] and component_id not in seen)
        seen.add(component_id)
        require(component.get("status") in ("healthy", "degraded", "unhealthy", "unknown", "stale", "not_applicable")
                and component.get("availability") in ("up", "down", "unknown", "not_applicable")
                and type(component.get("required")) is bool)
        require(isinstance(component.get("resource_scope"), str) and isinstance(component.get("resource_owner_source_id"), str)
                and isinstance(component.get("service_id"), (str, type(None))))
        require(component.get("identity_grant") in evidence.GRANTS)
        check_coverage(component.get("coverage"))
        require(isinstance(component.get("reasons"), list))
        for name in ("observed_at", "last_success_at", "fresh_until", "sample_id"):
            require(name in component)
            require(component[name] is None or (isinstance(component[name], str) and
                    (HEX.fullmatch(component[name]) if name == "sample_id" else evidence.parse_time(component[name]) is not None)))
        slots, conditions = component.get("evidence"), component.get("conditions")
        require(isinstance(slots, list) and isinstance(conditions, list))
        total_slots += len(slots)
        total_conditions += len(conditions)
        require(total_slots <= MAX_SLOTS and total_conditions <= evidence.MAX_CONDITIONS)
        by_id = {}
        for slot in slots:
            require(isinstance(slot, dict) and isinstance(slot.get("slot_id"), str)
                    and 0 < len(slot["slot_id"]) <= 256 and slot["slot_id"] not in by_id)
            by_id[slot["slot_id"]] = slot
            require(slot.get("required_grant") in evidence.GRANTS and type(slot.get("required")) is bool)
            require(all(key in slot for key in ("source", "source_identity", "status", "reason", "observed_at",
                        "collected_at", "last_success_at", "fresh_until", "sample_id", "value", "unit", "provenance", "limits", "truncated")))
            for name in ("observed_at", "collected_at", "last_success_at", "fresh_until"):
                require(slot[name] is None or evidence.parse_time(slot[name]) is not None)
            require(slot["sample_id"] is None or isinstance(slot["sample_id"], str) and HEX.fullmatch(slot["sample_id"]))
            require(type(slot["truncated"]) is bool and isinstance(slot["provenance"], dict)
                    and isinstance(slot["limits"], dict) and isinstance(slot["source_identity"], dict))
            if slot["observed_at"] is not None:
                expected_sample = evidence.digest([value["source_id"], component_id, slot["slot_id"],
                                                   slot["source_identity"], evidence.timestamp(evidence.parse_time(slot["observed_at"]))])
                require(slot["sample_id"] == expected_sample, "source_sample_identity_mismatch")
                maximum = evidence.slot_deadline(slot)
                actual = evidence.parse_time(slot["fresh_until"])
                require(maximum is not None and actual is not None and actual <= maximum, "source_deadline_invalid")
                require(slot["last_success_at"] == slot["collected_at"], "source_success_time_invalid")
        condition_keys = set()
        for condition in conditions:
            require(isinstance(condition, dict) and isinstance(condition.get("condition_key"), str)
                    and HEX.fullmatch(condition["condition_key"]) and condition["condition_key"] not in condition_keys)
            condition_keys.add(condition["condition_key"])
            require(condition.get("outcome") in ("pass", "fail", "unknown", "not_applicable")
                    and condition.get("impact") in ("availability", "operational"))
            basis = condition.get("basis")
            require(isinstance(basis, list) and len(basis) <= evidence.MAX_BASIS)
            require(condition["outcome"] not in ("pass", "fail") or bool(basis))
            basis_ids = set()
            for fact in basis:
                require(isinstance(fact, dict) and fact.get("slot_id") in by_id
                        and fact["slot_id"] not in basis_ids and isinstance(fact.get("resource_key"), str))
                basis_ids.add(fact["slot_id"])
                authoritative = by_id[fact["slot_id"]]
                require(all(fact.get(key) == authoritative.get(key) for key in ("sample_id", "observed_at", "fresh_until")), "causal_basis_mismatch")
    require(not value["coverage"]["complete"] or all(row["coverage"]["complete"] for row in components if row["required"]), "coverage_omission_inconsistent")
    return value


_RESERVE_LUA = """-- health_snapshot.reserve.v2
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3])
return 1
"""
_RELEASE_LUA = """-- health_snapshot.release.v2
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('DEL', KEYS[1], KEYS[2])
return 1
"""
_PUBLISH_LUA = """-- health_snapshot.publish.v2
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
if redis.call('GET', KEYS[2]) ~= ARGV[2] then return 0 end
local reservation = cjson.decode(ARGV[2])
local publication = cjson.decode(ARGV[3])
local lease = cjson.decode(ARGV[1])
if reservation.lease ~= ARGV[1] or lease.source_id ~= reservation.source_id or lease.config_revision ~= reservation.config_revision then return 0 end
if publication.source_id ~= reservation.source_id or publication.config_revision ~= reservation.config_revision then return 0 end
if publication.publication_generation ~= reservation.generation then return 0 end
local generation = tonumber(redis.call('GET', KEYS[5]) or '0')
if generation >= reservation.generation then return 0 end
local current = nil
if redis.call('STRLEN', KEYS[3]) <= tonumber(ARGV[6]) then current = redis.call('GET', KEYS[3]) end
if current then redis.call('SET', KEYS[4], current, 'EX', ARGV[4]) end
redis.call('SET', KEYS[3], ARGV[3], 'EX', ARGV[4])
redis.call('SET', KEYS[5], ARGV[5], 'EX', ARGV[4])
redis.call('DEL', KEYS[1], KEYS[2])
return 1
"""
_READ_LUA = """-- health_snapshot.read.v2
local size = redis.call('STRLEN', KEYS[1])
if size == 0 then return {'', 'missing', ''} end
if size > tonumber(ARGV[1]) then return {'', 'oversize', ''} end
if redis.call('STRLEN', KEYS[2]) > 32 then return {'', 'malformed', ''} end
return {redis.call('GET', KEYS[1]), 'ok', redis.call('GET', KEYS[2]) or ''}
"""


class PublicationCache:
    def __init__(self, source_id, *, connect=None, prefix="mojo:health", clock=None, guard=None):
        if not isinstance(source_id, str) or not HEX.fullmatch(source_id):
            raise SnapshotError("source_identity_unresolved")
        self.source_id, self.connect = source_id, connect
        self.prefix, self.clock = prefix, clock or time.monotonic
        self.guard = guard or publication_guard

    def keys(self):
        base = self.prefix + ":{" + self.source_id + "}:"
        return {name: base + name for name in ("lease", "reservation", "current", "previous", "published")}

    def connection(self, deadline):
        from mojo.helpers.redis import get_bounded_connection
        if deadline <= self.clock():
            raise SnapshotError("snapshot_deadline_exceeded")
        return (self.connect or get_bounded_connection)(timeout=0.5, max_connections=1,
                                                       read_from_replicas=False, deadline=deadline)

    def acquire(self, config_revision, deadline):
        if not isinstance(config_revision, str) or not HEX.fullmatch(config_revision):
            raise SnapshotError("invalid_publication_identity")
        lease = evidence.canonical({"token": secrets.token_hex(24), "source_id": self.source_id,
                                    "config_revision": config_revision})
        with self.connection(deadline) as client:
            return lease if client.set(self.keys()["lease"], lease, nx=True, ex=LEASE_SECONDS) else None

    def reserve(self, lease, config_revision, deadline, *, reserve=None):
        try:
            bound = json.loads(lease)
            if bound["source_id"] != self.source_id or bound["config_revision"] != config_revision:
                raise SnapshotError("publication_configuration_changed")
        except (ValueError, TypeError, KeyError):
            raise SnapshotError("publication_lease_lost") from None
        keys = self.keys()
        with self.connection(deadline) as client:
            if not lease or client.get(keys["lease"]) != lease:
                raise SnapshotError("publication_lease_lost")
        generation = (reserve or reserve_generation)(self.source_id, config_revision, deadline)
        reservation = evidence.canonical({"source_id": self.source_id, "config_revision": config_revision,
                                          "generation": generation, "lease": lease})
        with self.connection(deadline) as client:
            if client.eval(_RESERVE_LUA, 2, keys["lease"], keys["reservation"], lease, reservation, LEASE_SECONDS) != 1:
                raise SnapshotError("publication_lease_lost")
        return generation, reservation

    def publish(self, value, lease, reservation, deadline, *, current_identity, current_config_revision):
        validate_publication(value, current_identity)
        if value["source_id"] != self.source_id or value["config_revision"] != current_config_revision:
            raise SnapshotError("publication_configuration_changed")
        keys = self.keys()
        with self.guard(self.source_id, value["publication_generation"], current_config_revision, deadline):
            with self.connection(deadline) as client:
                accepted = client.eval(_PUBLISH_LUA, 5, *(keys[name] for name in ("lease", "reservation", "current", "previous", "published")),
                                       lease, reservation, evidence.canonical(value), CACHE_TTL,
                                       str(value["publication_generation"]), MAX_BYTES)
        if accepted != 1:
            raise SnapshotError("publication_fence_refused")
        return True

    def release(self, lease, deadline):
        keys = self.keys()
        with self.connection(deadline) as client:
            return client.eval(_RELEASE_LUA, 2, keys["lease"], keys["reservation"], lease) == 1

    def read(self, identity, deadline):
        keys = self.keys()
        with self.connection(deadline) as client:
            raw, state, published = client.eval(_READ_LUA, 2, keys["current"], keys["published"], MAX_BYTES)
        if state != "ok":
            raise SnapshotError("snapshot_" + state)
        try:
            value = json.loads(raw)
            validate_publication(value, identity)
            if str(value["publication_generation"]) != str(published):
                raise SnapshotError("publication_generation_mismatch")
            return value
        except (ValueError, TypeError, RecursionError):
            raise SnapshotError("snapshot_malformed") from None


def unknown(identity, reason, now=None):
    return {"contract": CONTRACT, "schema_version": 2,
            "source_id": (identity or {}).get("source_id"),
            "source_identity": (identity or {}).get("source_identity"),
            "publication_generation": None, "generated_at": None, "published_at": None,
            "publication_digest": None,
            "served_at": evidence.timestamp(now or evidence.utcnow()), "roster_revision": None,
            "profile_version": PROFILE_VERSION, "collection_status": "unknown", "components": [],
            "coverage": coverage([], reasons=[reason], denominator_known=False),
            "failure_evidence": {"capture_status": "best_effort", "gaps": ["not_read"]}}


def project_publication(value, grants, *, now=None):
    """Re-evaluate expiry and source grants without modifying immutable bytes."""
    now = now or evidence.utcnow()
    result = {key: copy.deepcopy(value[key]) for key in RESPONSE_FIELDS if key in value}
    result["publication_digest"] = evidence.digest({key: item for key, item in value.items() if key != "publication_digest"})
    components, denied, denied_components = [], False, {}
    for item in result["components"]:
        owner = item["identity_grant"]
        if not grants.get(owner):
            denied = True
            denied_components[owner] = True
            continue
        slots, denied_ids, denied_grants = [], set(), {}
        for slot in item["evidence"]:
            if not grants.get(slot["required_grant"]):
                denied = True
                denied_ids.add(slot["slot_id"])
                grant = slot["required_grant"]
                denied_grants[grant] = denied_grants.get(grant, False) or slot["required"]
            else:
                if slot.get("status") == "ok" and not evidence.slot_is_fresh(slot, now):
                    if any(evidence.valid_time(slot.get(key), now) is None for key in ("observed_at", "collected_at", "last_success_at")):
                        slot.update(status="unknown", reason="source_time_invalid", fresh_until=None)
                    else:
                        slot.update(status="stale", reason="sample_expired")
                slots.append(slot)
        # One denial per grant, independent of how many protected resources or
        # measurements it concealed. Denied counts are not an identity oracle.
        for grant, required in sorted(denied_grants.items()):
            slots.append(evidence.slot(value["source_id"], item["component_id"], "permission_denied:" + grant,
                                      source="restricted", required_grant=grant,
                                      status="permission_denied", reason="permission_required", required=required, now=now))
        conditions = []
        for condition in item["conditions"]:
            if any(fact["slot_id"] in denied_ids for fact in condition["basis"]):
                continue
            if condition["outcome"] in ("pass", "fail") and any(
                    not evidence.slot_is_fresh(next(slot for slot in slots if slot["slot_id"] == fact["slot_id"]), now)
                    for fact in condition["basis"]):
                condition.update(outcome="unknown", reason="causal_basis_unavailable")
            conditions.append(condition)
        projected = evidence.component(value["source_id"], item["kind"], item["resource_id"], slots, conditions,
                                       service_id=item["service_id"], resource_scope=item["resource_scope"],
                                       resource_owner_source_id=item["resource_owner_source_id"], required=item["required"],
                                       identity_grant=owner,
                                       coverage_complete=item["coverage"]["complete"] and not denied_ids
                                           and not item["coverage"].get("omitted_total") and not item["coverage"].get("truncated")
                                           and not item["coverage"].get("omitted_conditions"),
                                       reasons=["permission_required"] if denied_ids else item["reasons"], now=now)
        for key in ("expected_total", "omitted_total", "truncated", "omitted_conditions"):
            projected["coverage"][key] = item["coverage"].get(key, False if key == "truncated" else 0)
        if denied_ids:
            # Composite identity can encode restricted observations and is not
            # confirmation proof; never expose the privileged cached hash.
            projected["sample_id"] = None
            projected["coverage"]["expected_total"] = None
        components.append(projected)
    for owner in sorted(denied_components):
        placeholder = evidence.slot(value["source_id"], "monitoring:restricted_" + owner, "permission_denied",
            source="restricted", required_grant=owner, status="permission_denied", reason="permission_required", now=now)
        coarse = evidence.component(value["source_id"], "monitoring", "restricted_" + owner, [placeholder], [],
                                    resource_scope="restricted", identity_grant="platform", now=now)
        coarse["sample_id"] = None
        coarse["coverage"]["expected_total"] = None
        components.append(coarse)
    result["components"] = components
    result["coverage"] = coverage(components, reasons=["permission_required"] if denied else value["coverage"].get("reasons"),
                                  omitted=value["coverage"].get("omitted_total", 0), denominator_known=not denied and value["coverage"].get("expected_total") is not None)
    result["collection_status"] = "complete" if result["coverage"]["complete"] else "partial" if components else "unknown"
    if denied:
        result["roster_revision"] = None
    result["failure_evidence"] = {"capture_status": "best_effort", "summary_only": True}
    result["served_at"] = evidence.timestamp(now)
    return result


def _current_actor(actor, deadline):
    from mojo.apps.account.models import User
    if not isinstance(actor, User):
        return actor
    # Direct service calls receive the same revocation protection as a newly
    # authenticated REST/MCP request, without trusting a long-lived User copy.
    with evidence.primary_cursor(deadline, max_queries=4) as cursor:
        cursor.execute('SELECT is_active, is_superuser, permissions FROM "%s" WHERE id=%%s' % User._meta.db_table, [actor.pk])
        row = cursor.fetchone()
    if row is None:
        raise SnapshotError("permission_denied")
    actor = copy.copy(actor)
    actor.is_active, actor.is_superuser, actor.permissions = row
    if isinstance(actor.permissions, str):
        actor.permissions = json.loads(actor.permissions)
    return actor


def read_snapshot(actor, evidence_cursor=None, *, connect=None, identity_reader=None,
                  actor_loader=None, failure_reader=None, now=None, deadline=None):
    """Only cached bytes and one failure page; never collection/refresh on miss."""
    from mojo.apps.aws.services.fleet_health import source_grants, FleetHealthError
    if evidence_cursor is not None and (not isinstance(evidence_cursor, str) or not 0 < len(evidence_cursor) <= 4096):
        raise SnapshotError("invalid_evidence_cursor")
    now = now or evidence.utcnow()
    deadline = min(deadline or time.monotonic() + READ_SECONDS, time.monotonic() + READ_SECONDS)
    try:
        actor = (actor_loader or _current_actor)(actor, deadline)
        grants = source_grants(actor)
        if not grants["platform"]:
            raise SnapshotError("permission_denied")
    except FleetHealthError:
        raise SnapshotError("permission_denied") from None
    except SnapshotError:
        raise
    except Exception:
        raise SnapshotError("permission_validation_unavailable") from None
    identity = None
    try:
        identity = (identity_reader or (lambda: resolve_identity(deadline=deadline)))()
        cached = PublicationCache(identity["source_id"], connect=connect).read(identity, deadline)
        result = project_publication(cached, grants, now=now)
    except Exception as exc:
        reason = exc.error_code if isinstance(exc, SnapshotError) else (
            str(exc) if str(exc) == "bounded_cluster_startup_unsupported" else "snapshot_unavailable")
        result = unknown(identity, reason, now)
    if grants["jobs"] and identity:
        try:
            result["failure_evidence"] = (failure_reader or evidence.failure_page)(
                identity["source_id"], evidence_cursor, deadline=deadline, now=now)
        except evidence.EvidenceError as exc:
            raise SnapshotError(exc.code) from None
    else:
        result["failure_evidence"] = {"status": "permission_denied", "reason": "jobs_permission_required"}
    return result


def mcp_projection(value, evidence_cursor=None):
    """Fit transport bytes without advancing past evidence the caller never saw."""
    result = {key: copy.deepcopy(value[key]) for key in RESPONSE_FIELDS if key in value}
    limit = 24 * 1024
    if len(evidence.canonical(result).encode()) <= limit:
        return result
    result["rest_reference"] = "/api/account/admin/health/snapshot"
    history = result.get("failure_evidence", {})
    if history.get("events") or history.get("terminal_fallback"):
        for name in ("events", "terminal_fallback"):
            history[name] = []
        history.update(has_more=True, next_cursor=history.get("checkpoint_cursor") or evidence_cursor)
        history["gaps"] = sorted(set(history.get("gaps", []) + ["mcp_output_limit"]))
    result["coverage"].update(complete=False, truncated=True)
    result["coverage"]["reasons"] = sorted(set(result["coverage"].get("reasons", []) + ["mcp_output_limit"]))
    result["collection_status"] = "partial"
    for component in reversed(result["components"]):
        if len(evidence.canonical(result).encode()) <= limit:
            break
        omitted = len(component["evidence"])
        component.update(evidence=[], conditions=[], status="unknown", availability="unknown",
                         sample_id=None, observed_at=None, fresh_until=None)
        component["coverage"].update(complete=False, truncated=True, omitted_total=omitted)
        component["reasons"] = ["mcp_output_limit"]
    while len(evidence.canonical(result).encode()) > limit and result["components"]:
        result["components"].pop()
        result["coverage"]["omitted_total"] += 1
        result["coverage"]["returned_total"] -= 1
    if len(evidence.canonical(result).encode()) > limit:
        checkpoint = history.get("checkpoint_cursor") or evidence_cursor
        result = unknown(None, "mcp_output_limit")
        result["rest_reference"] = "/api/account/admin/health/snapshot"
        result["failure_evidence"] = {"capture_status": "best_effort", "has_more": True,
                                      "next_cursor": checkpoint, "gaps": ["mcp_output_limit"]}
    return result


PRODUCER_FUNC = "mojo.apps.account.asyncjobs.produce_health_snapshot"


def enqueue_snapshot(*, now=None, configuration_reader=None, publisher=None):
    """One running producer plus one latest pending slot, no catch-up replay."""
    from django.db import connections, transaction
    from mojo.apps import jobs
    from mojo.apps.jobs.models import Job
    now = now or evidence.utcnow()
    configuration = (configuration_reader or evidence.source_configuration)(time.monotonic() + 3)
    source_id = configuration["source_id"]
    slot = int(now.timestamp()) // 300
    idempotency = evidence.digest([source_id, slot])
    channel = settings.get_static("ADMIN_FLEET_CONFIG_CHANNEL", "edge")
    with transaction.atomic(using="default"):
        with connections["default"].cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout='750ms'")
            cursor.execute("SELECT pg_try_advisory_xact_lock(%s,%s)", [int(source_id[:8], 16) - 2 ** 31, int(source_id[8:16], 16) - 2 ** 31])
            if not cursor.fetchone()[0]:
                return {"status": "coalesced", "reason": "dispatch_in_progress"}
        existing = Job.objects.using("default").filter(idempotency_key=idempotency).first()
        if existing and existing.status != "pending":
            return {"status": "coalesced", "job_id": str(existing.pk)}
        pending = list(Job.objects.using("default").select_for_update().filter(
            func=PRODUCER_FUNC, status="pending", payload__source_id=source_id).order_by("created")[:128])
        if len(pending) == 128:
            return {"status": "unknown", "reason": "pending_producer_limit"}
        payload = {"source_id": source_id, "slot": slot, "config_revision": configuration["config_revision"]}
        expires = now + timedelta(seconds=300 - now.timestamp() % 300)
        if pending:
            chosen = existing if existing is not None else pending[0]
            Job.objects.using("default").filter(pk=chosen.pk, status="pending").update(
                payload=payload, idempotency_key=idempotency, expires_at=expires, modified=now)
            for obsolete in pending:
                if obsolete.pk != chosen.pk:
                    Job.objects.using("default").filter(pk=obsolete.pk, status="pending").update(status="canceled", finished_at=now, modified=now)
            return {"status": "coalesced", "job_id": str(chosen.pk), "slot": slot}
        job_id = (publisher or jobs.publish)(PRODUCER_FUNC, payload=payload, channel=channel,
            idempotency_key=idempotency, max_retries=0, expires_at=expires, max_exec_seconds=60)
        return {"status": "queued", "job_id": job_id, "slot": slot}


def _fit_publication(identity, generation, revision, components, *, generated_at, published_at, roster_revision, configuration):
    components = copy.deepcopy(components)
    def trim(component, count):
        removed = component["evidence"][-count:]
        component["evidence"] = component["evidence"][:-count]
        ids = {row["slot_id"] for row in removed}
        before = len(component["conditions"])
        component["conditions"] = [row for row in component["conditions"] if not any(fact["slot_id"] in ids for fact in row["basis"])]
        component["coverage"]["omitted_conditions"] = component["coverage"].get("omitted_conditions", 0) + before - len(component["conditions"])
        component["coverage"]["omitted_total"] += len(removed)
        component["coverage"]["returned_total"] = len(component["evidence"])
        component["coverage"]["fresh_total"] = min(component["coverage"]["fresh_total"], len(component["evidence"]))
        component["coverage"].update(complete=False, truncated=True)
        failures = [row for row in component["conditions"] if row["outcome"] == "fail"]
        down = any(row["impact"] == "availability" for row in failures)
        component.update(status="unhealthy" if down else "degraded" if failures else "unknown", availability="down" if down else "unknown", observed_at=None, fresh_until=None)
        component["reasons"] = sorted(set(component["reasons"] + ["publication_output_limit"]))
    while sum(len(row["evidence"]) for row in components) > MAX_SLOTS:
        largest = max(components, key=lambda row: len(row["evidence"]))
        trim(largest, max(1, len(largest["evidence"]) // 8))
    while sum(len(row["conditions"]) for row in components) > evidence.MAX_CONDITIONS:
        largest = max(components, key=lambda row: len(row["conditions"]))
        largest["conditions"].pop()
        largest["coverage"]["omitted_conditions"] = largest["coverage"].get("omitted_conditions", 0) + 1
        largest["coverage"].update(complete=False, truncated=True)
        largest["status"] = "unknown"
    for _ in range(256):
        try:
            result = publication(identity, generation, revision, components, generated_at=generated_at,
                published_at=published_at, roster_revision=roster_revision)
            if configuration.get("services_omitted") or not configuration.get("services_complete", True):
                result["coverage"].update(expected_total=None if not configuration.get("services_complete", True) else result["coverage"]["expected_total"] + configuration.get("services_omitted", 0), omitted_total=configuration.get("services_omitted", 0), complete=False, truncated=True)
                result["coverage"]["reasons"].append("api_roster_limit")
                result["collection_status"] = "partial"
                result["publication_digest"] = evidence.digest({key: item for key, item in result.items() if key != "publication_digest"})
            validate_publication(result, identity)
            return result
        except SnapshotError as exc:
            if exc.error_code != "snapshot_oversize":
                raise
            candidates = [row for row in components if row["evidence"]]
            if not candidates:
                raise
            largest = max(candidates, key=lambda row: len(evidence.canonical(row)))
            trim(largest, max(1, len(largest["evidence"]) // 8))
    raise SnapshotError("snapshot_oversize")


def produce_snapshot(job, *, readers=None, connect=None, configuration_reader=None, reserve=None, guard=None, now=None, monotonic=None):
    """One deterministic60s acquisition, never called by the fast read path."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from functools import partial
    from mojo.apps.aws.services import fleet_health, capacity
    from mojo.helpers.aws.provider_call import DeadlineProviderClient
    from mojo.helpers.redis import get_bounded_connection
    from mojo.apps.jobs.manager import get_manager
    from mojo.apps.jobs.keys import JobKeys
    from mojo.apps import jobs
    now, monotonic = now or evidence.utcnow(), monotonic or time.monotonic
    deadline, readers = monotonic() + 60, dict(readers or {})
    payload = job.payload or {}
    if type(payload.get("slot")) is not int or payload["slot"] != int(now.timestamp()) // 300:
        return {"status": "expired", "reason": "superseded_slot"}
    budget = evidence.CycleBudget(deadline, clock=monotonic)
    config_reader = configuration_reader or partial(evidence.source_configuration, budget=budget)
    configuration = config_reader(deadline)
    identity = {key: configuration[key] for key in ("source_id", "source_identity")}
    if payload.get("source_id") != identity["source_id"]:
        raise SnapshotError("source_identity_mismatch")
    def counted_connect(**kwargs):
        return evidence.BudgetRedis((connect or get_bounded_connection)(**kwargs), budget)
    cursor_context = lambda end: evidence.primary_cursor(end, budget=budget)
    def check_configuration(cursor):
        if configuration_reader is None:
            from mojo.apps.account.models import Setting
            cursor.execute('SELECT key FROM "%s" WHERE group_id IS NULL AND key IN (%%s,%%s,%%s,%%s) FOR SHARE' % Setting._meta.db_table,
                [system_settings.INSTALLATION_UUID, system_settings.INSTALLATION_SLUG, system_settings.BASE_URL, system_settings.EXPECTED_EDGE_TOPOLOGY])
            current = evidence.source_configuration(deadline, budget=budget, cursor_context=lambda end: nullcontext(cursor))
        else:
            current = configuration_reader(deadline)
        if any(current[key] != configuration[key] for key in ("source_id", "source_identity", "config_revision")):
            raise SnapshotError("publication_configuration_changed")
    cache = PublicationCache(identity["source_id"], connect=counted_connect,
        guard=guard or partial(publication_guard, cursor_context=cursor_context, configuration_check=check_configuration))
    lease = cache.acquire(configuration["config_revision"], deadline)
    if lease is None:
        return {"status": "coalesced", "reason": "producer_running"}
    try:
        generation, reservation = cache.reserve(lease, configuration["config_revision"], deadline,
            reserve=reserve or partial(reserve_generation, cursor_context=cursor_context))
        try:
            previous = cache.read(identity, deadline)
        except Exception:
            previous = None
        class InternalActor:
            is_active = True
            is_request_user = True
            def has_permission(self, permissions):
                return True
        class Provider:
            def __init__(self, service):
                self.service = service
            def __getattr__(self, operation):
                def call(**params):
                    budget.take("provider_operations")
                    if operation == "describe_instances":
                        budget.take("inventory_pages")
                    return getattr(DeadlineProviderClient(self.service, deadline, region=configuration["expected"].get("spec").region), operation)(**params)
                return call
        def inventory(spec, end):
            if spec is None:
                return {"instances": {}, "complete": False, "reasons": ["identity_unavailable"]}
            result = capacity.health_inventory(spec, end, ec2_client=Provider("ec2"), elbv2_client=Provider("elbv2"))
            result["observed_at"] = evidence.timestamp(evidence.utcnow())
            return result
        def heartbeats(end):
            with counted_connect(timeout=0.5, max_connections=2, read_from_replicas=False, deadline=end) as client:
                return get_manager().get_runners_bounded(settings.get_static("ADMIN_FLEET_CONFIG_CHANNEL", "edge"), limit=128, timeout=min(1, end - monotonic()), client=client)
        def scheduler(end):
            with counted_connect(timeout=0.5, max_connections=1, read_from_replicas=False, deadline=end) as client:
                active = bool(client.get(JobKeys().scheduler_lock()))
            return {"status": "ok", "scope": "installation", "scheduler_active": active, "observed_at": None}
        def fleet_source():
            managed = readers.get("managed", lambda: evidence.managed_series(configuration, deadline, budget=budget))()
            raw_readers = {"expected": lambda grants, end: configuration["expected"], "inventory": inventory,
                "heartbeats": heartbeats, "scheduler": scheduler, "identity_cache": fleet_health.IdentityRoster(connect=counted_connect),
                "diagnostics_batch": lambda roster, end: jobs.get_sysinfo(diagnostic=True, roster=roster, deadline=end,
                    connect=counted_connect, revision=settings.get_static("MOJO_FLEET_CONFIG_REVISION", None)), **readers.get("fleet", {})}
            acquired = fleet_health.acquire_snapshot(InternalActor(), deadline=deadline, readers=raw_readers, budget=budget, now=now, extra_series=managed["series"])
            return acquired, managed
        values, probes = {}, {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(readers.get("native", lambda: evidence.database_and_job_facts(configuration["channels"], deadline, budget=budget))): "native",
                       pool.submit(readers.get("redis", lambda: {"native": evidence.redis_facts(deadline, connect=counted_connect), "queues": evidence.queue_facts(configuration["channels"], deadline, connect=counted_connect, budget=budget)})): "redis",
                       pool.submit(fleet_source): "fleet"}
            for service in configuration["services"]:
                budget.take("api_services")
                futures[pool.submit(readers.get("probe", evidence.probe_service), service, deadline)] = service["resource_id"]
            for future in as_completed(futures):
                key = futures[future]
                try:
                    value = future.result()
                except Exception:
                    value = {"status": "unknown", "reason": "collector_unavailable"}
                if key in ("native", "redis", "fleet"):
                    values[key] = value
                else:
                    probes[key] = value
        fleet_value = values.get("fleet")
        if not isinstance(fleet_value, tuple):
            acquired = {"expected": configuration["expected"], "rows": fleet_health.reconcile(configuration["expected"]["declarations"]), "evidence": {}, "roster_revision": None}
            managed = {"series": []}
        else:
            acquired, managed = fleet_value
        components = evidence.build_components(configuration, values.get("native", {}), values.get("redis", {}).get("native", {}), acquired, probes, managed=managed, previous=previous, queues=values.get("redis", {}).get("queues", {}), now=evidence.utcnow())
        result = _fit_publication(identity, generation, configuration["config_revision"], components,
            generated_at=evidence.timestamp(now), published_at=evidence.timestamp(evidence.utcnow()),
            roster_revision=acquired.get("roster_revision"), configuration=configuration)
        cache.publish(result, lease, reservation, deadline, current_identity=identity, current_config_revision=configuration["config_revision"])
        return {"status": "published", "publication_generation": generation, "collection_status": result["collection_status"], "accounting": dict(budget.counts)}
    finally:
        if monotonic() < deadline:
            try:
                cache.release(lease, deadline)
            except Exception:
                pass
