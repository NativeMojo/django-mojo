"""Recovery privacy and diagnostic write-budget regressions."""
from testit import helpers as th


@th.django_unit_test('general signal readers cannot retrieve or filter private review details')
def test_review_not_in_general_signals(opts):
    import uuid
    from testit.client import RestClient
    from mojo.apps.account.models import BouncerSignal, User
    name = 'review-private-' + uuid.uuid4().hex[:12] + '@example.test'
    user = User.objects.create_user(username=name, email=name, password='Review-private-4478!')
    user.is_active = user.is_email_verified = True
    user.permissions = {'users': True}
    user.save()
    row = BouncerSignal.objects.create(server_signals={
        'normal_signal': True,
        'hosted_gate': {'reason': 'cookies', 'review': {
            'email': name, 'note': 'private report', 'resolution': 'private resolution'}}})
    client = RestClient(opts.client.host)
    try:
        assert client.login(name, 'Review-private-4478!'), 'general signal reader must authenticate'
        for url in (f'/api/account/bouncer/signal/{row.pk}?graph=detail',
                    f'/api/account/bouncer/signal?graph=detail&id={row.pk}'):
            response = client.get(url)
            assert response.status_code == 200, 'existing general signal access must remain available'
            text = str(response.json)
            assert name not in text and 'private report' not in text and 'private resolution' not in text, 'private review content must only appear on the protected recovery endpoint'
            assert 'normal_signal' in text and 'cookies' in text, 'ordinary diagnostic details must remain available'
        ignored = client.get(f'/api/account/bouncer/signal?id={row.pk}&server_signals__hosted_gate__review__email=does-not-match')
        assert ignored.status_code == 200 and len(ignored.json.data) == 1 and ignored.json.data[0].id == row.pk, 'sensitive filters must be ignored rather than become an oracle for review contact data'
        assert client.get('/api/auth/bouncer/recovery').status_code == 403, 'general user permissions cannot read recovery queue'
    finally:
        row.delete()
        user.delete()
        client.session.close()


@th.django_unit_test('diagnostic budget exhaustion does not persist rows or issue unusable tickets')
def test_diagnostic_write_budget(opts):
    import uuid
    from django.test import RequestFactory
    from mojo.helpers.redis import get_bounded_connection
    from mojo.apps.account.models import BouncerSignal
    from mojo.apps.account.services.bouncer import hosted_recovery as service
    request = RequestFactory().get('/auth', HTTP_HOST='budget-' + uuid.uuid4().hex + '.test')
    request.ip = '198.18.0.17'
    request.muid = uuid.uuid4().hex
    _, key = service._budget_keys(request.ip)
    redis = get_bounded_connection(timeout=1, read_from_replicas=False)
    try:
        redis.set(key, 300, ex=300)
        diagnostic = service.record(request, 'login', 'recovery', 'cookies')
        assert diagnostic.get('reference'), 'the page must retain a support reference'
        assert 'review_ticket' not in diagnostic, 'an unpersisted diagnostic must not offer a broken intake ticket'
        assert not BouncerSignal.objects.filter(muid=request.muid).exists(), 'repeated page loads must stop creating persistent records at the diagnostic budget'
    finally:
        redis.delete(key)
        redis.close()
        BouncerSignal.objects.filter(muid=request.muid).delete()


@th.django_unit_test('diagnostic budgets share one Redis Cluster hash slot')
def test_diagnostic_cluster_slot(opts):
    from redis.crc import key_slot
    from mojo.apps.account.services.bouncer.hosted_recovery import _budget_keys
    for ip in ('198.18.0.17', '2001:db8::1', None):
        keys = _budget_keys(ip)
        assert len({key_slot(key.encode()) for key in keys}) == 1, 'multi-key admission must work on supported Redis Cluster deployments'


@th.django_unit_test('IP-denied diagnostic attempts preserve global capacity for other visitors')
def test_diagnostic_admission_is_atomic(opts):
    import uuid
    from mojo.helpers.redis import get_bounded_connection
    from mojo.apps.account.services.bouncer.hosted_recovery import _BUDGET
    prefix = 'test:bouncer-budget:{' + uuid.uuid4().hex + '}'
    global_key, ip_key, fresh_ip = prefix + ':global', prefix + ':ip', prefix + ':new'
    redis = get_bounded_connection(timeout=1, read_from_replicas=False)
    try:
        redis.set(global_key, 1, ex=300)
        redis.set(ip_key, 300, ex=300)
        allowed = redis.eval(_BUDGET, 2, global_key, ip_key, 3000, 300)
        assert not allowed, 'an exhausted IP must not be admitted'
        assert redis.get(global_key) == '1', 'IP-denied traffic must not consume global capacity'
        redis.set(global_key, 3000, ex=300)
        assert not redis.eval(_BUDGET, 2, global_key, fresh_ip, 3000, 300), 'global exhaustion must reject new writes'
        assert redis.get(fresh_ip) is None, 'global denial must not create unbounded per-IP keys'
        redis.delete(global_key, ip_key)
        assert redis.eval(_BUDGET, 2, global_key, ip_key, 3000, 300), 'available budgets must admit the write'
        assert redis.get(global_key) == redis.get(ip_key) == '1', 'admission must charge both budgets once'
        assert 0 < redis.ttl(global_key) <= 300 and 0 < redis.ttl(ip_key) <= 300, 'both budgets must expire'
    finally:
        redis.delete(global_key, ip_key, fresh_ip)
        redis.close()
