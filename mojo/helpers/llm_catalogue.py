"""Bounded, credential-scoped model catalogue mechanics (no routing authority)."""

from collections import OrderedDict
from datetime import datetime, timezone
import json
import math
import secrets
import time

FRESH_SECONDS = 86400
MAX_STALE_SECONDS = 7 * 86400
SAFE_ERRORS = frozenset({
    'provider_authentication', 'provider_billing_exhausted', 'provider_failed',
    'provider_rate_limited', 'provider_rejected', 'provider_timeout',
    'provider_unavailable', 'provider_unsupported', 'policy_invalid', 'policy_mixed',
    'route_missing', 'credential_missing', 'credential_mismatch', 'emergency_stopped',
    'capability_unsupported', 'loop_limit', 'budget_exhausted', 'concurrency_exhausted',
    'breaker_open', 'breaker_half_open', 'permit_unavailable', 'control_state_unknown',
    'ledger_unavailable', 'ledger_persistence_unknown', 'catalogue_incomplete',
    'catalogue_unavailable', 'refresh_in_progress', 'refresh_throttled',
    'safety_unavailable', 'no_compatible_model',
})
RELEASE_LOCK = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


def model_id(value):
    return isinstance(value, str) and 0 < len(value) <= 128 \
        and value == value.strip() and not any(c.isspace() or ord(c) < 32 for c in value)


