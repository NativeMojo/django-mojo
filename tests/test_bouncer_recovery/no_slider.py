"""Recovery after retiring the hosted slider, including in-flight sessions."""
import json

from testit import helpers as th


@th.django_unit_test('recoverable uncertainty offers a Continue check')
def test_uncertain_visit_uses_continue(opts):
    from mojo.apps.account.services.bouncer.hosted_gate import route
    from mojo.apps.account.services.bouncer.scoring import ScoringResult
    result = ScoringResult(score=50, decision='monitor', triggered_signals=[],
                           signal_scores={}, metadata={'uncapped_score': 50, 'recovery_credit': 0, 'historical_block': 0})
    assert route(result, 'login') == 'check', 'an uncertain visitor must get Continue without a slider'


@th.django_unit_test('Continue recovers existing slider sessions even during the old cooldown')
def test_legacy_slider_session_can_continue(opts):
    from mojo.apps.account.services.bouncer.hosted_challenge import ChallengeStore
    from test_bouncer_recovery.state import MemoryRedis
    redis = MemoryRedis()
    store = ChallengeStore(redis, 'site.test', 'legacy-browser', clock=lambda: 1000)
    challenge = store.issue('login')
    state = json.loads(redis.data[store.key])
    state.update(attempts=3, cooldown=1060)
    record = state['descriptors'][challenge['descriptor']]
    record.update(stage='slider', target=50, results={
        'old-request': {'next_action': 'cooldown', 'retry_after': 60}})
    redis.data[store.key] = json.dumps(state)
    result = store.complete(challenge['descriptor'], 'check', 'old-request', policy='check')
    assert result['next_action'] == 'check_cookie', 'old slider misses and cooldown must not trap a recoverable visitor'
    assert result['issued'] == 1000, 'Continue must still require confirmation of the newly issued cookie'
