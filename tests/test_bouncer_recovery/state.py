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


@th.django_unit_test("Continue retries preserve the grant and current restrictions always win")
def test_replay_and_late_restriction(opts):
    from mojo.apps.account.services.bouncer.hosted_challenge import ChallengeStore
    now = [1000]
    store = ChallengeStore(MemoryRedis(), 'site.test', 'one-browser', clock=lambda: now[0])
    challenge = store.issue('login')
    descriptor = challenge['descriptor']
    good = store.complete(descriptor, 'check', 'success', policy='check')
    assert good['next_action'] == 'check_cookie', "Continue grants only the cookie-confirmation step"
    now[0] += 1
    for operation in ('check', 'submit'):
        again = store.complete(descriptor, operation, 'retry', policy='check', answer=0)
        assert again == good, "network and legacy client retries retain the exact pass timestamp"
    denied = store.complete(descriptor, 'check', 'success', policy='decoy')
    assert denied['next_action'] == 'decoy', "a newly discovered restriction must override a completed grant"
    assert store.complete(descriptor, 'check', 'retry', policy='check')['next_action'] == 'decoy', "a selected restriction must remain sticky"


@th.django_unit_test("wrong-host and expired capabilities never grant access")
def test_scope_and_expiry(opts):
    from mojo.apps.account.services.bouncer.hosted_challenge import ChallengeStore
    redis = MemoryRedis()
    now = [1000]
    store = ChallengeStore(redis, 'site.test', 'browser', clock=lambda: now[0])
    challenge = store.issue('login')
    other = ChallengeStore(redis, 'other.test', 'browser', clock=lambda: now[0])
    assert other.read(challenge['descriptor']) is None, "a descriptor is bound to its render host"
    assert other.complete(challenge['descriptor'], 'check', 'wrong-host', policy='check')['reason'] == 'expired', "a foreign descriptor cannot grant access"
    now[0] += 301
    assert store.complete(challenge['descriptor'], 'check', 'expired', policy='check')['reason'] == 'expired', "an expired descriptor cannot grant access"


@th.django_unit_test('real Redis serializes parallel checks and preserves restrictions and expiry')
def test_real_redis_parallel_transitions(opts):
    from concurrent.futures import ThreadPoolExecutor
    import uuid
    from mojo.helpers.redis import get_bounded_connection
    from mojo.apps.account.services.bouncer.hosted_challenge import ChallengeStore
    redis = get_bounded_connection(timeout=1, read_from_replicas=False)
    now = [1000]
    store = ChallengeStore(redis, 'parallel.test', uuid.uuid4().hex, clock=lambda: now[0])
    try:
        challenge = store.issue('login')
        def check(index):
            return store.complete(challenge['descriptor'], 'check', str(index), policy='check')
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(check, range(3)))
        assert all(r == {'next_action': 'check_cookie', 'issued': 1000} for r in results), 'parallel checks must share one atomic grant'
        form = store.issue('login', form=True)
        assert store.authorize(form['descriptor'], 'decoy')['stage'] == 'decoy', 'form restrictions must persist atomically'
        assert store.authorize(form['descriptor'], 'check')['stage'] == 'decoy', 'a later clean request must retain the form restriction'
        assert store.authorize(form['descriptor'], 'recovery')['stage'] == 'decoy', 'operator recovery cannot downgrade a selected decoy'
        now[0] += 301
        assert store.read(challenge['descriptor']) is None, 'challenge expires after five minutes'
        assert store.complete(challenge['descriptor'], 'check', 'expired', policy='check')['reason'] == 'expired', 'expired challenge cannot grant a pass'
    finally:
        redis.delete(store.key)
        redis.close()


@th.django_unit_test('state failures never return a grant')
def test_unavailable_state(opts):
    from mojo.apps.account.services.bouncer.hosted_challenge import ChallengeStore, ChallengeUnavailable
    class BusyRedis(MemoryRedis):
        def eval(self, *args):
            return 0
    try:
        ChallengeStore(BusyRedis(), 'site.test', 'browser').issue('login')
    except ChallengeUnavailable:
        return
    raise AssertionError('failed atomic state write must end in unavailability, never an issued challenge')
