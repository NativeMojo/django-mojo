import json

from testit import helpers as th


class MemoryRedis:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def eval(self, script, count, key, old, new, ttl):
        if self.data.get(key, '') != old:
            return 0
        self.data[key] = new
        return 1


@th.django_unit_test("reloads and other purposes share three attempts and a cooldown")
def test_shared_retry_budget(opts):
    from mojo.apps.account.services.bouncer.hosted_challenge import ChallengeStore
    now = [1000]
    store = ChallengeStore(MemoryRedis(), 'site.test', 'one-browser', clock=lambda: now[0])
    first = store.issue('login', action='slider')
    second = store.issue('registration', action='slider')
    for index, challenge in enumerate([first, second, first]):
        result = store.complete(challenge['descriptor'], 'submit', f'answer-{index}', policy='slider', answer=0)
    assert result['next_action'] == 'cooldown' and result['retry_after'] == 60, "three misses across tabs must exhaust one budget"
    fresh = store.issue('public_message', action='slider')
    assert fresh['next_action'] == 'cooldown', "a new descriptor must not reset the cooldown"
    now[0] += 61
    ready = store.complete(first['descriptor'], 'check', 'retry-check', policy='slider')
    assert ready['attempts_remaining'] == 3, "explicit retry after cooldown starts a new budget"


@th.django_unit_test("replayed answers are idempotent and current restrictions win over completed grants")
def test_replay_and_late_restriction(opts):
    from mojo.apps.account.services.bouncer.hosted_challenge import ChallengeStore
    redis = MemoryRedis()
    store = ChallengeStore(redis, 'site.test', 'one-browser', clock=lambda: 1000)
    challenge = store.issue('login', action='slider')
    descriptor = challenge['descriptor']
    bad = store.complete(descriptor, 'submit', 'miss', policy='slider', answer=0)
    again = store.complete(descriptor, 'submit', 'miss', policy='slider', answer=0)
    assert bad == again and bad['attempts_remaining'] == 2, "lost-response retries must spend one attempt"
    good = store.complete(descriptor, 'submit', 'success', policy='slider', answer=challenge['target'])
    assert good['next_action'] == 'check_cookie', "a correct target may grant only the cookie-confirmation step"
    denied = store.complete(descriptor, 'submit', 'success', policy='decoy', answer=challenge['target'])
    assert denied['next_action'] == 'decoy', "a newly discovered restriction must override a cached success"
    assert json.loads(redis.data[store.key])['attempts'] == 1, "successful and replayed answers do not count as misses"


@th.django_unit_test("wrong-host capabilities and malformed numeric proofs never grant access")
def test_invalid_proof_and_scope(opts):
    from mojo.apps.account.services.bouncer.hosted_challenge import ChallengeStore
    redis = MemoryRedis()
    store = ChallengeStore(redis, 'site.test', 'browser', clock=lambda: 1000)
    challenge = store.issue('login', action='slider')
    assert ChallengeStore(redis, 'other.test', 'browser').read(challenge['descriptor']) is None, "a descriptor is bound to its render host"
    for answer in (True, float('nan'), float('inf'), '50', {}, -1, 101):
        result = store.complete(challenge['descriptor'], 'submit', 'invalid', policy='slider', answer=answer)
        assert result['next_action'] == 'error', "non-finite, non-number or out-of-range answers are invalid"
    assert json.loads(redis.data[store.key])['attempts'] == 0, "malformed transport data does not count as a wrong answer"
