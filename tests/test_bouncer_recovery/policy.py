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
