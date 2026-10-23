"""On-demand fleet evidence. Expectations outlive responding providers/runners.

Detailed node, coverage and private snapshot acquisition share checked runtime
and provider readers. Source permissions are enforced here as well as at the
Assistant boundary. This module persists only short-lived identity aliases;
the account snapshot service owns protected health publications.
"""
import hashlib
import json
import math
import re
import time
from datetime import datetime, timezone

from django.core import signing

from mojo.helpers.request import is_signed_in_user
from mojo.helpers.settings import settings

SCHEMA_VERSION = 1
MAX_NODES = 128
MAX_BYTES = 24 * 1024
PLATFORM_PERMS = ["view_platform", "manage_platform", "admin"]
AWS_PERMS = ["manage_aws"]
JOBS_PERMS = ["manage_jobs", "view_jobs", "jobs"]
READ_PERMS = PLATFORM_PERMS + AWS_PERMS + JOBS_PERMS
CURSOR_SALT = "mojo.fleet.health.cursor.v1"
NODE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,252}$")
IDENTITY_TTL = 120


class FleetHealthError(Exception):
    def __init__(self, error_code, message=None):
        self.error_code = error_code
        self.message = message or error_code.replace("_", " ")
        super().__init__(self.message)


def _now():
    return datetime.now(timezone.utc).isoformat()


def source_grants(actor):
    # has_permission is the global User helper; group membership and ApiKey
    # identities must never open installation-wide sources.
    if (not actor or not getattr(actor, "is_active", False)
            or not is_signed_in_user(actor)):
        raise FleetHealthError("permission_denied")
    grants = {"platform": bool(actor.has_permission(PLATFORM_PERMS)),
              "aws": bool(actor.has_permission(AWS_PERMS)),
              "jobs": bool(actor.has_permission(JOBS_PERMS))}
    if not any(grants.values()):
        raise FleetHealthError("permission_denied")
    return grants


def _state(status, reason=None, **data):
    return dict(status=status, reason=reason, **data)


def _read_expected(grants, deadline, *, get_static=None, topology_reader=None, spec_reader=None):
    """Read independent declarations; malformed intent never falls back to live nodes."""
    from mojo.apps.account.services import fleet_apply, system_settings
    from mojo.apps.edge.settings_validators import expected_topology
    from mojo.apps.aws.services import capacity
    from mojo.deploy.provision import spec as provision
    get_static = get_static or settings.get_static
    topology_reader = topology_reader or system_settings.get_value
    spec_reader = spec_reader or capacity._configured_spec
    declarations, sources, configured = [], {}, False
    if grants["platform"]:
        explicit = get_static("ADMIN_FLEET_CONFIG_EXPECTED_NODES", None)
        if explicit is not None:
            configured = True
            try:
                # Reuse the existing exact configured-host validator.
                hosts = fleet_apply.expected_nodes()
                declarations.extend({"node_id": host, "hostname": host,
                                     "source": "configuration"} for host in hosts)
                sources["configuration"] = _state("ok")
            except Exception:
                sources["configuration"] = _state("unknown", "invalid_expected_nodes")
        else:
            sources["configuration"] = _state("unconfigured", "not_declared")
        try:
            raw = topology_reader(system_settings.EXPECTED_EDGE_TOPOLOGY)
            if raw is not None:
                configured = True
                topology = expected_topology(system_settings.EXPECTED_EDGE_TOPOLOGY, raw)
                declarations.extend({"node_id": node, "hostname": None,
                                     "source": "edge_topology"} for node in topology["nodes"])
                sources["edge_topology"] = _state("ok")
            else:
                sources["edge_topology"] = _state("unconfigured", "not_declared")
        except Exception:
            sources["edge_topology"] = _state("unknown", "invalid_expected_topology")
    else:
        sources.update(configuration=_state("permission_denied"), edge_topology=_state("permission_denied"))
    selected = None
    if grants["platform"] or grants["aws"]:
        try:
            selected = spec_reader()
            if selected is not None:
                configured = True
                names = provision.names(selected)
                if provision.validate_names(selected):
                    raise ValueError("invalid provision declaration")
                roles = {row["name"]: row for row in selected.node_declarations}
                for name in names["nodes"]:
                    role = roles.get(name) or {}
                    declarations.append({"node_id": name, "hostname": name,
                                         "source": "provision", "managed_name": name,
                                         "role": role.get("role"),
                                         "request_service_required": role.get("request_service")})
                sources["provision"] = _state("ok")
            else:
                sources["provision"] = _state("unconfigured", "not_declared")
        except Exception:
            configured = True
            selected = None
            sources["provision"] = _state("unknown", "identity_unavailable")
    else:
        sources["provision"] = _state("permission_denied")
    complete = not any(row["status"] in ("unknown", "permission_denied") for row in sources.values())
    if len({row["node_id"] for row in declarations}) > MAX_NODES:
        complete = False
        sources["roster"] = _state("unknown", "expected_roster_overflow")
    return {"declarations": declarations[:MAX_NODES * 3], "sources": sources,
            "configured": configured, "complete": complete, "spec": selected}


def _read_inventory(spec, deadline):
    from mojo.apps.aws.services.capacity import health_inventory
    return health_inventory(spec, deadline)


def _read_heartbeats(deadline):
    from mojo.apps.jobs.manager import get_manager
    from mojo.helpers.redis import get_bounded_connection
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("deadline_exceeded")
    channel = settings.get_static("ADMIN_FLEET_CONFIG_CHANNEL", "edge")
    with get_bounded_connection(timeout=min(1, remaining), max_connections=2,
                                read_from_replicas=False, deadline=deadline) as client:
        return get_manager().get_runners_bounded(channel, limit=128,
                                                timeout=min(1, remaining), client=client)


def _read_scheduler(deadline, *, connect=None, now=None):
    from mojo.apps.jobs.keys import JobKeys
    from mojo.helpers.redis import get_bounded_connection
    from mojo.apps.incident.services.health_liveness import read_cached
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("deadline_exceeded")
    with (connect or get_bounded_connection)(timeout=min(1, remaining), max_connections=1,
                                read_from_replicas=False, deadline=deadline) as client:
        active = bool(client.get(JobKeys().scheduler_lock()))
        detector = read_cached(client=client, now=now)
    # This is the existing installation summary, never a per-node detector.
    return _state("ok", scope="installation", scheduler_active=active,
                  observed_at=None, source="scheduler_lock", node_health="unknown", detector=detector)


def _read_diagnostics(row, runners, deadline, revision=None):
    from mojo.apps import jobs
    if not runners:
        # A jobs reader may diagnose a selected declared host without being
        # authorized to publish the platform heartbeat summary.
        runners = _read_heartbeats(deadline)
    selected = [runner for runner in runners if runner.get("hostname", "").lower() == row.get("hostname")]
    if not selected:
        return _state("unavailable", "diagnostic_channel_membership_missing")
    return jobs.get_sysinfo(diagnostic=True, roster=selected,
                           deadline=deadline, revision=revision)


def _read_trends(row, spec, mounts, hours, deadline):
    from mojo.helpers.aws.cloudwatch import CloudWatchHelper
    from mojo.deploy.provision.spec import names
    if not row.get("instance_id"):
        return _state("unavailable", "instance_unresolved")
    namespace = names(spec).get("metrics_namespace") if spec is not None else None
    return CloudWatchHelper(region=spec.region if spec else None).get_node_evidence(
        row["instance_id"], namespace=namespace or "CWAgent", mounts=mounts,
        hours=hours, deadline=deadline)


def _fresh(value, now, max_age=120):
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        age = (now - timestamp).total_seconds()
        # The roster helper owns its configured TTL; the pure join only accepts
        # its explicit alive result and rejects impossible/future fixture data.
        return -5 <= age <= max_age
    except (ValueError, TypeError, AttributeError):
        return False


