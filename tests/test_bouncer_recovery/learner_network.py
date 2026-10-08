"""#7392: fake bot reports cannot make the Bouncer block a network, and the
learner leaves alone a signature a person made or switched off.

The learner used to write a `subnet_24` signature, good for a day, after five
reported blocks from one IPv4 /24 in an hour. The reports are an anonymous
caller's own word, so five requests locked out everyone behind a shared
address. On a row that already existed it also set `is_active` and a new
expiry, switching a signature an operator had turned off back on.

The learner is called in process with an object carrying `.payload`, as the
job engine calls it. Each test takes its own /24 at random from the shared
address space (100.64.0.0/10) and removes what it made: its rows, its
counters, its events and the event limiter keys. The signature cache is
rebuilt from the table and never deleted, so a test only ever asserts on
values that are its own.
"""
import hashlib
import json
import random
import uuid

from testit import helpers as th
from testit.helpers import assert_eq, assert_true

LEARN_FUNC = 'mojo.apps.account.services.bouncer.learner.learn_from_block'
REFRESH_FUNC = 'mojo.apps.account.asyncjobs.refresh_bouncer_sig_cache'
EVENT_CATEGORY = 'security:bouncer:subnet'
PWORD = 'Learner-network-7392!'


class _Job:
    def __init__(self, payload):
        self.payload = payload


class _Net:
    """One /24 owned by one test, and everything the test made on it."""

    def __init__(self):
        self.prefix = f'100.{random.randint(64, 127)}.{random.randint(0, 255)}'
        self.subnet = f'{self.prefix}.0/24'
        self.marker = 'm7392' + uuid.uuid4().hex[:16]
        self.signal = f'test_signal_{self.marker}'
        self.values = [self.subnet]

    def ip(self, host):
        return f'{self.prefix}.{host}'

    def campaign_hash(self, signals):
        return hashlib.sha256(json.dumps(sorted(signals)).encode()).hexdigest()[:16]

    def report(self, count, signals=None, first_host=1):
        """Run the learner `count` times, each from another address of the /24."""
        from mojo.apps.account.services.bouncer.learner import learn_from_block
        for host in range(first_host, first_host + count):
            learn_from_block(_Job({
                'muid': '',
                'duid': 'duid-' + uuid.uuid4().hex[:12],
                'ip': self.ip(host),
                'fingerprint_id': '',
                'risk_score': 85,
                'triggered_signals': signals or [],
                'user_agent': '',
            }))

    def rows(self):
        from mojo.apps.account.models import BotSignature
        return BotSignature.objects.filter(value__in=self.values)

    def row(self, sig_type='subnet_24', value=None, **fields):
        from mojo.apps.account.models import BotSignature
        value = value or self.subnet
        if value not in self.values:
            self.values.append(value)
        fields.setdefault('confidence', 90)
        fields.setdefault('is_active', True)
        fields.setdefault('block_count', 1)
        return BotSignature.objects.create(sig_type=sig_type, value=value, **fields)

    def events(self):
        from mojo.apps.incident.models import Event
        return Event.objects.filter(category=EVENT_CATEGORY, metadata__subnet=self.subnet)

    def matches(self, host=200):
        from mojo.apps.account.services.bouncer.learner import check_signature_cache
        matched, _, _ = check_signature_cache(self.ip(host))
        return matched

    def forget_the_hour(self):
        """Remove the counters and the once-an-hour event flag of this /24:
        what the passing of the counting hour does."""
        from mojo.apps.account.services.bouncer import learner
        from mojo.apps.incident.reporter import notice_key
        from mojo.helpers.redis import get_connection
        get_connection().delete(
            f'{learner._SUBNET_PREFIX}{self.subnet}',
            f'{learner._CAMPAIGN_PREFIX}{self.campaign_hash([self.signal])}',
            notice_key(EVENT_CATEGORY, self.subnet))

    def cleanup(self):
        from mojo.apps.account.services.bouncer.learner import refresh_sig_cache
        from mojo.apps.incident.reporter import budget_key
        from mojo.helpers.redis import get_connection
        self.rows().delete()
        self.events().delete()
        self.forget_the_hour()
        # the hourly cap on these events is shared: give back what was used
        get_connection().delete(budget_key(EVENT_CATEGORY, 3600))
        refresh_sig_cache()


