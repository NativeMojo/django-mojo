"""Bounded OS-cron detector; cached reads never collect or renew evidence."""
import json
import re
import socket
import time
from contextlib import nullcontext
from datetime import datetime, timezone

from mojo.helpers.settings import settings
from mojo.apps.jobs.services.liveness import (
    PROGRESS_TTL, lease_digest, progress_key, progress_sample)

DETECTOR_ID = "mojo.apps.incident.cronjobs.check_system_health"
DETECTOR_MAX_AGE = 420
DETECTOR_TTL = 900
BASELINE_TTL = 86400
MAX_HOSTS = 128
MAX_CHANNELS = 32
MAX_BYTES = 128 * 1024
HOST = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,252}$")
CHANNEL = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_DEFAULT = object()


def _key(name):
    from mojo.apps.jobs.keys import JobKeys
    return f"{JobKeys().prefix}:health:{name}"


def _time(value):
    try:
        result = datetime.fromisoformat(value)
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (ValueError, TypeError):
        return None


def _json(raw, default=None):
    if raw is None:
        return default
    if not isinstance(raw, (str, bytes)) or len(raw) > MAX_BYTES:
        raise ValueError("liveness_evidence_invalid")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("liveness_evidence_invalid")
    return value


def _store(client, key, value, ttl):
    raw = json.dumps(value, sort_keys=True)
    if len(raw.encode()) > MAX_BYTES:
        raise ValueError("liveness_evidence_overflow")
    client.set(key, raw, ex=ttl)


def _state(status="unavailable", reason="source_unavailable", healthy=None, **kwargs):
    return dict(status=status, reason=reason, healthy=healthy, **kwargs)


def _expected(deadline):
    from mojo.apps.account.models import Setting
    from mojo.apps.account.services import health_evidence as he, system_settings
    from mojo.apps.aws.services import fleet_health
    # Same existing fleet topology, read through the bounded primary transport.
    with he.primary_cursor(deadline, max_queries=1, transactional=False) as cursor:
        cursor.execute('SELECT value FROM "%s" WHERE group_id IS NULL AND key=%%s' % Setting._meta.db_table,
                       [system_settings.EXPECTED_EDGE_TOPOLOGY])
        row = cursor.fetchone()
    raw = row[0] if row else None
    topology = json.loads(raw) if isinstance(raw, str) else raw
    return fleet_health._read_expected({"platform": True, "aws": True, "jobs": True}, deadline,
                                      topology_reader=lambda key: topology)