def reconcile(declarations, inventory=None, runners=None, proofs=None, now=None, heartbeat_ttl=15):
    """Pure conservative join. Only uniquely scoped provision names join EC2.

    ``proofs`` are already checked host/incarnation receipts, never arbitrary
    runner ids or unchecked broadcast output. Multiple engines share one host.
    """
    now = now or datetime.now(timezone.utc)
    rows = {}
    for declaration in declarations:
        identifier = declaration.get("node_id")
        if not isinstance(identifier, str) or not NODE_RE.fullmatch(identifier):
            continue
        row = rows.setdefault(identifier, {
            "node_id": identifier, "hostname": None, "instance_id": None,
            "runner_id": None, "role": None, "request_service_required": None,
            "expected": True, "membership_sources": [], "identity_status": "resolved",
            "lifecycle": "missing", "health": "unknown", "reasons": [],
            "observed_at": None, "last_heartbeat": None, "aliases": [],
            "proof_started": None, "proof_observed_at": None,
            "provider_state": None, "target_states": [],
        })
        source = declaration.get("source")
        if source and source not in row["membership_sources"]:
            row["membership_sources"].append(source)
        for key in ("hostname", "role", "request_service_required"):
            value = declaration.get(key)
            if value is not None:
                if row[key] is not None and row[key] != value:
                    row["identity_status"] = "ambiguous"
                    row["reasons"].append("declaration_conflict")
                else:
                    row[key] = value
    facts = (inventory or {}).get("instances") or {}
    targets = {}
    for group in ((inventory or {}).get("serving") or {}).get("groups") or []:
        for target in group.get("targets") or []:
            targets.setdefault(target.get("id"), []).append(target.get("state"))
    assigned = set()
    # Name matches require a valid provision declaration AND provider scoping.
    for identifier, row in rows.items():
        if "declaration_conflict" in row["reasons"]:
            row["role"] = None
            row["request_service_required"] = None
        names = {item.get("managed_name") for item in declarations
                 if item.get("node_id") == identifier and item.get("source") == "provision"}
        matches = [fact for fact in facts.values() if fact.get("name") in names - {None}]
        if len(matches) > 1:
            row["identity_status"] = "ambiguous"
            row["reasons"].append("provider_name_ambiguous")
        elif len(matches) == 1:
            fact = matches[0]
            row["instance_id"] = fact["instance_id"]
            assigned.add(fact["instance_id"])
    for identifier, fact in sorted(facts.items()):
        if identifier in assigned:
            continue
        key = "instance:" + identifier
        rows[key] = {"node_id": key, "hostname": None, "instance_id": identifier,
                     "runner_id": None, "role": None, "request_service_required": None,
                     "expected": False, "membership_sources": ["cloud_inventory"],
                     "identity_status": "unresolved", "lifecycle": "observed",
                     "health": "unknown", "reasons": ["undeclared_managed_instance"],
                     "observed_at": None, "last_heartbeat": None,
                     "provider_state": None, "target_states": []}
    by_host = {}
    for runner in runners or []:
        host = runner.get("hostname")
        if isinstance(host, str) and host and _fresh(runner.get("last_heartbeat"), now, max_age=heartbeat_ttl) and runner.get("alive") is True:
            by_host.setdefault(host.lower(), []).append(runner)
    # An exact expected-id/hostname match is a provisional alias; drill-down
    # still requires checked local identity before accepting host diagnostics.
    for row in rows.values():
        if row["hostname"] is None and row["node_id"] in by_host:
            row["hostname"] = row["node_id"]
            row["identity_status"] = "unresolved"
    # Explicit checked proof is the only authority for a custom node id alias.
    valid_proofs = {}
    for proof in proofs or []:
        host, identity = proof.get("host"), proof.get("proof") or {}
        identifier = identity.get("node_id")
        candidates = by_host.get(host, [])
        if (identifier in rows and _fresh(identity.get("observed_at"), now)
                and any(r.get("started") == proof.get("started") for r in candidates)):
            valid_proofs.setdefault(identifier, []).append(proof)
    for identifier, receipts in valid_proofs.items():
        if identifier not in rows:
            continue
        row = rows[identifier]
        hosts = {receipt["host"] for receipt in receipts}
        if len(hosts) != 1:
            row["identity_status"] = "ambiguous"
            row["proof_observed_at"] = max(receipt["proof"]["observed_at"] for receipt in receipts)
            row["aliases"] = sorted(hosts)[:4]
            row["reasons"].append("node_proof_ambiguous")
            continue
        proof = receipts[0]
        host = proof["host"]
        if row["hostname"] is not None and row["hostname"] != host:
            row["identity_status"] = "ambiguous"
            row["reasons"].append("node_proof_ambiguous")
            continue
        # Hostname declarations are aliases when a fresh checked incarnation
        # explicitly proves a different configured logical EDGE_NODE_ID. Never
        # merge two instances or two distinct logical declarations.
        aliases = [candidate for candidate in rows.values()
                   if candidate is not row and candidate["node_id"] == host
                   and candidate["hostname"] == host and candidate["expected"]]
        conflict = any(candidate["identity_status"] == "ambiguous" or (
            candidate["instance_id"] and row["instance_id"]
            and candidate["instance_id"] != row["instance_id"]) for candidate in aliases)
        if conflict:
            row["identity_status"] = "ambiguous"
            row["reasons"].append("node_proof_ambiguous")
            continue
        for alias in aliases:
            for key in ("instance_id", "role", "request_service_required"):
                if row[key] is None:
                    row[key] = alias[key]
                elif alias[key] is not None and alias[key] != row[key]:
                    row["identity_status"] = "ambiguous"
                    row["reasons"].append("declaration_conflict")
                    row["request_service_required"] = None
            row["membership_sources"] = sorted(set(row["membership_sources"] + alias["membership_sources"]))
            row["aliases"].append(alias["node_id"])
            del rows[alias["node_id"]]
        if row["identity_status"] != "ambiguous":
            row["identity_status"] = "resolved"
        row.update(hostname=host, proof_started=proof["started"],
                   proof_observed_at=(proof["proof"] or {}).get("observed_at"))
    used_hosts = set()
    for row in rows.values():
        instance = facts.get(row["instance_id"]) or {}
        if instance:
            row["provider_state"] = instance.get("state")
            row["target_states"] = sorted(set(filter(None, targets.get(row["instance_id"], []))))
            row["lifecycle"] = instance.get("state") or "unknown"
            if instance.get("state") in ("terminated", "shutting-down"):
                row["lifecycle"] = "terminal"
                if row["expected"]:
                    row["reasons"].append("expected_terminal_conflict")
            if "draining" in row["target_states"] or "unhealthy.draining" in row["target_states"]:
                row["lifecycle"] = "draining"
        host = row["hostname"]
        candidates = by_host.get(host, [])
        if candidates:
            used_hosts.add(host)
            identities = {}
            for candidate in candidates:
                identities.setdefault(candidate.get("runner_id"), set()).add(candidate.get("started"))
            if any(len(starts) > 1 for starts in identities.values()):
                row["identity_status"] = "ambiguous"
                row["reasons"].append("runner_incarnation_ambiguous")
            candidate = sorted(candidates, key=lambda item: item.get("runner_id", ""))[0]
            row.update(runner_id=candidate.get("runner_id"),
                       last_heartbeat=candidate.get("last_heartbeat"), observed_at=candidate.get("last_heartbeat"))
            if candidate.get("draining") is True:
                row["lifecycle"] = "draining"
            elif not instance:
                row["lifecycle"] = "responding"
        if row["lifecycle"] in ("stopped", "stopping", "terminal") and row["expected"]:
            row["health"] = "unhealthy"
        if row["expected"] and not instance and not candidates:
            row["reasons"].append("expected_node_missing")
        if row["identity_status"] == "ambiguous":
            row["health"] = "unknown"
        row["membership_sources"].sort()
        row["reasons"] = sorted(set(row["reasons"]))
    # A live host that cannot be joined is visible, never used as the denominator.
    for host, candidates in sorted(by_host.items()):
        if host in used_hosts:
            continue
        key = "host:" + host
        candidate = sorted(candidates, key=lambda item: item.get("runner_id", ""))[0]
        rows[key] = {"node_id": key, "hostname": host, "instance_id": None,
                     "runner_id": candidate.get("runner_id"), "role": None,
                     "request_service_required": None, "expected": False,
                     "membership_sources": ["runner_heartbeat"], "identity_status": "unresolved",
                     "lifecycle": "draining" if candidate.get("draining") else "responding",
                     "health": "unknown", "reasons": ["undeclared_runner_host"],
                     "observed_at": candidate.get("last_heartbeat"),
                     "last_heartbeat": candidate.get("last_heartbeat"),
                     "provider_state": None, "target_states": []}
    # A hostname claimed by distinct expected identities is not a unique alias.
    for host in {row["hostname"] for row in rows.values() if row["hostname"]}:
        claimed = [row for row in rows.values() if row["hostname"] == host and row["expected"]]
        if len(claimed) > 1:
            for row in claimed:
                row.update(identity_status="ambiguous", health="unknown")
                row["reasons"].append("hostname_ambiguous")
    for row in rows.values():
        row.setdefault("aliases", [])
        row.setdefault("proof_started", None)
        row.setdefault("proof_observed_at", None)
    return [rows[key] for key in sorted(rows)]