def _refresh():
    from mojo.apps.account.services.bouncer.learner import refresh_sig_cache
    refresh_sig_cache()


def _in_a_day():
    from datetime import timedelta
    from mojo.helpers import dates
    return dates.utcnow() + timedelta(days=1)


# ---------------------------------------------------------------------------
# No automatic network block
# ---------------------------------------------------------------------------

@th.django_unit_test('#7392: five fake bot reports from one network do not block that network')
def test_five_reports_from_one_network_do_not_block_that_network(opts):
    net = _Net()
    try:
        net.report(5)
        assert_eq(net.rows().count(), 0, 'five reports from one /24 must not write a signature for it')
        assert_true(not net.matches(), 'another address in that /24 must not match a signature')
        net.report(3, first_host=10)
        assert_eq(net.rows().count(), 0, 'nor do more reports past the threshold')
    finally:
        net.cleanup()


@th.django_unit_test('#7392: five fake bot reports through the public check do not block the network')
def test_public_check_cannot_block_a_network(opts):
    """No job engine runs during the suite, so the jobs the endpoint publishes
    are run here, in process, selected by this test's own User-Agent. The
    test server takes the caller's address from X-Real-IP, so the reports
    count against this test's /24 and not against loopback."""
    from testit.client import RestClient
    from mojo.apps.account.models.bouncer_device import BouncerDevice
    from mojo.apps.jobs.models import Job
    net = _Net()
    user_agent = f'Mozilla/5.0 (X11; Linux x86_64) net-test {net.marker}'
    # one different extra signal per report, so the five do not form a campaign
    extras = ['selenium_artifacts', 'phantom_globals', 'nightmare_global', 'screen_zero', 'outer_size_zero']
    duids = ['duid-' + uuid.uuid4().hex[:12] for _ in extras]
    client = RestClient(opts.client.host)
    mine = Job.objects.filter(func=LEARN_FUNC, payload__user_agent=user_agent)
    try:
        for host, (duid, extra) in enumerate(zip(duids, extras), start=1):
            resp = client.post('/api/account/bouncer/assess', {
                'duid': duid,
                'page_type': 'login',
                'session_id': 'sess-' + uuid.uuid4().hex[:12],
                'signals': {
                    'environment': {'webdriver_flag': True, 'playwright_artifacts': True,
                                    'puppeteer_artifacts': True, extra: True},
                    'behavior': {'mouse_move_count': 0},
                },
            }, headers={'User-Agent': user_agent, 'X-Real-IP': net.ip(host)})
            assert_eq(resp.status_code, 200, f'the public check answers, got {resp.status_code}')
            assert_eq(resp.json.data.decision, 'block', 'three asserted automation signals are a block')
        assert_eq(sorted(mine.values_list('payload__ip', flat=True)), sorted(net.ip(h) for h in range(1, 6)),
                  'each blocked report publishes one learn job carrying the address the server saw')
        ran = th.run_pending_jobs(func=LEARN_FUNC, payload={'user_agent': user_agent})
        assert_eq(ran, 5, 'the five learn jobs run')
        assert_eq(net.rows().count(), 0, 'five public reports from one /24 must not write a signature for it')
        assert_true(not net.matches(), 'another visitor on that network must not match a signature')
        assert_eq(net.events().count(), 1, 'and the threshold is recorded once')
    finally:
        client.session.close()
        from mojo.helpers.redis import get_connection
        from mojo.apps.account.services.bouncer import learner
        campaigns = {net.campaign_hash(p.get('triggered_signals') or [])
                     for p in mine.values_list('payload', flat=True)}
        if campaigns:
            get_connection().delete(*[f'{learner._CAMPAIGN_PREFIX}{c}' for c in campaigns])
        mine.delete()
        BouncerDevice.objects.filter(duid__in=duids).delete()
        net.cleanup()


@th.django_unit_test('#7392: a learned network signature already stored is not enforced')
def test_learned_subnet_signature_is_not_enforced(opts):
    net = _Net()
    try:
        net.row(source='auto', expires_at=_in_a_day(), block_count=5)
        _refresh()
        assert_true(not net.matches(), 'a network signature the learner wrote must no longer match')
        assert_eq(net.rows().filter(is_active=True).count(), 1, 'the stored row is left as it was')
    finally:
        net.cleanup()


