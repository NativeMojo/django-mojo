"""Bounded, atomic state for hosted recovery, independent of the public SDK."""
import hashlib
import json
import secrets
import time

CHALLENGE_TTL = 300
FORM_TTL = 1800
STATE_TTL = 1860
MAX_DESCRIPTORS = 8

# One key owns the bounded descriptors. Comparing the
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
            # Retain the legacy shape while older workers may share this key.
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
                      'stage': 'check' if action in ('slider', 'cooldown') else action,
                      # Legacy workers still expect these fields during rollout.
                      'form': form, 'target': 50, 'results': {}, 'issued': None}
            entries[descriptor] = record
            return {'descriptor': descriptor, **self._view(record)}
        return self._change(issue)

    def read(self, descriptor):
        raw = self.redis.get(self.key)
        state = json.loads(raw) if raw else {}
        record = state.get('descriptors', {}).get(descriptor)
        if not record or record['expires'] < self.clock():
            return None
        return record

    def _view(self, record):
        stage = record['stage']
        if stage in ('slider', 'cooldown'):
            stage = 'check'
        result = {'next_action': 'check_cookie' if stage == 'granted' else stage}
        if stage == 'granted':
            result['issued'] = record['issued']
        return result

    def authorize(self, descriptor, policy):
        """Serialize fresh restrictions with grants, including form descriptors."""
        def authorize(state, now):
            record = state['descriptors'].get(descriptor)
            if not record or record['expires'] < now:
                return None
            if policy == 'decoy' or (policy == 'recovery' and record['stage'] != 'decoy'):
                record['stage'] = policy
            return dict(record)
        return self._change(authorize)

    def complete(self, descriptor, operation, request_id, *, policy, answer=None):
        def complete(state, now):
            record = state['descriptors'].get(descriptor)
            if not record or record['expires'] < now:
                return {'next_action': 'recovery', 'reason': 'expired'}
            if record['form']:
                return {'next_action': 'recovery', 'reason': 'restart'}
            # A new restriction always wins, even over a cached success.
            if policy == 'decoy' or (policy == 'recovery' and record['stage'] != 'decoy'):
                record['stage'] = policy
            if record['stage'] in ('decoy', 'recovery'):
                return self._view(record)
            if operation not in ('check', 'submit'):
                return {'next_action': 'error', 'reason': 'invalid'}
            # Old pages may still submit a slider answer. Treat that as Continue,
            # ignoring obsolete misses/cooldowns, only after fresh restrictions.
            # Persist one grant timestamp so network retries confirm the same cookie.
            if record['stage'] != 'granted':
                record.update(stage='granted', issued=int(now))
            return self._view(record)
        return self._change(complete)