def roster_scope(expected):
    """Cache/consumer fence for selected environment and normalized declarations."""
    spec = expected.get("spec")
    identity = ({"project": getattr(spec, "project", None), "environment": getattr(spec, "env", None),
                 "fleet": getattr(spec, "fleet", None)} if spec is not None else None)
    declarations = sorted(expected.get("declarations", []),
                          key=lambda row: (row.get("node_id", ""), row.get("source", "")))
    return hashlib.sha256(json.dumps({"identity": identity, "declarations": declarations},
                                     sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class IdentityRoster:
    """Shared 120-second identity aliases, <=128 rows/96KiB; no health evidence."""
    FIELDS = ("node_id", "hostname", "instance_id", "role", "request_service_required",
              "expected", "membership_sources", "identity_status", "aliases",
              "proof_started", "proof_observed_at")

    def __init__(self, connect=None, clock=None, prefix=None):
        self.connect = connect
        self.clock = clock or time.time
        self.prefix = prefix

    def _connection(self, deadline):
        from mojo.helpers.redis import get_bounded_connection
        connect = self.connect or get_bounded_connection
        return connect(timeout=0.25, max_connections=1,
                       read_from_replicas=False, deadline=deadline)

    def _key(self, grants, scope=None):
        mask = "".join("1" if grants[key] else "0" for key in ("platform", "aws", "jobs"))
        prefix = self.prefix if self.prefix is not None else settings.get_static("JOBS_REDIS_PREFIX", "mojo:jobs")
        return prefix + ":fleet:health:identities:v1:" + mask + (":" + scope if scope else "")

    def write(self, grants, revision, rows, deadline, *, scope=None):
        identities = [{key: row.get(key) for key in self.FIELDS} for row in rows[:128]]
        if len(rows) > MAX_NODES or any(not self._valid_identity(row) for row in identities):
            return False
        raw = json.dumps({"revision": revision, "scope": scope, "expires_at": self.clock() + IDENTITY_TTL,
                          "rows": identities}, separators=(",", ":"), allow_nan=False)
        if len(raw.encode()) > 96 * 1024:
            return False
        with self._connection(deadline) as client:
            return bool(client.set(self._key(grants, scope), raw, ex=IDENTITY_TTL))

    @staticmethod
    def _valid_identity(row):
        node_id = row.get("node_id")
        if (not isinstance(node_id, str) or type(row.get("expected")) is not bool
                or row.get("identity_status") not in ("resolved", "unresolved", "ambiguous")):
            return False
        if row.get("expected"):
            if not NODE_RE.fullmatch(node_id):
                return False
        elif not (node_id.startswith("instance:i-") or node_id.startswith("host:")):
            return False
        for key in ("hostname", "role", "instance_id"):
            if row.get(key) is not None and (not isinstance(row[key], str) or len(row[key]) > 253):
                return False
        if row.get("hostname") and not NODE_RE.fullmatch(row["hostname"]):
            return False
        if row.get("instance_id") and not re.fullmatch(r"i-[a-z0-9]{1,32}", row["instance_id"]):
            return False
        if row.get("request_service_required") is not None and type(row["request_service_required"]) is not bool:
            return False
        aliases = row.get("aliases")
        if (not isinstance(aliases, list) or len(aliases) > 4
                or any(not isinstance(alias, str) or not NODE_RE.fullmatch(alias) for alias in aliases)):
            return False
        if any(row.get(key) is not None and (not isinstance(row[key], str) or len(row[key]) > 96)
               for key in ("proof_started", "proof_observed_at")):
            return False
        sources = row.get("membership_sources")
        return (isinstance(sources, list) and len(sources) <= 5 and all(source in (
            "configuration", "edge_topology", "provision", "cloud_inventory", "runner_heartbeat") for source in sources))

    def read(self, grants, deadline, *, scope=None):
        with self._connection(deadline) as client:
            raw = client.get(self._key(grants, scope))
        if not isinstance(raw, (str, bytes)) or len(raw) > 96 * 1024:
            return None
        try:
            value = json.loads(raw)
            if (not isinstance(value, dict) or value.get("scope") != scope or type(value.get("expires_at")) not in (int, float)
                    or not math.isfinite(value["expires_at"]) or value["expires_at"] <= self.clock()
                    or value["expires_at"] > self.clock() + IDENTITY_TTL + 5
                    or not isinstance(value.get("revision"), str)
                    or not re.fullmatch("[a-f0-9]{64}", value["revision"])
                    or not isinstance(value.get("rows"), list) or len(value["rows"]) > 128):
                return None
            seen = set()
            for row in value["rows"]:
                if (not isinstance(row, dict) or set(row) != set(self.FIELDS)
                        or not isinstance(row.get("node_id"), str) or len(row["node_id"]) > 270
                        or row["node_id"] in seen or type(row["expected"]) is not bool
                        or row["identity_status"] not in ("resolved", "unresolved", "ambiguous")
                        or not self._valid_identity(row)):
                    return None
                seen.add(row["node_id"])
            if _revision(value["rows"]) != value["revision"]:
                return None
            return value
        except (ValueError, TypeError):
            return None


def _revision(rows):
    identities = [{key: row.get(key) for key in IdentityRoster.FIELDS
                   if key not in ("proof_started", "proof_observed_at")} for row in rows]
    return hashlib.sha256(json.dumps(identities, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _cursor(revision, last):
    return signing.dumps({"revision": revision, "last": last}, salt=CURSOR_SALT, compress=True)


def _parse_cursor(cursor):
    if cursor is None:
        return None
    if not isinstance(cursor, str) or not cursor or len(cursor) > 1024:
        raise FleetHealthError("invalid_cursor")
    try:
        value = signing.loads(cursor, salt=CURSOR_SALT)
        if (not isinstance(value, dict) or set(value) != {"revision", "last"}
                or not isinstance(value["revision"], str) or len(value["revision"]) != 64
                or not isinstance(value["last"], str) or len(value["last"]) > 270):
            raise ValueError()
        return value
    except (ValueError, TypeError, signing.BadSignature):
        raise FleetHealthError("invalid_cursor") from None


def _collect(actor, deadline, readers):
    grants = source_grants(actor)
    expected = readers.get("expected", _read_expected)(grants, deadline)
    sources = {"expected": {"status": "ok" if expected["complete"] else "unknown",
                            "details": expected["sources"]}}
    cached_proofs, cached_ambiguities = [], []
    if grants["jobs"]:
        try:
            record = (readers.get("identity_cache") or IdentityRoster()).read(grants, deadline, scope=roster_scope(expected))
            for identity in (record or {}).get("rows", []):
                if (identity.get("identity_status") == "ambiguous"
                        and _fresh(identity.get("proof_observed_at"), datetime.now(timezone.utc))):
                    cached_ambiguities.append(identity)
                if identity.get("proof_started") and identity.get("proof_observed_at"):
                    cached_proofs.append({"host": identity.get("hostname"),
                                          "started": identity["proof_started"],
                                          "proof": {"node_id": identity["node_id"],
                                                    "observed_at": identity["proof_observed_at"]}})
        except Exception:
            pass
    inventory, runners = {}, []
    for name, allowed, default in (("inventory", grants["aws"], _read_inventory),
                                    ("heartbeats", grants["platform"], _read_heartbeats),
                                    ("scheduler", grants["platform"], _read_scheduler)):
        if not allowed:
            sources[name] = _state("permission_denied")
            continue
        if time.monotonic() >= deadline:
            sources[name] = _state("unavailable", "deadline_exceeded")
            continue
        try:
            reader = readers.get(name, default)
            result = reader(expected.get("spec"), deadline) if name == "inventory" else reader(deadline)
            if name == "inventory":
                inventory = result
                sources[name] = _state("ok" if result.get("complete") else "unknown",
                                       reasons=result.get("reasons") or [],
                                       pages=result.get("pages", 0), row_count=result.get("row_count", 0),
                                       serving={key: (result.get("serving") or {}).get(key)
                                                for key in ("complete", "reasons", "failure")})
            elif name == "heartbeats":
                runners = result
                sources[name] = _state("ok", observed_at=_now(), count=len(result))
            else:
                sources[name] = result
        except Exception as exc:
            reason = str(exc) if str(exc) in ("runner_roster_overflow", "runner_roster_invalid", "runner_roster_timeout",
                                               "bounded_cluster_startup_unsupported", "redis_deadline_exceeded", "deadline_exceeded") else "source_unavailable"
            sources[name] = _state("unavailable", reason)
    heartbeat_ttl = readers.get("heartbeat_ttl")
    if heartbeat_ttl is None:
        try:
            heartbeat_ttl = max(1, int(settings.get_static("JOBS_RUNNER_HEARTBEAT_SEC", 5))) * 3
        except (ValueError, TypeError):
            heartbeat_ttl = 15
    rows = reconcile(expected["declarations"], inventory, runners, cached_proofs,
                     heartbeat_ttl=heartbeat_ttl)
    for identity in cached_ambiguities:
        for row in rows:
            if row["node_id"] == identity["node_id"]:
                row.update(identity_status="ambiguous", health="unknown", aliases=identity.get("aliases", []),
                           proof_observed_at=identity["proof_observed_at"])
                row["reasons"] = sorted(set(row["reasons"] + ["node_proof_ambiguous"]))
    overflow = len(rows) > MAX_NODES
    if overflow:
        sources["roster"] = _state("unknown", "identity_limit", omitted_count=len(rows) - MAX_NODES)
        # Expectations retain priority over observed-only aliases at the cap.
        rows = sorted(sorted(rows, key=lambda row: not row["expected"])[:MAX_NODES],
                      key=lambda row: row["node_id"])
    return grants, expected, rows, sources, runners, overflow, inventory


def _envelope(expected, rows, sources, overflow):
    expected_rows = [row for row in rows if row["expected"]]
    missing = sum("expected_node_missing" in row["reasons"] for row in expected_rows)
    ambiguous = sum(row["identity_status"] == "ambiguous" for row in rows)
    source_incomplete = any(row.get("status") not in ("ok", "not_applicable") for row in sources.values())
    complete = expected["complete"] and not source_incomplete and not overflow and not missing and not ambiguous
    configured = expected["configured"]
    reasons = sorted({row.get("reason") for row in sources.values() if row.get("reason")})
    if missing:
        reasons.append("expected_nodes_missing")
    if ambiguous:
        reasons.append("identity_ambiguous")
    return {"schema_version": 1, "generated_at": _now(), "roster_revision": _revision(rows), "roster_scope": roster_scope(expected),
            "collection_status": ("unconfigured" if not configured else "complete" if complete else "partial"),
            "health": "unhealthy" if any(row["health"] == "unhealthy" for row in rows) else "unknown",
            "health_scope": "provider_and_runner_presence", "coverage": {
                "expected_total": len(expected_rows) if configured and expected["complete"] and not overflow else None,
                "observed_total": sum(bool(row["instance_id"] or row["runner_id"]) for row in rows),
                "responding_total": sum(bool(row["runner_id"]) for row in rows),
                "returned": 0, "missing_total": missing, "ambiguous_total": ambiguous,
                "complete": bool(complete and configured), "has_more": False, "next_cursor": None,
                "reasons": reasons}, "sources": sources, "truncated": overflow}


def report(actor, limit=20, cursor=None, *, readers=None, deadline=None):
    """Bounded fleet page; no per-node control or CloudWatch fan-out."""
    if type(limit) is not int or not 1 <= limit <= 20:
        raise FleetHealthError("invalid_limit")
    continuation = _parse_cursor(cursor)
    readers = readers or {}
    deadline = min(deadline, time.monotonic() + 8) if deadline is not None else time.monotonic() + 8
    grants, expected, rows, sources, runners, overflow, inventory = _collect(actor, deadline, readers)
    output = _envelope(expected, rows, sources, overflow)
    revision = output["roster_revision"]
    start = 0
    if continuation:
        if continuation["revision"] != revision:
            raise FleetHealthError("roster_changed")
        ids = [row["node_id"] for row in rows]
        if continuation["last"] not in ids:
            raise FleetHealthError("invalid_cursor")
        start = ids.index(continuation["last"]) + 1
    page = rows[start:start + limit]
    output["nodes"] = page
    def coverage():
        has_more = start + len(page) < len(rows)
        output["coverage"].update(returned=len(page), has_more=has_more,
                                   next_cursor=_cursor(revision, page[-1]["node_id"]) if has_more and page else None)
        if has_more or start > 0:
            output["coverage"]["complete"] = False
            output["truncated"] = True
            output["collection_status"] = "partial" if expected["configured"] else "unconfigured"
    coverage()
    while len(json.dumps(output, separators=(",", ":")).encode()) > MAX_BYTES and len(page) > 1:
        page.pop()
        output["coverage"]["reasons"] = sorted(set(output["coverage"]["reasons"] + ["output_limit"]))
        coverage()
    try:
        cached = readers.get("identity_cache") or IdentityRoster()
        if not cached.write(grants, revision, rows, deadline, scope=roster_scope(expected)):
            output["sources"]["identity_cache"] = _state("unavailable", "identity_cache_unavailable")
    except Exception:
        output["sources"]["identity_cache"] = _state("unavailable", "identity_cache_unavailable")
    return output


def _services(row, sources, *, checked=None, runners=None, revision=None, now=None, heartbeat_ttl=None):
    """One interpretation of checked, current, role-bound request readiness."""
    required = row.get("request_service_required")
    services = {"request_service": _state("not_applicable" if required is False else "unknown",
                                      "role_excludes_request_service" if required is False else "current_service_proof_unavailable",
                                      source="declared_role", observed_at=None, unit=None,
                                      required=required),
            "runner": _state("ok" if row.get("runner_id") else "unknown",
                              None if row.get("runner_id") else "runner_proof_unavailable",
                              source="heartbeat", observed_at=row.get("last_heartbeat"), unit=None),
            "scheduler": _state("unknown", "per_node_scheduler_unproved", scope="node",
                                 installation=sources.get("scheduler"), source="scheduler_lock",
                                 observed_at=None, unit=None)}
    if not revision or not checked or row.get("identity_status") == "ambiguous":
        return services
    now = now or datetime.now(timezone.utc)
    heartbeat_ttl = heartbeat_ttl if heartbeat_ttl is not None else max(1, int(settings.get_static("JOBS_RUNNER_HEARTBEAT_SEC", 5))) * 3
    if checked.get("status") not in ("verified", "partial") or checked.get("anomalies"):
        return services
    matching = [receipt for receipt in checked.get("results", [])
                if receipt.get("host") == row.get("hostname") and receipt.get("status") == "success"]
    if len(matching) != 1 or not row.get("runner_id"):
        return services
    receipt = matching[0]
    if not any(runner.get("hostname", "").lower() == receipt.get("host")
               and runner.get("started") == receipt.get("started") and runner.get("alive")
               and _fresh(runner.get("last_heartbeat"), now, max_age=heartbeat_ttl) for runner in runners or []):
        return services
    payload = receipt.get("result") or {}
    proof = payload.get("proof") or {}
    if (payload.get("hostname") != row.get("hostname") or proof.get("node_id") != row["node_id"]
            or not _fresh(proof.get("observed_at"), now)):
        return services
    activation = payload.get("activation") or {}
    services["activation"] = {key: activation.get(key) for key in (
        "status", "reason", "scope", "revision", "receipt_status", "request_service_required",
        "request_service_profile", "observed_at", "health", "health_reason")}
    bound_required = activation.get("request_service_required")
    if (activation.get("status") != "ok" or activation.get("scope") != "configuration_activation"
            or activation.get("revision") != revision or type(bound_required) is not bool
            or not _fresh(activation.get("observed_at"), now)):
        return services
    if required not in (None, bound_required):
        row["request_service_required"] = None
        services["request_service"] = _state("unknown", "role_conflict", required=None)
        return services
    row["request_service_required"] = bound_required
    services["request_service"]["required"] = bound_required
    if bound_required is False:
        services["request_service"] = _state("not_applicable", "bound_role_excludes_request_service",
            source="configuration_activation", observed_at=activation.get("observed_at"), unit=None, required=False)
        return services
    from mojo.deploy.fleet_config_role import REQUEST_PROFILES
    service = payload.get("request_service") or {}
    if service.get("status") != "ok":
        services["request_service"]["reason"] = service.get("reason") or "current_service_proof_unavailable"
        return services
    physical = service.get("node")
    profile = activation.get("request_service_profile")
    # This is host_channel's existing normalization. Keep the raw socket proof
    # hostname in the observation rather than replacing its physical identity.
    host = physical.lower().replace(".", "-").replace("_", "-") if isinstance(physical, str) else None
    if (host != row.get("hostname")
            or service.get("loaded_revision") != revision or type(service.get("pid")) is not int
            or service["pid"] <= 0 or type(service.get("healthy")) is not bool
            or type(profile) is not str or profile not in REQUEST_PROFILES or service.get("profile") != profile
            or not _fresh(service.get("observed_at"), now, max_age=20)):
        return services
    services["request_service"] = _state("ok", source="signed_request_proof", required=True, unit=None,
        **{key: service[key] for key in ("node", "loaded_revision", "pid", "healthy", "profile", "observed_at")})
    return services


def _project_current(value):
    if not isinstance(value, dict):
        return _state("unavailable", "collector_missing")
    result = {}
    common = ("status", "reason", "source", "observed_at", "unit", "value")
    fields = {
        "cpu": ("percent", "sampling_interval_seconds", "sampling"),
        "load": ("one", "five", "fifteen"),
        "memory": ("total", "used", "available", "percent"),
        "swap": ("total", "used", "free", "percent"),
        "network": ("bytes_sent", "bytes_recv", "packets_sent", "packets_recv", "errin", "errout",
                    "dropin", "dropout", "counter_scope"),
    }
    for family, extra in fields.items():
        raw = value.get(family)
        result[family] = ({key: raw.get(key) for key in common + extra if key in raw}
                          if isinstance(raw, dict) else _state("unavailable", "collector_missing"))
    raw = value.get("filesystems") or {}
    filesystems = {key: raw.get(key) for key in common if key in raw}
    mounts = raw.get("mounts") if isinstance(raw.get("mounts"), list) else []
    filesystems["mounts"] = []
    for mount in mounts[:16]:
        if not isinstance(mount, dict):
            continue
        dto = {key: mount.get(key) for key in common + ("path", "read_only", "total", "used", "free", "percent") if key in mount}
        inodes = mount.get("inodes")
        if isinstance(inodes, dict):
            dto["inodes"] = {key: inodes.get(key) for key in common + ("total", "free") if key in inodes}
        filesystems["mounts"].append(dto)
    missing = raw.get("missing") if isinstance(raw.get("missing"), list) else []
    filesystems["missing"] = [{key: mount.get(key) for key in ("path", "status", "reason")}
                              for mount in missing[:8] if isinstance(mount, dict)]
    filesystems["omitted_count"] = max(0, int(raw.get("omitted_count") or 0)) + max(0, len(mounts) - 16)
    filesystems["omitted_missing_count"] = max(0, len(missing) - 8)
    result["filesystems"] = filesystems
    return result


def _diagnostics_complete(current):
    if any(not isinstance(current.get(family), dict) or current[family].get("status") != "ok"
           for family in ("cpu", "load", "memory", "swap", "network", "filesystems")):
        return False
    filesystems = current["filesystems"]
    return (bool(filesystems.get("mounts")) and not filesystems.get("missing")
            and not filesystems.get("omitted_count") and not filesystems.get("omitted_missing_count")
            and all(mount.get("status") == "ok" and (mount.get("inodes") or {}).get("status") in (
                "ok", "unsupported") for mount in filesystems["mounts"]))


def node(actor, node_id, hours=24, *, readers=None, deadline=None):
    """One known stable id, current checked evidence plus separate historical trends."""
    if not isinstance(node_id, str) or not node_id or len(node_id) > 270:
        raise FleetHealthError("invalid_node_id")
    if type(hours) is not int or not 1 <= hours <= 168:
        raise FleetHealthError("invalid_hours")
    readers = readers or {}
    deadline = min(deadline, time.monotonic() + 20) if deadline is not None else time.monotonic() + 20
    grants = source_grants(actor)
    expected = readers.get("expected", _read_expected)(grants, deadline)
    declarations = reconcile(expected["declarations"])
    known = next((row for row in declarations if row["node_id"] == node_id), None)
    cached = None
    try:
        cache = readers.get("identity_cache") or IdentityRoster()
        record = cache.read(grants, deadline, scope=roster_scope(expected))
        if record:
            cached = next((row for row in record["rows"] if row["node_id"] == node_id), None)
    except Exception:
        pass
    if not known and not cached:
        raise FleetHealthError("unknown_resource", "Unknown node identity; refresh get_fleet_health.")
    if (cached or known).get("identity_status") == "ambiguous":
        raise FleetHealthError("identity_ambiguous")
    grants, expected, rows, sources, runners, overflow, inventory = _collect(actor, deadline, readers)
    row = next((item for item in rows if item["node_id"] == node_id), None)
    if row is None and any(node_id in item.get("aliases", []) for item in rows):
        raise FleetHealthError("roster_changed", "A checked node identity changed; refresh get_fleet_health.")
    if row is None:
        # Preserve a recently observed alias, but never manufacture its presence.
        row = dict(cached or known, hostname=None, instance_id=None, runner_id=None,
                   role=None, request_service_required=None, aliases=[],
                   lifecycle="missing", health="unknown",
                   reasons=["node_no_longer_observed"], last_heartbeat=None, observed_at=None,
                   provider_state=None, target_states=[])
    if row.get("identity_status") == "ambiguous":
        raise FleetHealthError("identity_ambiguous")
    output = _envelope(expected, rows, sources, overflow)
    output.update(health_scope="node_diagnostics_and_scoped_services", node=row)
    output["coverage"]["returned"] = 1
    row["services"] = _services(row, sources)
    row["current"] = _state("permission_denied")
    row["trends"] = _state("permission_denied")
    revision = readers.get("revision", settings.get_static("MOJO_FLEET_CONFIG_REVISION", None)) if grants["platform"] else None
    checked = None
    if grants["jobs"] and time.monotonic() < deadline:
        try:
            checked = readers.get("diagnostics", _read_diagnostics)(
                row, runners, min(deadline, time.monotonic() + 5),
                revision)
            row["runtime"] = {key: checked.get(key) for key in (
                "status", "reason", "anomalies", "missing_hosts", "failed_hosts") if key in checked}
            receipts = checked.get("results") or []
            matching = [receipt for receipt in receipts if receipt.get("host") == row.get("hostname")
                        and receipt.get("status") == "success"]
            if checked.get("status") in ("verified", "partial") and not checked.get("anomalies") and len(matching) == 1:
                payload = matching[0].get("result") or {}
                proof = payload.get("proof") or {}
                if (proof.get("node_id") and proof["node_id"] != row["node_id"]
                        and any(item.get("node_id") == proof["node_id"] for item in expected["declarations"])):
                    if any(item["node_id"] == proof["node_id"] and item.get("identity_status") == "ambiguous"
                           and _fresh(item.get("proof_observed_at"), datetime.now(timezone.utc)) for item in rows):
                        # A single refreshed claimant cannot erase a still-fresh
                        # conflicting proof. Only expiry/new roster can clear it.
                        raise FleetHealthError("identity_ambiguous")
                    prior_proofs = [{"host": item.get("hostname"), "started": item["proof_started"],
                                     "proof": {"node_id": item["node_id"], "observed_at": item["proof_observed_at"]}}
                                    for item in rows if item.get("proof_started") and item.get("proof_observed_at")]
                    aliases = reconcile(expected["declarations"], inventory, runners, prior_proofs + [{
                        "host": matching[0]["host"], "started": matching[0].get("started"), "proof": proof}],
                        heartbeat_ttl=readers.get("heartbeat_ttl", max(1, int(settings.get_static("JOBS_RUNNER_HEARTBEAT_SEC", 5))) * 3))
                    try:
                        alias_saved = (readers.get("identity_cache") or IdentityRoster()).write(
                            grants, _revision(aliases), aliases, deadline, scope=roster_scope(expected))
                    except Exception:
                        alias_saved = False
                    if alias_saved:
                        raise FleetHealthError("roster_changed", "Checked proof resolved a logical node alias; refresh get_fleet_health.")
                    output["sources"]["identity_cache"] = _state("unavailable", "identity_alias_persistence_unavailable")
                # Checked transport binds host/incarnation. A custom explicit
                # proof identity must agree with the selected logical node.
                if (payload.get("hostname") != row.get("hostname")
                        or (row["expected"] and proof.get("node_id") and proof["node_id"] != row["node_id"])):
                    row["current"] = _state("unknown", "identity_mismatch")
                else:
                    row["current"] = _project_current(payload.get("current"))
                    if proof.get("node_id") == row["node_id"] and _fresh(proof.get("observed_at"), datetime.now(timezone.utc)):
                        row.update(identity_status="resolved", proof_started=matching[0].get("started"),
                                   proof_observed_at=proof["observed_at"])
                        output["roster_revision"] = _revision(rows)
                        try:
                            (readers.get("identity_cache") or IdentityRoster()).write(
                                grants, output["roster_revision"], rows, deadline, scope=roster_scope(expected))
                        except Exception:
                            pass
                    if proof.get("node_type") == "sites":
                        row["services"]["node_type"] = {"type": "sites", "scope": "sites_lifecycle"}
                    row["services"]["runner"].update(status="ok", source="checked_reply",
                                                       observed_at=payload.get("observed_at"))
            else:
                errors = [receipt.get("error") for receipt in receipts if receipt.get("error")]
                anomalies = checked.get("anomalies") or []
                reason = errors[0] if errors else checked.get("reason")
                if not reason:
                    if any(str(value).startswith("incompatible_host:") for value in anomalies):
                        reason = "checked_protocol_incompatible"
                    elif "identity_mismatch" in anomalies:
                        reason = "identity_mismatch"
                    elif any(str(value).startswith("duplicate_reply:") for value in anomalies):
                        reason = "checked_reply_ambiguous"
                    elif checked.get("missing_hosts"):
                        reason = "checked_reply_timeout"
                    else:
                        reason = "checked_reply_missing"
                if reason == "runner_draining":
                    row["lifecycle"] = "draining"
                row["current"] = _state("unknown" if reason in ("identity_mismatch", "checked_reply_ambiguous") else "unavailable", reason)
        except FleetHealthError:
            raise
        except Exception as exc:
            unsupported = str(exc) == "bounded_cluster_startup_unsupported"
            row["current"] = _state("unsupported" if unsupported else "unavailable",
                                    "bounded_cluster_startup_unsupported" if unsupported else "checked_transport_unavailable")
    elif grants["jobs"]:
        row["current"] = _state("unavailable", "deadline_exceeded")
    services = _services(row, sources, checked=checked, runners=runners, revision=revision,
                         heartbeat_ttl=readers.get("heartbeat_ttl"))
    row["services"]["request_service"] = services["request_service"]
    if "activation" in services:
        row["services"]["activation"] = services["activation"]
    if grants["aws"] and time.monotonic() < deadline:
        try:
            mounts = [item["path"] for item in (row.get("current", {}).get("filesystems") or {}).get("mounts", [])]
            row["trends"] = readers.get("trends", _read_trends)(row, expected.get("spec"), mounts, hours, deadline)
        except Exception:
            row["trends"] = _state("unavailable", "cloudwatch_unavailable")
    elif grants["aws"]:
        row["trends"] = _state("unavailable", "deadline_exceeded")
    # Resource readings do not invent a pressure policy or prove API liveness.
    output["health"] = row["health"]
    if not _diagnostics_complete(row["current"]) or row["trends"].get("status") != "ok":
        output["collection_status"] = "partial" if expected["configured"] else "unconfigured"
        output["coverage"]["complete"] = False
    # Keep summaries/units/window before buckets. Remove oldest detailed buckets
    # explicitly until the complete contract fits the transport's byte ceiling.
    trends = row["trends"]
    series = [trends.get("cpu"), trends.get("memory")] + trends.get("filesystems", [])
    while len(json.dumps(output, separators=(",", ":")).encode()) > MAX_BYTES:
        candidates = [metric for metric in series if isinstance(metric, dict) and metric.get("buckets")]
        if not candidates:
            break
        metric = max(candidates, key=lambda item: len(item["buckets"]))
        metric["buckets"].pop(0)
        metric["omitted_count"] = metric.get("omitted_count", 0) + 1
        output["truncated"] = True
        output["coverage"]["complete"] = False
        output["collection_status"] = "partial"
    def size():
        return len(json.dumps(output, separators=(",", ":")).encode())
    # Large full dimension sets are indivisible evidence. Omit a whole series
    # rather than publish a different, partially clipped CloudWatch identity.
    for metric in sorted((item for item in series if isinstance(item, dict)),
                         key=lambda item: len(json.dumps(item)), reverse=True):
        if size() <= MAX_BYTES:
            break
        omitted = len(metric.get("buckets") or []) + metric.get("omitted_count", 0)
        metadata = {key: metric.get(key) for key in (
            "source", "namespace", "metric", "path", "unit", "start", "end", "period_seconds",
            "aggregation", "sample_count", "last_sample_at", "minimum", "maximum", "average") if key in metric}
        metric.clear()
        metric.update(metadata, status="unavailable", reason="output_limit", dimensions=None,
                      dimensions_omitted=True, buckets=[], omitted_count=omitted,
                      omitted_series_count=1)
        output["truncated"] = True
    filesystems = row["current"].get("filesystems") or {}
    for key, count_key in (("mounts", "omitted_count"), ("missing", "omitted_missing_count")):
        while size() > MAX_BYTES and filesystems.get(key):
            filesystems[key].pop()
            filesystems[count_key] = filesystems.get(count_key, 0) + 1
            filesystems.update(status="unknown", reason="output_limit")
            output["truncated"] = True
    runtime = row.get("runtime") or {}
    while size() > MAX_BYTES and runtime.get("anomalies"):
        runtime["anomalies"].pop()
        runtime["omitted_anomalies"] = runtime.get("omitted_anomalies", 0) + 1
        output["truncated"] = True
    if size() > MAX_BYTES:
        row["current"] = _state("unavailable", "output_limit", omitted_family_count=6)
        row["trends"] = _state("unavailable", "output_limit", omitted_family_count=len(series))
        output["truncated"] = True
    if output["truncated"]:
        output["coverage"]["complete"] = False
        output["coverage"]["reasons"] = sorted(set(output["coverage"]["reasons"] + ["output_limit"]))
        output["collection_status"] = "partial"
    if size() > MAX_BYTES:
        # Bounded contract fallback even for malformed injected/older receipts.
        row.pop("runtime", None)
        row["services"] = {"request_service": _state("unknown", "output_limit"),
                           "runner": _state("unknown", "output_limit"),
                           "scheduler": _state("unknown", "per_node_scheduler_unproved")}
        output["sources"] = {key: _state(value.get("status", "unknown"), value.get("reason"))
                             for key, value in sources.items()}
    if size() > MAX_BYTES:
        raise FleetHealthError("output_limit", "The diagnostic contract could not fit its bounded response.")
    return output


def _snapshot_readers(deadline, budget, readers, *, connect_factory=None, provider_factory=None, topology_key=None, revision=None):
    """Private default readers all charge the same source-operation budget."""
    from mojo.apps.account.services import health_evidence as he
    from mojo.helpers.redis import get_bounded_connection
    from mojo.apps.jobs.manager import get_manager
    from mojo.apps.jobs.keys import JobKeys
    from mojo.apps.aws.services import capacity
    from mojo.helpers.aws.provider_call import DeadlineProviderClient
    from mojo.apps import jobs
    readers = dict(readers)
    def connect(**kwargs):
        return he.bounded_redis_connection(connect=connect_factory or get_bounded_connection, budget=budget, **kwargs)
    def inventory(spec, end):
        if spec is None:
            return {"instances": {}, "complete": False, "reasons": ["identity_unavailable"]}
        class Provider:
            def __init__(self, service):
                self.service = service
            def __getattr__(self, operation):
                def call(**params):
                    budget.take("provider_operations")
                    if operation == "describe_instances":
                        budget.take("inventory_pages")
                    client = provider_factory(self.service, end, spec.region) if provider_factory else DeadlineProviderClient(self.service, end, region=spec.region)
                    return getattr(client, operation)(**params)
                return call
        value = capacity.health_inventory(spec, end, ec2_client=Provider("ec2"), elbv2_client=Provider("elbv2"))
        value["observed_at"] = _now()
        return value
    def heartbeats(end):
        with connect(timeout=0.5, max_connections=2, read_from_replicas=False, deadline=end) as client:
            return get_manager().get_runners_bounded(settings.get_static("ADMIN_FLEET_CONFIG_CHANNEL", "edge"), limit=128, timeout=min(1, end-time.monotonic()), client=client)
    def scheduler(end):
        return _read_scheduler(end, connect=connect)
    def expected(grants, end):
        from mojo.apps.account.models import Setting
        def topology(key):
            with he.primary_cursor(end, budget=budget) as cursor:
                cursor.execute('SELECT value FROM "%s" WHERE group_id IS NULL AND key=%%s' % Setting._meta.db_table, [topology_key or key])
                row = cursor.fetchone()
            raw = row[0] if row else None
            return json.loads(raw) if isinstance(raw, str) else raw
        return _read_expected(grants, end, topology_reader=topology)
    readers.setdefault("expected", expected)
    readers.setdefault("inventory", inventory)
    readers.setdefault("heartbeats", heartbeats)
    readers.setdefault("scheduler", scheduler)
    readers.setdefault("identity_cache", IdentityRoster(connect=connect))
    readers.setdefault("diagnostics_batch", lambda roster, end: jobs.get_sysinfo(diagnostic=True, roster=roster,
        deadline=end, timeout=5, connect=connect,
        revision=revision))
    return readers


def acquire_snapshot(actor, *, deadline, readers=None, budget=None, now=None, node_ids=None, extra_series=None, wall_clock=None):
    """Shared raw acquisition for snapshots/coverage, never a public-tool loop."""
    from mojo.apps.account.services.health_evidence import CycleBudget
    from mojo.helpers.aws.cloudwatch import CloudWatchHelper
    from mojo.deploy.provision.spec import names
    phase_now = lambda: now if now is not None else (wall_clock or (lambda: datetime.now(timezone.utc)))()
    budget = budget or CycleBudget(deadline)
    readers = readers or {}
    revision = readers.get("revision", settings.get_static("MOJO_FLEET_CONFIG_REVISION", None)) if source_grants(actor)["platform"] else None
    readers = _snapshot_readers(deadline, budget, readers, revision=revision)
    grants, expected, rows, sources, runners, overflow, inventory = readers.get("collected") or _collect(actor, deadline, readers)
    checked = _state("unavailable", "diagnostic_channel_membership_missing", results=[])
    if grants["jobs"]:
        try:
            if not grants["platform"] and not runners:
                runners = readers["heartbeats"](deadline)
            # The already bounded channel registry is the transport authority.
            # All checked proofs must be reconciled together, including hosts
            # whose custom logical node id has no hostname alias yet.
            chosen = [row for row in rows if row["expected"] and (node_ids is None or row["node_id"] in node_ids)]
            proved_target = node_ids is not None and chosen and all(row.get("hostname") and row.get("identity_status") == "resolved" for row in chosen)
            hosts = {row.get("hostname") for row in chosen}
            roster = [runner for runner in runners if not proved_target or runner.get("hostname", "").lower() in hosts][:128]
            if roster:
                budget.take("checked_broadcasts")
                budget.take("checked_targets", len(roster))
                checked = readers["diagnostics_batch"](roster, min(deadline, time.monotonic()+5))
            valid = checked.get("status") in ("verified", "partial") and not checked.get("anomalies")
            receipts = [item for item in checked.get("results", []) if valid and item.get("status") == "success"
                and (item.get("result") or {}).get("hostname") == item.get("host")
                and any(r.get("hostname", "").lower() == item.get("host") and r.get("started") == item.get("started") for r in roster)]
            prior = [{"host": row.get("hostname"), "started": row["proof_started"], "proof": {
                "node_id": row["node_id"], "observed_at": row["proof_observed_at"]}}
                for row in rows if row.get("proof_started") and row.get("proof_observed_at")]
            resolved = reconcile(expected["declarations"], inventory, runners, prior + [{
                "host": receipt["host"], "started": receipt.get("started"), "proof": (receipt.get("result") or {}).get("proof") or {}}
                for receipt in receipts], now=phase_now(), heartbeat_ttl=readers.get("heartbeat_ttl", max(1, int(settings.get_static("JOBS_RUNNER_HEARTBEAT_SEC", 5)))*3))
            for previous in rows:
                if previous.get("identity_status") == "ambiguous" and _fresh(previous.get("proof_observed_at"), phase_now()):
                    for row in resolved:
                        if row["node_id"] == previous["node_id"]:
                            row.update(identity_status="ambiguous", health="unknown", aliases=previous.get("aliases", []), proof_observed_at=previous["proof_observed_at"])
                            row["reasons"] = sorted(set(row["reasons"] + ["node_proof_ambiguous"]))
            overflow = overflow or len(resolved) > 128
            rows = sorted(sorted(resolved, key=lambda row: not row["expected"])[:128], key=lambda row: row["node_id"])
            try:
                readers["identity_cache"].write(grants, _revision(rows), rows, deadline, scope=roster_scope(expected))
            except Exception:
                sources["identity_cache"] = _state("unknown", "identity_alias_persistence_unavailable")
        except Exception:
            checked = _state("unavailable", "checked_transport_unavailable", results=[])
    selected = [row for row in rows if row["expected"] and (node_ids is None or row["node_id"] in node_ids)][:128]
    # Role/readiness observations below do not change the reconciled identity
    # revision used by coverage cursors. Declaration changes still fence it.
    roster_revision = _revision(rows)
    evidence = {}
    for row in selected:
        current = _state("permission_denied") if not grants["jobs"] else _state("unavailable", checked.get("reason") or "checked_reply_missing")
        if row["identity_status"] == "ambiguous":
            current = _state("unknown", "identity_ambiguous")
        elif grants["jobs"] and checked.get("status") in ("verified", "partial") and not checked.get("anomalies"):
            matching = [item for item in checked.get("results", []) if item.get("host") == row.get("hostname") and item.get("status") == "success"
                and any(r.get("hostname", "").lower() == item.get("host") and r.get("started") == item.get("started") for r in runners)]
            if len(matching) == 1:
                payload = matching[0].get("result") or {}
                proof = payload.get("proof") or {}
                if payload.get("hostname") == row.get("hostname") and (not proof.get("node_id") or proof["node_id"] == row["node_id"]):
                    current = _project_current(payload.get("current"))
        evidence[row["node_id"]] = {"current": current, "trends": _state("permission_denied"),
            "services": _services(row, sources, checked=checked if grants["jobs"] else None,
                runners=runners, revision=revision, now=phase_now(), heartbeat_ttl=readers.get("heartbeat_ttl"))}
    cloud = None
    if grants["aws"]:
        try:
            spec = expected.get("spec")
            namespace = names(spec)["metrics_namespace"] if spec is not None else "CWAgent"
            mounts = {node_id: [row["path"] for row in (detail["current"].get("filesystems") or {}).get("mounts", []) + (detail["current"].get("filesystems") or {}).get("missing", [])] for node_id, detail in evidence.items()}
            cloud_rows = [row for row in selected if row["identity_status"] != "ambiguous"]
            cloud = readers.get("recent", lambda rows, namespace, mounts, deadline: CloudWatchHelper(
                region=spec.region if spec else None, static_configuration=True).get_recent_evidence(rows, namespace, mounts,
                    deadline=deadline, budget=budget, now=phase_now(), extra_series=extra_series, maximum_operations=readers.get("metric_operations", 32)))(cloud_rows, namespace, mounts, deadline)
            for row in selected:
                evidence[row["node_id"]]["trends"] = (_state("unknown", "identity_ambiguous") if row["identity_status"] == "ambiguous" else
                    (cloud.get("nodes") or {}).get(row["node_id"]) or _state("unavailable", "not_collected_budget"))
        except Exception:
            for detail in evidence.values():
                detail["trends"] = _state("unavailable", "cloudwatch_unavailable")
    roster_complete = _envelope(expected, rows, sources, overflow)["coverage"]["complete"]
    return {"grants": grants, "expected": expected, "rows": rows, "sources": sources,
            "runners": runners, "overflow": overflow, "inventory": inventory, "evidence": evidence,
            "cloud": cloud, "roster_complete": roster_complete, "roster_revision": roster_revision, "roster_scope": roster_scope(expected)}


def _coverage_slot(raw, *, source, required=True, configuration="unknown", provenance=None, now=None, freshness=20):
    from mojo.apps.account.services.health_evidence import parse_time, valid_time, utcnow
    now, raw = now or utcnow(), raw or {}
    at = valid_time(raw.get("observed_at") or raw.get("last_sample_at"), now) if raw.get("status") in ("ok", "stale") else None
    age = (now - at).total_seconds() if at else None
    status, reason = raw.get("status", "unknown"), raw.get("reason")
    covered = ("permission_denied" if status == "permission_denied" else "unsupported" if status == "unsupported" else
               "not_applicable" if status == "not_applicable" else "not_collected_budget" if reason in ("not_collected_budget", "metric_budget_exceeded") else
               "permission_denied" if reason == "provider_denied" else "unknown" if at is None and status == "unknown" else
               "missing" if at is None and reason in ("no_series", "no_datapoints", "no_sample_in_window", "configured_mount_absent", "checked_reply_missing") else
               "collector_error" if at is None and source == "runtime" and status == "unavailable" else
               "provider_error" if at is None and source == "cloudwatch" and status == "unavailable" else
               "unknown" if at is None else "incomplete" if raw.get("truncated") else "stale" if age > freshness else "covered")
    if configuration == "not_configured" and at is None:
        covered = "not_configured"
    value = raw.get("value")
    if value is None:
        value = {key: raw[key] for key in ("percent", "total", "used", "free", "available", "scheduler_active", "healthy") if key in raw} or None
    return {"required": required, "configuration_status": configuration, "configuration_provenance": provenance,
            "status": status, "reason": reason, "coverage_status": covered,
            "last_real_sample_at": at.isoformat() if at else None, "collected_at": _now(), "truncated": bool(raw.get("truncated")),
            "age_seconds": max(0, age) if age is not None else None, "freshness_limit_seconds": freshness,
            "configuration_drift": bool(at is not None and configuration == "not_configured"),
            "value": value, "unit": raw.get("unit"), "source_identity": {"source": source,
                **{key: raw[key] for key in ("namespace", "dimensions", "metric", "period_seconds", "aggregation", "start", "end") if key in raw}}}


def project_monitoring_coverage(acquired, node_id, *, now=None):
    """Pure returned-node requirements/evidence projection."""
    from mojo.apps.account.services.health_evidence import utcnow
    now = now or utcnow()
    row = next(row for row in acquired["rows"] if row["node_id"] == node_id)
    detail = acquired["evidence"][node_id]
    current, trends = detail["current"], detail["trends"]
    declared = acquired["expected"].get("spec") is not None
    measurements = []
    def measurement(key, runtime, cloud=None, *, mount=None, cloud_config=None, required=True):
        runtime = runtime or current if current.get("status") == "permission_denied" else runtime
        slots = {"runtime": _coverage_slot(runtime, source="runtime", required=required,
            configuration="declared" if runtime and runtime.get("status") == "ok" else "unknown", now=now)}
        if cloud is not None:
            cloud_config = cloud_config or ("declared" if declared else "unknown")
            slots["cloudwatch"] = _coverage_slot(cloud, source="cloudwatch", required=required,
                configuration=cloud_config, provenance="selected_provision_recipe" if declared else "fallback_candidate",
                now=now, freshness=3 * (cloud.get("period_seconds") or (60 if key == "memory" else 300)))
        measurements.append({"key": key, "node_id": node_id, "required": required,
            "role_provenance": row["membership_sources"], "mount": mount, "sources": slots})
    cloud_denied = _state("permission_denied")
    for family in ("cpu", "memory"):
        measurement(family, current.get(family), trends.get(family) or (cloud_denied if not acquired["grants"]["aws"] else _state("unknown", "instance_unresolved")))
    filesystems = current.get("filesystems") or {}
    mounts = {mount["path"]: mount for mount in filesystems.get("mounts", []) + filesystems.get("missing", [])}
    mounts.setdefault("/", _state("unknown", "filesystem_inventory_unknown"))
    for path, mount in sorted(mounts.items()):
        cloud = next((item for item in trends.get("filesystems", []) if item.get("path") == path), None)
        measurement("filesystem:" + path, mount, cloud or (cloud_denied if not acquired["grants"]["aws"] else _state("unavailable", "no_series")), mount=path,
                    cloud_config="declared" if declared and path == "/" else "not_configured" if declared else "unknown")
        measurement("inodes:" + path, mount.get("inodes") or _state("unknown", "inodes_unavailable"), mount=path)
    measurement("filesystem_inventory", {**filesystems, "truncated": bool(filesystems.get("omitted_count") or filesystems.get("omitted_missing_count"))})
    heartbeat = {"status": "ok" if row.get("last_heartbeat") else "unknown", "observed_at": row.get("last_heartbeat"), "value": bool(row.get("runner_id"))}
    measurements.append({"key": "runner", "node_id": node_id, "required": True, "role_provenance": row["membership_sources"], "mount": None,
        "sources": {"platform": _coverage_slot(heartbeat if acquired["grants"]["platform"] else _state("permission_denied"), source="heartbeat", now=now,
            freshness=max(1, int(settings.get_static("JOBS_RUNNER_HEARTBEAT_SEC", 5))) * 3)}})
    scheduler = acquired["sources"].get("scheduler", {})
    detector = scheduler.get("detector") or {"status": scheduler.get("status", "unknown"), "reason": "detector_evidence_unavailable"}
    measured = {**(detector.get("scheduler") or {}), "observed_at": detector.get("observed_at")}
    if detector.get("status") != "ok":
        measured.update(status=detector.get("status", "unknown"), reason=detector.get("reason"))
    measurements.append({"key": "scheduler", "node_id": node_id, "required": True, "mount": None,
        "sources": {"platform": _coverage_slot(measured, source="independent_detector", now=now,
            configuration="declared" if detector.get("detector_id") else "unknown", freshness=420)},
        "scope": "installation", "reason": measured.get("reason")})
    required = row.get("request_service_required")
    measurements.append({"key": "request_service", "node_id": node_id, "required": required,
        "sources": {"runtime": _coverage_slot(detail["services"]["request_service"], source="runtime", required=required is not False, now=now)}})
    stats = {"scope": "returned_node", "expected_total": len(measurements) if required is not None else None,
             "configured_total": 0, "observed_total": 0, "fresh_total": 0, "gap_total": 0,
             "unknown_total": 0, "omitted_total": 0, "complete": False, "status_counts": {}}
    for item in measurements:
        values = list(item["sources"].values())
        stats["configured_total"] += int(any(slot["configuration_status"] == "declared" for slot in values))
        stats["observed_total"] += int(any(slot["last_real_sample_at"] for slot in values))
        stats["fresh_total"] += int(all(slot["coverage_status"] in ("covered", "not_applicable") for slot in values))
        for slot in values:
            status = slot["coverage_status"]
            stats["status_counts"][status] = stats["status_counts"].get(status, 0) + 1
        stats["gap_total"] += int(any(slot["coverage_status"] not in ("covered", "not_applicable") for slot in values))
        stats["unknown_total"] += int(any(slot["coverage_status"] == "unknown" for slot in values))
    stats["complete"] = bool(stats["expected_total"] and not stats["gap_total"])
    return {"measurements": measurements, "measurement_coverage": stats,
            "agent_installation": _state("unknown", "no_current_installation_proof")}


def coverage(actor, node_id=None, cursor=None, *, readers=None, deadline=None, now=None):
    """Signed one-expected-node pages over the same bounded raw acquisition."""
    if node_id is not None and cursor is not None:
        raise FleetHealthError("invalid_request")
    if node_id is not None and (not isinstance(node_id, str) or not NODE_RE.fullmatch(node_id)):
        raise FleetHealthError("invalid_node_id")
    continuation = None
    if cursor is not None:
        if not isinstance(cursor, str) or not cursor or len(cursor) > 4096:
            raise FleetHealthError("invalid_cursor")
        try:
            continuation = signing.loads(cursor, salt="mojo.monitoring.coverage.v2", max_age=3600)
            if not isinstance(continuation, dict) or continuation.get("profile") != 2:
                raise ValueError()
        except (ValueError, TypeError, signing.BadSignature):
            raise FleetHealthError("invalid_cursor") from None
    deadline = min(deadline or time.monotonic() + 20, time.monotonic() + 20)
    from mojo.apps.account.services.health_evidence import CycleBudget
    budget = CycleBudget(deadline, limits={"provider_operations": 32, "cloudwatch_series": 10})
    readers = {**(readers or {}), "metric_operations": 12}
    selected_revision = readers.get("revision", settings.get_static("MOJO_FLEET_CONFIG_REVISION", None)) if source_grants(actor)["platform"] else None
    readers = _snapshot_readers(deadline, budget, readers, revision=selected_revision)
    # Resolve the roster once, then pass this exact acquisition into the raw
    # detail phase. No public node/report call and no second inventory sweep.
    collected = _collect(actor, deadline, readers)
    grants, expected, rows, sources, runners, overflow, inventory = collected
    revision = _revision(rows)
    expected_ids = [row["node_id"] for row in rows if row["expected"]]
    if continuation:
        if continuation.get("revision") != revision or continuation.get("scope") != roster_scope(expected):
            raise FleetHealthError("roster_changed")
        if continuation.get("last") not in expected_ids:
            raise FleetHealthError("invalid_cursor")
        position = expected_ids.index(continuation["last"]) + 1
    else:
        position = expected_ids.index(node_id) if node_id in expected_ids else 0
    if node_id is not None and node_id not in expected_ids:
        raise FleetHealthError("unknown_resource")
    if position >= len(expected_ids):
        raise FleetHealthError("unknown_resource")
    selected_id = expected_ids[position]
    if next(row for row in rows if row["node_id"] == selected_id)["identity_status"] == "ambiguous":
        raise FleetHealthError("identity_ambiguous")
    # Private pre-collected seam prevents repeating _collect below.
    acquired = acquire_snapshot(actor, deadline=deadline, readers={**readers, "collected": collected}, node_ids=[selected_id], now=now, budget=budget)
    if selected_id not in acquired["evidence"]:
        raise FleetHealthError("roster_changed")
    if acquired["roster_revision"] != revision:
        raise FleetHealthError("roster_changed")
    projected = project_monitoring_coverage(acquired, selected_id, now=now)
    result = _envelope(expected, rows, sources, overflow)
    result.update(health="unknown", health_scope="monitoring_coverage", node=next(row for row in acquired["rows"] if row["node_id"] == selected_id), **projected)
    more = position + 1 < len(expected_ids)
    result["coverage"].update(returned=1, has_more=more,
        next_cursor=signing.dumps({"revision": revision, "scope": roster_scope(expected), "profile": 2, "last": selected_id}, salt="mojo.monitoring.coverage.v2", compress=True) if more else None,
        complete=len(expected_ids) == 1 and projected["measurement_coverage"]["complete"] and result["coverage"]["complete"])
    while len(json.dumps(result, separators=(",", ":")).encode()) > MAX_BYTES and result["measurements"]:
        result["measurements"].pop()
        result["measurement_coverage"]["omitted_total"] += 1
        result["measurement_coverage"]["complete"] = False
        result["coverage"]["complete"] = False
        result.update(node_incomplete=True, truncated=True)
    if not result["coverage"]["complete"]:
        result["collection_status"] = "partial"
    return result
