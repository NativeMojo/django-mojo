"""#7392: a manual signature is enforced again after the cache key is gone.

The signature cache key lives for an hour and used to be rebuilt only at the
end of a learn job. On a quiet site, or with learning switched off, the key
expired and every signature, manual ones included, stopped matching until
the next block. The rebuild is now scheduled every 15 minutes.

This test deletes the shared key and sets BOUNCER_LEARN_ENABLED in
django.conf, in process, so it lives in this serial package and not beside
tests/test_bouncer_recovery/learner_network.py.
"""
import random

from testit import helpers as th
from testit.helpers import assert_eq, assert_true

REFRESH_FUNC = 'mojo.apps.account.asyncjobs.refresh_bouncer_sig_cache'
KEY = 'BOUNCER_LEARN_ENABLED'
_SENTINEL = object()


@th.django_unit_test('#7392: with learning off and the cache key gone, the scheduled refresh restores a manual signature')
def test_refresh_restores_manual_signature_after_the_key_is_gone(opts):
    from django.conf import settings as dj_settings
    from mojo.apps.account import cronjobs
    from mojo.apps.account.models import BotSignature
    from mojo.apps.account.services.bouncer import learner
    from mojo.apps.jobs.models import Job
    from mojo.helpers.redis import get_connection

    prefix = f'100.{random.randint(64, 127)}.{random.randint(0, 255)}'
    subnet, address = f'{prefix}.0/24', f'{prefix}.200'
    redis = get_connection()
    orig = getattr(dj_settings, KEY, _SENTINEL)
    before = set(Job.objects.filter(func=REFRESH_FUNC).values_list('pk', flat=True))
    setattr(dj_settings, KEY, False)
    try:
        BotSignature.objects.create(
            sig_type='subnet_24', value=subnet, source='manual', expires_at=None,
            confidence=90, is_active=True, block_count=1)
        redis.delete(learner.SIG_CACHE_KEY)
        assert_true(not redis.exists(learner.SIG_CACHE_KEY), 'this test needs the cache key to be gone')
        assert_true(not learner.check_signature_cache(address)[0],
                    'with the key gone the manual signature does not match: the state the refresh repairs')

        cronjobs.refresh_bouncer_sig_cache()
        published = Job.objects.filter(func=REFRESH_FUNC).exclude(pk__in=before)
        assert_eq(published.count(), 1, 'the scheduled function publishes one refresh job')
        assert_true(th.run_pending_jobs(func=REFRESH_FUNC) >= 1, 'the refresh job runs with learning off')

        assert_true(redis.exists(learner.SIG_CACHE_KEY), 'the refresh job writes the cache key again')
        assert_true(learner.check_signature_cache(address)[0],
                    'after the scheduled refresh the manual signature is enforced again')
    finally:
        if orig is _SENTINEL:
            if hasattr(dj_settings, KEY):
                delattr(dj_settings, KEY)
        else:
            setattr(dj_settings, KEY, orig)
        Job.objects.filter(func=REFRESH_FUNC).exclude(pk__in=before).delete()
        BotSignature.objects.filter(sig_type='subnet_24', value=subnet).delete()
        learner.refresh_sig_cache()