def _expected_hosts(expected, runners, client, now, deadline):
    """Use the shared checked alias cache, retaining established intent only."""
    from mojo.apps.aws.services import fleet_health as fh
    if not expected.get("complete"):
        raise ValueError("expected_roster_unavailable")
    declarations = expected["declarations"]
    if not expected.get("configured"):
        return None, []
    physical = {row["hostname"] for row in declarations if row.get("hostname")}
    logical = {row["node_id"] for row in declarations if not row.get("hostname")}
    if not logical:
        return physical, []
    grants, scope = {"platform": True, "aws": True, "jobs": True}, fh.roster_scope(expected)
    prior = _json(client.get(_key("expected_aliases")), {})
    mappings = prior.get("mappings", {}) if prior.get("scope") == scope else {}
    if not isinstance(mappings, dict) or len(mappings) > MAX_HOSTS:
        raise ValueError("expected_alias_invalid")
    cache = fh.IdentityRoster(connect=lambda **kwargs: nullcontext(client), clock=lambda: now.timestamp())
    record = cache.read(grants, deadline, scope=scope)
    if record is None:
        raw = _json(client.get(cache._key(grants, scope)), {})
        # Real Redis drops expired keys. Also recognize an old retained record
        # as expired, without treating malformed current data as a cache miss.
        expiry = raw.get("expires_at")
        if raw and (type(expiry) not in (int, float) or not expiry <= now.timestamp()):
            raise ValueError("expected_alias_invalid")
    identities = {row["node_id"]: row for row in (record or {}).get("rows", [])}
    proofs = [{"host": row["hostname"], "started": row["proof_started"],
               "proof": {"node_id": row["node_id"], "observed_at": row["proof_observed_at"]}}
              for row in identities.values() if row.get("hostname") and row.get("proof_started") and row.get("proof_observed_at")]
    reconciled = {row["node_id"]: row for row in fh.reconcile(declarations, runners=runners,
        proofs=proofs, now=now, heartbeat_ttl=max(1, int(settings.get_static("JOBS_RUNNER_HEARTBEAT_SEC", 5))) * 3)}
    retained, unresolved = {}, []
    for node in sorted(logical):
        identity, joined = identities.get(node, {}), reconciled.get(node, {})
        candidate = mappings.get(node, {})
        seen = _time(candidate.get("last_seen")) if isinstance(candidate, dict) else None
        if (seen is None or not 0 <= (now-seen).total_seconds() < BASELINE_TTL
                or candidate.get("status") not in ("resolved", "ambiguous")
                or candidate.get("hostname") is not None and not HOST.fullmatch(candidate["hostname"])
                or candidate.get("status") == "resolved" and not isinstance(candidate.get("started"), str)):
            candidate = {}
        current = fh._fresh(identity.get("proof_observed_at"), now)
        if current and identity.get("identity_status") == "ambiguous":
            candidate = {"status": "ambiguous", "hostname": None, "last_seen": identity["proof_observed_at"]}
        elif (current and identity.get("identity_status") == "resolved" and identity.get("hostname")
                and identity.get("proof_started") and (
                    joined.get("identity_status") == "resolved" and joined.get("proof_started")
                    or not any(row.get("hostname", "").lower() == identity["hostname"] for row in runners))):
            candidate = {"status": "resolved", "hostname": identity["hostname"],
                         "started": identity["proof_started"], "last_seen": identity["proof_observed_at"]}
        if candidate:
            retained[node] = candidate
        if candidate.get("status") == "resolved" and candidate.get("hostname"):
            physical.add(candidate["hostname"])
            current_host = [row for row in runners if row.get("hostname", "").lower() == candidate["hostname"]]
            if current_host and not any(row.get("started") == candidate["started"] for row in current_host):
                unresolved.append(node)
        else:
            unresolved.append(node)
    _store(client, _key("expected_aliases"), {"scope": scope, "mappings": retained}, BASELINE_TTL)
    return physical, unresolved


