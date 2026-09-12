from testit import helpers as th


@th.django_unit_test("recovery credit is bounded and cannot be claimed by a plugin name")
def test_recovery_credit_uses_class_identity(opts):
    from mojo.apps.account.services.bouncer.scoring import GeoAnalyzer, recovery_contribution

    class Plugin:
        name = 'geo'

    assert recovery_contribution(Plugin, 90, ['geo_vpn'], weight=lambda _: 90) == (0, 0), "a plugin named geo must keep its contribution"
    assert recovery_contribution(GeoAnalyzer, 10, ['geo_vpn', 'geo_tor'], weight=lambda _: 35) == (10, 0), "credit cannot exceed the actual positive contribution"
    assert recovery_contribution(GeoAnalyzer, -20, ['geo_vpn'], weight=lambda _: 35) == (0, 0), "negative contributions cannot create recovery credit"


@th.django_unit_test("raw score and legacy decision remain capped while hosted credit uses uncapped sum")
def test_uncapped_score_retains_strong_evidence(opts):
    from mojo.apps.account.services.bouncer.scoring import RiskScorer, ScoringContext, GeoAnalyzer

    class Strong:
        name = 'geo'

        @classmethod
        def analyze(cls, context):
            return 180, ['geo_vpn']

    context = ScoringContext({}, {'geo': {'is_tor': True, 'is_proxy': True, 'is_vpn': True}}, None, 'login')
    result = RiskScorer.score(context, analyzers=[GeoAnalyzer, Strong])
    assert result.score == 100 and result.decision == 'block', "legacy scoring remains capped at 100"
    assert result.metadata['uncapped_score'] - result.metadata['recovery_credit'] == 180, "soft credit must not erase an uncapped plugin block"


@th.django_unit_test('hosted policy preserves page boundaries, historical restrictions and strong evidence')
def test_hosted_routing_matrix(opts):
    from mojo.apps.account.services.bouncer.scoring import RiskScorer, ScoringResult
    from mojo.apps.account.services.bouncer.hosted_gate import route
    def result(raw, credit=0, history=0, page='login'):
        return ScoringResult(score=min(100, raw), decision=RiskScorer.decide(min(100, raw), page),
                             triggered_signals=[], signal_scores={}, metadata={'uncapped_score': raw, 'recovery_credit': credit, 'historical_block': history})
    for page in ('login', 'registration', 'public_message'):
        for raw in (0, 39, 40, 59, 60, 180):
            value = result(raw, page=page)
            expected = 'decoy' if value.decision == 'block' else ('check' if value.decision == 'allow' else 'slider')
            assert route(value, page) == expected, f'{page} must honor its actual configured boundary at {raw}'
        assert route(result(100, 100, page=page), page) == 'slider', 'ordinary uncertainty at the raw cap remains recoverable'
        assert route(result(180, 80, page=page), page) == 'decoy', 'uncapped retained block cannot be erased by recovery credit'
        assert route(result(100, 0, 100, page), page, blocked=True) == 'recovery', 'ambiguous historical block gets operator recovery'
        assert route(result(0, page=page), page, frozen=True) == 'recovery', 'stream freeze remains authoritative'
        assert route(result(0, page=page), page, matched=True) == 'decoy', 'active signature wins over an otherwise clean score'