# ---------------------------------------------------------------------------
# What a person made stays enforced
# ---------------------------------------------------------------------------

@th.django_unit_test('#7392: a manual network signature is still enforced')
def test_manual_subnet_signature_is_still_enforced(opts):
    net = _Net()
    try:
        net.row(source='manual', expires_at=_in_a_day())
        _refresh()
        assert_true(net.matches(), "an operator's network signature with an expiry must still match")
        net.rows().delete()
        net.row(source='imported', expires_at=_in_a_day())
        _refresh()
        assert_true(net.matches(), "a network signature with source 'imported' must still match")
    finally:
        net.cleanup()


@th.django_unit_test('#7392: an automatic network signature with no expiry is still enforced')
def test_auto_subnet_signature_without_expiry_is_still_enforced(opts):
    """The learner always sets an expiry. A row marked auto without one was
    made by a person through the ORM or a shell, where auto is the default."""
    net = _Net()
    try:
        net.row(source='auto', expires_at=None)
        _refresh()
        assert_true(net.matches(), 'a network signature with source auto and no expiry must still match')
    finally:
        net.cleanup()


@th.django_unit_test('#7392: an automatic address signature is still enforced')
def test_auto_ip_signature_is_still_enforced(opts):
    """The learner has never written an `ip` row, so each one is a person's."""
    net = _Net()
    try:
        net.row(sig_type='ip', value=net.ip(77), source='auto', expires_at=_in_a_day())
        net.row(sig_type='ip', value=net.ip(78), source='auto', expires_at=None)
        _refresh()
        assert_true(net.matches(77), 'an address signature with source auto and an expiry must still match')
        assert_true(net.matches(78), 'an address signature with source auto and no expiry must still match')
        assert_true(not net.matches(79), 'and it matches that address only')
    finally:
        net.cleanup()


@th.django_unit_test('#7392: a network signature created over REST without a source is enforced')
def test_signature_created_over_rest_without_source_is_enforced(opts):
    from testit.client import RestClient
    from mojo.apps.account.models import User
    net = _Net()
    name = f'{net.marker}@example.test'
    user = User.objects.create_user(username=name, email=name, password=PWORD)
    user.is_active = user.is_email_verified = True
    user.permissions = {'manage_security': True}
    user.save()
    client = RestClient(opts.client.host)
    try:
        assert_true(client.login(name, PWORD), 'the security operator must authenticate')
        resp = client.post('/api/account/bouncer/signature',
                           {'sig_type': 'subnet_24', 'value': net.subnet, 'expires_at': _in_a_day().isoformat()})
        assert_eq(resp.status_code, 200, f'the operator can create a network signature, got {resp.status_code}')
        _refresh()
        assert_true(net.matches(), 'a network signature made over REST with no source and an expiry is enforced')
    finally:
        client.session.close()
        user.delete()
        net.cleanup()


# ---------------------------------------------------------------------------
# The learner leaves a person's row alone
# ---------------------------------------------------------------------------

def _state(row):
    row.refresh_from_db()
    return (row.is_active, row.expires_at, row.block_count, row.confidence)


@th.django_unit_test('#7392: the learner does not switch a deactivated signature back on')
def test_learner_does_not_switch_a_deactivated_signature_back_on(opts):
    net = _Net()
    try:
        # a network row an older release learned, switched off by an operator
        subnet_row = net.row(source='auto', expires_at=_in_a_day(), is_active=False)
        # and a campaign row, which the learner still writes, switched off too
        campaign_row = net.row(sig_type='signal_set', value=net.campaign_hash([net.signal]),
                               source='auto', expires_at=_in_a_day(), is_active=False)
        before = _state(subnet_row), _state(campaign_row)
        net.report(6, signals=[net.signal])
        assert_eq(_state(subnet_row), before[0], 'a switched-off network signature is left exactly as it was')
        assert_eq(_state(campaign_row), before[1], 'a switched-off campaign signature is left exactly as it was')
        assert_true(not net.matches(), 'and the network is not blocked')
    finally:
        net.cleanup()