def _runners(client, roster, expected, channels, now, deadline):
    declaration = expected if isinstance(expected, dict) else None
    if declaration is None and expected is not None and (not isinstance(expected, (list, tuple))
            or len(expected) > MAX_HOSTS or len(set(expected)) != len(expected)
            or any(not isinstance(host, str) or not HOST.fullmatch(host.lower()) for host in expected)):
        raise ValueError("expected_roster_invalid")
    if declaration is None and expected is not None:
        clean = {host.lower() for host in expected}
        if len(clean) != len(expected):
            raise ValueError("expected_roster_invalid")
        expected = clean
    baseline = _json(client.get(_key("expected")), {})
    if len(baseline) > MAX_HOSTS:
        raise ValueError("expected_roster_overflow")
    live, observed = {}, {}
    for channel in channels:
        for row in roster(channel, limit=MAX_HOSTS, timeout=max(.05, deadline-time.monotonic()), client=client):
            host = row.get("hostname")
            if not isinstance(host, str) or not HOST.fullmatch(host.lower()):
                raise ValueError("runner_roster_invalid")
            live.setdefault(host.lower(), set()).add(channel)
            observed[row["runner_id"]] = row
            if len(live) > MAX_HOSTS:
                raise ValueError("runner_roster_overflow")
    unresolved = []
    if declaration is not None:
        expected, unresolved = _expected_hosts(declaration, list(observed.values()), client, now, deadline)
    retained = {}
    for host, prior in baseline.items():
        if not HOST.fullmatch(host) or not isinstance(prior, dict):
            raise ValueError("expected_roster_invalid")
        seen = _time(prior.get("last_seen"))
        roles = prior.get("channels")
        if (seen is None or not isinstance(roles, list) or len(roles) > MAX_CHANNELS
                or any(not isinstance(role, str) or not CHANNEL.fullmatch(role) for role in roles)):
            raise ValueError("expected_roster_invalid")
        if (0 <= (now-seen).total_seconds() < BASELINE_TTL
                and (expected is None or host in expected)):
            retained[host] = {"channels": sorted(set(roles) & set(channels)), "last_seen": prior["last_seen"]}
    # Host/channel identity survives PID replacement. Only current declared
    # channel removal retires a role: another responding engine on the same
    # host must not hide the loss of one of its sibling engines.
    for host, roles in live.items():
        if expected is None or host in expected:
            roles = roles | set(retained.get(host, {}).get("channels", []))
            retained[host] = {"channels": sorted(roles), "last_seen": now.isoformat()}
    required = {host: set(prior["channels"]) for host, prior in retained.items()}
    fleet_channel = settings.get_static("ADMIN_FLEET_CONFIG_CHANNEL", "edge")
    if expected is not None:
        for host in expected:
            required.setdefault(host, set()).add(fleet_channel)
    missing = [{"hostname": host, "channels": sorted(roles-live.get(host, set()))}
               for host, roles in sorted(required.items()) if roles-live.get(host, set())]
    absent_channels = [channel for channel in channels if not any(channel in roles for roles in live.values())]
    _store(client, _key("expected"), retained, BASELINE_TTL)
    healthy = False if missing or absent_channels and not unresolved else None if unresolved else True
    return _state("unknown" if healthy is None else "ok", "expected_alias_unresolved" if unresolved else None, healthy,
                  missing=missing, missing_channels=absent_channels, live_count=len(live),
                  **({"unresolved_nodes": unresolved} if unresolved else {}))


def _scheduler(client, now):
    from mojo.apps.jobs.keys import JobKeys
    lease = client.get(JobKeys().scheduler_lock())
    if not lease:
        return _state("ok", "scheduler_missing", False)
    progress = _json(client.get(progress_key()), {})
    observed = _time(progress.get("observed_at"))
    if not progress:
        return _state("ok", "scheduler_progress_missing", False)
    if (progress.get("version") != 1 or observed is None
            or not isinstance(progress.get("scheduler_id"), str)
            or len(progress["scheduler_id"]) > 256):
        return _state(reason="scheduler_progress_invalid")
    if progress.get("lease") != lease_digest(lease):
        return _state("unknown", "scheduler_leader_changed")
    age = (now-observed).total_seconds()
    if age < -5:
        return _state(reason="scheduler_progress_invalid")
    return _state("ok", None if age < PROGRESS_TTL else "scheduler_progress_stale",
                  age < PROGRESS_TTL, scheduler_id=progress["scheduler_id"],
                  progress_observed_at=observed.isoformat())


def _report(result, previous, report_fn):
    if report_fn is None:
        return
    for name in ("runners", "scheduler"):
        state = result[name]
        if state["healthy"] is True:
            continue
        prior = (previous or {}).get(name)
        if prior == state:
            continue
        category = "runner" if name == "runners" else "scheduler"
        missing = state.get("missing") or []
        report_fn(json.dumps(state, sort_keys=True),
                  title=("Runner down" if name == "runners" and state["healthy"] is False
                         else "Scheduler missing" if name == "scheduler" and state["healthy"] is False
                         else f"Health {category} evidence unavailable"),
                  category=f"system:health:{category}", level=10, scope="system",
                  hostname=missing[0]["hostname"] if len(missing) == 1 else "fleet")