def release_time(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and len(value) <= 64:
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return None
    try:
        stamp = parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return None
    # The provider documents epoch as an unknown release date.
    return stamp if stamp > 0 and math.isfinite(stamp) else None


def normalize_models(rows):
    """Bound and copy known metadata; conflicting duplicate IDs are unusable."""
    if not isinstance(rows, list) or len(rows) > 500:
        raise ValueError('catalogue_incomplete')
    found, conflicts = {}, set()
    for row in rows:
        if not isinstance(row, dict) or not model_id(row.get('id')):
            continue
        cap = row.get('capabilities')
        image = cap.get('image_input') if isinstance(cap, dict) else None
        created = row.get('created_at')
        stamp = release_time(created)
        tokens = row.get('max_tokens')
        entry = {
            'id': row['id'],
            'display_name': row.get('display_name')[:200]
                if isinstance(row.get('display_name'), str) else row['id'],
            'line': row.get('line') if isinstance(row.get('line'), str)
                and len(row['line']) <= 32 else None,
            'lifecycle': row.get('lifecycle')
                if isinstance(row.get('lifecycle'), str)
                and len(row['lifecycle']) <= 32 else None,
            'created_at': iso(stamp),
            'max_tokens': tokens if type(tokens) is int and 0 < tokens <= 2147483647 else None,
            'capabilities': {'image_input': {'supported':
                isinstance(image, dict) and image.get('supported') is True}},
        }
        prior = found.get(entry['id'])
        if prior is not None and prior != entry:
            conflicts.add(entry['id'])
        found[entry['id']] = entry
    return [entry for key, entry in found.items() if key not in conflicts]


def select_sonnet(models, output_cap, images=False):
    candidates = []
    for entry in normalize_models(models):
        stamp = release_time(entry['created_at'])
        if entry['line'] != 'sonnet' or entry['lifecycle'] != 'active' or stamp is None:
            continue
        if entry['max_tokens'] is None or entry['max_tokens'] < output_cap:
            continue
        if images and entry['capabilities']['image_input']['supported'] is not True:
            continue
        candidates.append((stamp, entry['id']))
    return max(candidates)[1] if candidates else ''


def iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat().replace('+00:00', 'Z') \
        if stamp is not None else None


def safe_code(error):
    code = getattr(error, 'code', '')
    if not code and isinstance(error, ValueError) and str(error) == 'catalogue_incomplete':
        code = 'catalogue_incomplete'
    return code if code in SAFE_ERRORS else 'catalogue_unavailable'


class Catalogue:
    """Redis-coordinated refresh, with bounded local last-good read fallback.

    All dependencies can be test-owned. A cache/control error never authorizes
    a provider request. Failed attempts retain the original success deadline.
    """

    def __init__(self, clock=None):
        self.clock = clock or time.time
        self.local = OrderedDict()

    def _remember(self, key, entry):
        self.local[key] = entry
        self.local.move_to_end(key)
        while len(self.local) > 32:
            self.local.popitem(last=False)

    def _read(self, redis, key, now):
        try:
            raw = redis.get(key)
            if raw:
                if len(raw) > 512000:
                    raise ValueError("invalid cache")
                entry = json.loads(raw)
                if not isinstance(entry, dict) or set(entry) != {
                        'models', 'fetched_at', 'attempted_at', 'error_code'}:
                    raise ValueError('invalid cache')
                for field in ('fetched_at', 'attempted_at'):
                    value = entry[field]
                    if value is not None and (type(value) not in (int, float)
                            or not math.isfinite(value) or value <= 0 or value > now):
                        raise ValueError('invalid cache')
                if entry['error_code'] and entry['error_code'] not in SAFE_ERRORS:
                    raise ValueError('invalid cache')
                entry['models'] = normalize_models(entry['models'])
                if entry['fetched_at'] is None and entry['models']:
                    raise ValueError('invalid cache')
                self._remember(key, entry)
                return entry
        except Exception:
            pass
        return self.local.get(key) or {
            'models': [], 'fetched_at': None, 'attempted_at': None, 'error_code': ''}

    def _save(self, redis, key, entry):
        self._remember(key, entry)
        redis.set(key, json.dumps(entry, separators=(',', ':')), ex=MAX_STALE_SECONDS + 86400)

    def state(self, redis, key, loader=None, refresh=False, force=False):
        now = self.clock()
        entry = self._read(redis, key, now)
        outcome = {'attempted': False, 'ok': None, 'code': ''}
        fetched = entry['fetched_at']
        fresh = fetched is not None and now - fetched < FRESH_SECONDS
        if refresh and (force or not fresh):
            token = secrets.token_hex(16)
            locked = False
            try:
                locked = bool(redis.set(key + ':lock', token, nx=True, ex=30))
                if not locked:
                    outcome = {'attempted': False, 'ok': False, 'code': 'refresh_in_progress'}
                elif not redis.set(key + ':cooldown', token, nx=True, ex=60):
                    outcome = {'attempted': False, 'ok': False, 'code': 'refresh_throttled'}
                else:
                    outcome = {'attempted': True, 'ok': False, 'code': ''}
                    entry = dict(entry, attempted_at=now)
                    try:
                        models = normalize_models(loader())
                        entry = dict(models=models, fetched_at=self.clock(),
                                     attempted_at=now, error_code='')
                        outcome['ok'] = True
                    except Exception as error:
                        entry['error_code'] = safe_code(error)
                        outcome['code'] = entry['error_code']
                    self._save(redis, key, entry)
            except Exception:
                outcome['ok'] = False
                outcome['code'] = 'control_state_unknown'
            finally:
                if locked:
                    try:
                        redis.eval(RELEASE_LOCK, 1, key + ':lock', token)
                    except Exception:
                        pass
        now = self.clock()
        fetched = entry['fetched_at']
        age = now - fetched if fetched is not None else None
        usable = age is not None and 0 <= age < MAX_STALE_SECONDS
        status = 'fresh' if usable and age < FRESH_SECONDS else 'stale' if usable else 'unavailable'
        return {
            'models': entry['models'] if usable else [],
            'discovery': {
                'status': status, 'fetched_at': iso(fetched),
                'attempted_at': iso(entry['attempted_at']),
                'expires_at': iso(fetched + FRESH_SECONDS) if fetched is not None else None,
                'max_stale_at': iso(fetched + MAX_STALE_SECONDS) if fetched is not None else None,
                'error_code': entry['error_code'],
            },
            'refresh': outcome,
        }