@th.django_unit_test('#7392: the learner does not change a manual signature')
def test_learner_does_not_change_a_manual_signature(opts):
    net = _Net()
    try:
        # permanent manual rows: before, the learner gave them an expiry
        subnet_row = net.row(source='manual', expires_at=None)
        campaign_row = net.row(sig_type='signal_set', value=net.campaign_hash([net.signal]),
                               source='manual', expires_at=None)
        before = _state(subnet_row), _state(campaign_row)
        net.report(6, signals=[net.signal])
        assert_eq(_state(subnet_row), before[0], "an operator's permanent network block is left exactly as it was")
        assert_eq(_state(campaign_row), before[1], "an operator's campaign signature is left exactly as it was")
        assert_true(net.matches(), "and the operator's network block is still enforced")
    finally:
        net.cleanup()


class _EditWhileTheLearnerHoldsTheRow:
    """Apply an operator's edit to one signature at the moment the learner
    has read it and not yet written: the row the learner holds is then older
    than the table. Uses Django's own post_init signal as the seam, so no
    production code is replaced."""

    def __init__(self, row, **edit):
        self.pk, self.edit, self.done = row.pk, edit, 0

    def _receive(self, sender, instance, **kwargs):
        if instance.pk == self.pk and not self.done:
            self.done += 1
            # update(), not save(): it builds no instance, so it cannot re-enter
            sender.objects.filter(pk=self.pk).update(**self.edit)

    def __enter__(self):
        from django.db.models.signals import post_init
        from mojo.apps.account.models import BotSignature
        self._signal, self._sender = post_init, BotSignature
        post_init.connect(self._receive, sender=BotSignature, weak=False)
        return self

    def __exit__(self, *exc):
        self._signal.disconnect(self._receive, sender=self._sender)


def _overlapping_edit_is_kept(what, **edit):
    from mojo.apps.account.models import BotSignature
    from mojo.apps.account.services.bouncer.learner import _upsert_signature
    net = _Net()
    value = net.campaign_hash([net.signal])
    try:
        # a campaign row the learner wrote: it is allowed to extend this one
        row = net.row(sig_type='signal_set', value=value, source='auto',
                      expires_at=_in_a_day(), confidence=10)
        with _EditWhileTheLearnerHoldsTheRow(row, expires_at=None, **edit) as step:
            _upsert_signature('signal_set', value, 'auto', 90, 86400)
        assert_eq(step.done, 1, 'this test needs the edit to land while the learner holds the row')
        stored = BotSignature.objects.filter(pk=row.pk).values(
            'source', 'is_active', 'expires_at', 'block_count', 'confidence').get()
        expected = {'source': 'auto', 'is_active': True, 'expires_at': None,
                    'block_count': 1, 'confidence': 10}
        expected.update(edit)
        assert_eq(stored, expected, f'{what} while the learner held the row is left exactly as the operator saved it')
    finally:
        net.cleanup()


@th.django_unit_test('#7392: a signature switched off while the learner holds it is not written over')
def test_switch_off_during_the_learner_save_is_kept(opts):
    _overlapping_edit_is_kept('a signature switched off and made permanent', is_active=False)


@th.django_unit_test('#7392: a signature made manual while the learner holds it is not written over')
def test_made_manual_during_the_learner_save_is_kept(opts):
    _overlapping_edit_is_kept('a signature made manual and permanent', source='manual')


@th.django_unit_test('#7392: the learner still extends a campaign signature it wrote itself')
def test_learner_still_extends_its_own_campaign_signature(opts):
    from mojo.apps.account.models import BotSignature
    net = _Net()
    value = net.campaign_hash([net.signal])
    net.values.append(value)
    try:
        net.report(5, signals=[net.signal])
        row = BotSignature.objects.get(sig_type='signal_set', value=value)
        assert_eq((row.source, row.is_active, row.block_count), ('auto', True, 1),
                  'the fifth report of one signal set writes the campaign signature')
        net.report(1, signals=[net.signal], first_host=9)
        row.refresh_from_db()
        assert_eq(row.block_count, 2, 'and the next report counts on the same row')
    finally:
        net.cleanup()


# ---------------------------------------------------------------------------
# The scheduled refresh
# ---------------------------------------------------------------------------