def collect(*, client=None, roster=None, expected=_DEFAULT, channels=None, now=None, report_fn=None, deadline=None):
    """One bounded read/write pass, invoked directly by OS cron, never by jobs."""
    from mojo.apps.account.services import health_evidence as he
    now = now or datetime.now(timezone.utc)
    deadline = deadline or time.monotonic()+4
    if client is None:
        try:
            budget = he.CycleBudget(deadline, limits={"redis_commands": 256})
            with he.bounded_redis_connection(budget=budget, deadline=deadline, max_connections=2) as bounded:
                return collect(client=bounded, roster=roster, expected=expected, channels=channels,
                               now=now, report_fn=report_fn, deadline=deadline)
        except Exception:
            result = {"status": "unavailable", "reason": "detector_transport_unavailable", "observed_at": None,
                      "detector_id": DETECTOR_ID, "runners": _state(), "scheduler": _state()}
            _report(result, None, report_fn)
            return result
    if roster is None:
        from mojo.apps.jobs.manager import get_manager
        roster = get_manager().get_runners_bounded
    result = {"version": 1, "status": "ok", "reason": None, "observed_at": now.isoformat(),
              "detector_id": DETECTOR_ID, "node": socket.gethostname()}
    previous = None
    try:
        previous = _json(client.get(_key("detector")))
        channels = channels if channels is not None else settings.get_static("JOBS_CHANNELS", None)
        if channels is None:
            from mojo.apps.jobs import DEFAULT_CHANNELS
            channels = DEFAULT_CHANNELS
        if (not isinstance(channels, (list, tuple)) or not channels or len(channels) > MAX_CHANNELS
                or any(not isinstance(channel, str) or not CHANNEL.fullmatch(channel) for channel in channels)):
            raise ValueError("expected_channels_invalid")
        channels = sorted(set(channels))
        if expected is _DEFAULT:
            expected = _expected(deadline)
        if expected is not None:
            channel = settings.get_static("ADMIN_FLEET_CONFIG_CHANNEL", "edge")
            if not isinstance(channel, str) or not CHANNEL.fullmatch(channel):
                raise ValueError("expected_channels_invalid")
            channels = sorted(set(channels) | {channel})
            if len(channels) > MAX_CHANNELS:
                raise ValueError("expected_channels_invalid")
        result["runners"] = _runners(client, roster, expected, channels, now, deadline)
    except Exception as exc:
        reason = str(exc) if str(exc) in ("runner_roster_invalid", "runner_roster_overflow", "runner_roster_timeout", "expected_roster_invalid", "expected_roster_overflow", "expected_channels_invalid") else "runner_roster_unavailable"
        result["runners"] = _state(reason=reason)
    try:
        result["scheduler"] = _scheduler(client, now)
    except Exception:
        result["scheduler"] = _state(reason="scheduler_read_unavailable")
    _store(client, _key("detector"), result, DETECTOR_TTL)
    _report(result, previous, report_fn)
    return result


def read_cached(*, client, now=None):
    now = now or datetime.now(timezone.utc)
    try:
        result = _json(client.get(_key("detector")))
        if result is None:
            return _state("unknown", "detector_evidence_unavailable", observed_at=None)
        observed = _time(result.get("observed_at"))
        if (result.get("version") != 1 or result.get("detector_id") != DETECTOR_ID
                or observed is None or (now-observed).total_seconds() < -30
                or any(not isinstance(result.get(name), dict) or not (type(result[name].get("healthy")) is bool or result[name].get("healthy") is None)
                       for name in ("runners", "scheduler"))):
            raise ValueError("detector_evidence_invalid")
        if (now-observed).total_seconds() >= DETECTOR_MAX_AGE:
            result.update(status="stale", reason="detector_evidence_expired")
        return result
    except Exception:
        return _state(reason="detector_evidence_unavailable", observed_at=None)
