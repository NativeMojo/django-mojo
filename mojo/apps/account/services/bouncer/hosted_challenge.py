"""Bounded, atomic state for hosted recovery, independent of the public SDK."""
import hashlib
import json
import math
import secrets
import time

CHALLENGE_TTL = 300
FORM_TTL = 1800
STATE_TTL = 1860
MAX_DESCRIPTORS = 8
ATTEMPTS = 3
COOLDOWN = 60
TOLERANCE = 8

# One key owns both the descriptors and the shared retry budget. Comparing the
# serialized value avoids lost updates across workers; the callback has no I/O.
_CAS = """
local old = redis.call('GET', KEYS[1]) or ''
if old ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
return 1
"""


class ChallengeUnavailable(Exception):
    pass


class ChallengeStore:
    def __init__(self, redis, host, muid, *, clock=None):
        self.redis = redis
        self.clock = clock or time.time
        digest = hashlib.sha256(f'{host}\n{muid}'.encode()).hexdigest()
        self.key = f'bouncer:hosted:{{{digest}}}'

    def _change(self, operation):
        for _ in range(8):
            raw = self.redis.get(self.key) or ''
            state = json.loads(raw) if raw else {'descriptors': {}, 'attempts': 0, 'cooldown': 0}
            result = operation(state, self.clock())
            encoded = json.dumps(state, separators=(',', ':'), sort_keys=True)
            if self.redis.eval(_CAS, 1, self.key, raw, encoded, STATE_TTL):
                return result
        raise ChallengeUnavailable('busy')

    def issue(self, purpose, group_uuid='', *, action='check', form=False):
        descriptor = secrets.token_urlsafe(24)

        def issue(state, now):
            entries = state['descriptors']
            for key in list(entries):
                if entries[key]['expires'] < now:
                    del entries[key]
            if len(entries) >= MAX_DESCRIPTORS:
                del entries[min(entries, key=lambda key: entries[key]['created'])]
            record = {'purpose': purpose, 'group_uuid': group_uuid, 'created': now,
                      'expires': now + (FORM_TTL if form else CHALLENGE_TTL),
                      'target': secrets.randbelow(51) + 25, 'stage': action,
                      'form': form, 'results': {}, 'issued': None}
            entries[descriptor] = record
            return {'descriptor': descriptor, **self._view(record, state, now)}
        return self._change(issue)

    def read(self, descriptor):
        raw = self.redis.get(self.key)
        state = json.loads(raw) if raw else {}
        record = state.get('descriptors', {}).get(descriptor)
        if not record or record['expires'] < self.clock():
            return None
        return record

    def _view(self, record, state, now):
        stage = record['stage']
        if stage not in ('decoy', 'recovery', 'granted') and state['cooldown'] > now:
            return {'next_action': 'cooldown', 'retry_after': math.ceil(state['cooldown'] - now)}
        action = 'check_cookie' if stage == 'granted' else stage
        result = {'next_action': action}
        if stage == 'slider':
            result.update(target=record['target'], tolerance=TOLERANCE,
                          attempts_remaining=max(0, ATTEMPTS - state['attempts']))
        if stage == 'granted':
            result['issued'] = record['issued']
        return result

    def complete(self, descriptor, operation, request_id, *, policy, answer=None):
        def complete(state, now):
            record = state['descriptors'].get(descriptor)
            if not record or record['expires'] < now:
                return {'next_action': 'recovery', 'reason': 'expired'}
            if record['form']:
                return {'next_action': 'recovery', 'reason': 'restart'}
            # A new restriction always wins, even over a cached success.
            if policy in ('decoy', 'recovery'):
                record['stage'] = policy
            if record['stage'] in ('decoy', 'recovery'):
                return self._view(record, state, now)
            if request_id in record['results']:
                return record['results'][request_id]
            if record['stage'] == 'granted':
                return self._view(record, state, now)
            if state['cooldown'] > now:
                return self._view(record, state, now)
            if state['cooldown'] or state.get('window_end', 0) < now:
                state.update(attempts=0, cooldown=0, window_end=now + CHALLENGE_TTL)
            if operation == 'check':
                if record['stage'] == 'slider' or policy == 'slider':
                    record['stage'] = 'slider'
                else:
                    record.update(stage='granted', issued=int(now))
                # Checks are idempotent in state; storing them would let reloads
                # crowd the bounded answer cache or replay an obsolete grant.
                return self._view(record, state, now)
            if record['stage'] != 'slider':
                return {'next_action': 'recovery', 'reason': 'restart'}
            if type(answer) not in (int, float) or not math.isfinite(answer) or not 0 <= answer <= 100:
                return {'next_action': 'error', 'reason': 'invalid'}
            if abs(answer - record['target']) <= TOLERANCE:
                record.update(stage='granted', issued=int(now))
            else:
                state['attempts'] += 1
                if state['attempts'] >= ATTEMPTS:
                    state['cooldown'] = now + COOLDOWN
            result = self._view(record, state, now)
            # No request can grow this map without spending an attempt or
            # completing the record. Clear only at a new, explicit budget.
            if len(record['results']) >= ATTEMPTS + 1:
                record['results'].clear()
            record['results'][request_id] = result
            return result
        return self._change(complete)