@th.django_unit_test('#7392: the signature cache refresh is scheduled every 15 minutes')
def test_signature_cache_refresh_is_scheduled(opts):
    from mojo.apps.account import cronjobs  # noqa: F401  registers the schedule
    from mojo.decorators.cron import schedule
    specs = [spec for spec in getattr(schedule, 'scheduled_functions', [])
             if spec['func'].__module__ == 'mojo.apps.account.cronjobs'
             and spec['func'].__name__ == 'refresh_bouncer_sig_cache']
    assert_eq(len(specs), 1, 'refresh_bouncer_sig_cache must be a scheduled function of the account app')
    assert_eq((specs[0]['minutes'], specs[0]['hours']), ('*/15', '*'), 'and it runs every 15 minutes')


@th.django_unit_test('#7392: the scheduled refresh puts a manual signature into the cache')
def test_scheduled_refresh_restores_manual_signatures(opts):
    """A manual row the cache does not hold stands for a cache that expired
    on a quiet site. The key itself is shared and is not deleted here."""
    from mojo.apps.account import cronjobs
    from mojo.apps.jobs.models import Job
    net = _Net()
    before = set(Job.objects.filter(func=REFRESH_FUNC).values_list('pk', flat=True))
    try:
        net.row(source='manual', expires_at=None)
        assert_true(not net.matches(), 'this test needs the new row to be missing from the cache')
        cronjobs.refresh_bouncer_sig_cache()
        published = Job.objects.filter(func=REFRESH_FUNC).exclude(pk__in=before)
        assert_eq(published.count(), 1, 'the scheduled function publishes one refresh job')
        assert_eq(published.first().channel, 'cleanup', 'on the cleanup channel')
        assert_true(th.run_pending_jobs(func=REFRESH_FUNC) >= 1, 'the refresh job runs')
        assert_true(net.matches(), 'after the scheduled refresh the manual signature is enforced')
    finally:
        Job.objects.filter(func=REFRESH_FUNC).exclude(pk__in=before).delete()
        net.cleanup()


# ---------------------------------------------------------------------------
# The record an operator can list
# ---------------------------------------------------------------------------

@th.django_unit_test('#7392: a network reaching the threshold records one event and starts nothing')
def test_network_threshold_records_one_event(opts):
    from mojo.apps.incident.models.event import INCIDENT_LEVEL_THRESHOLD
    net = _Net()
    try:
        net.report(4)
        assert_eq(net.events().count(), 0, 'nothing is recorded below the threshold')
        net.report(2, first_host=5)
        assert_eq(net.events().count(), 1, 'six reports in one hour record one event')
        event = net.events().first()
        assert_eq(event.level, 5, 'the event is level 5')
        assert_true(event.level < INCIDENT_LEVEL_THRESHOLD, 'which is below the level that opens an incident')
        assert_eq(event.source_ip, None, 'it carries no address a rule could block')
        assert_true(not any((event.metadata or {}).get(key) for key in ('source_ip', 'request_ip')),
                    'nor one in its metadata')
        assert_eq(event.incident_id, None, 'no incident is opened for it')
        assert_eq((event.metadata.get('subnet'), event.metadata.get('report_count')), (net.subnet, 5),
                  'it names the network and the count')

        net.forget_the_hour()
        net.report(5, first_host=20)
        assert_eq(net.events().count(), 2, 'the next counting hour records one more')
    finally:
        net.cleanup()


@th.django_unit_test('#7392: the campaign incident carries no address')
def test_campaign_incident_carries_no_address(opts):
    """Its default rule blocks an address. It cannot, because the event has none."""
    from mojo.apps.incident.models import Event
    net = _Net()
    campaign = net.campaign_hash([net.signal])
    mine = Event.objects.filter(category='security:bouncer:campaign', metadata__campaign_hash=campaign)
    try:
        net.report(5, signals=[net.signal])
        assert_eq(mine.count(), 1, 'five reports of one signal set record the campaign once')
        event = mine.first()
        assert_eq(event.source_ip, None, 'the campaign event carries no address')
        assert_true(not any((event.metadata or {}).get(key) for key in ('source_ip', 'request_ip')),
                    'nor one in its metadata')
    finally:
        incidents = [pk for pk in mine.values_list('incident_id', flat=True) if pk]
        mine.delete()
        if incidents:
            from mojo.apps.incident.models import Incident
            Incident.objects.filter(pk__in=incidents, events__isnull=True).delete()
        net.values.append(campaign)
        net.cleanup()
