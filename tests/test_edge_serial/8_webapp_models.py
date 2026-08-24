"""
WebApp model — serial half (maestro #2792).

`test_bucket_fail_closed` removes the global EDGE_RELEASE_BUCKETS row to prove
registration fails closed with no declared buckets. That mutates a shared
Setting row visible to every parallel worker, so it runs serially, out of the
parallel `test_edge/8_webapp_models.py`.
"""
from testit import helpers as th

from tests.test_edge._helpers import (
    cleanup, declare_pools, declare_release_buckets, make_group, raises,
    RELEASE_BUCKET,
)


@th.django_unit_setup()
def setup_webapp_models(opts):
    cleanup()
    declare_pools()
    declare_release_buckets()
    opts.group = make_group("edgewebapp")


@th.django_unit_test("with no declared buckets, registering a site fails closed")
def test_bucket_fail_closed(opts):
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.edge.models import WebApp

    Setting.remove("EDGE_RELEASE_BUCKETS", group=None)
    try:
        err = raises(
            WebApp.objects.create, group=opts.group, slug="noconfig",
            bucket=RELEASE_BUCKET, prefix="x")
        assert err is not None, \
            "a site was registered with no release buckets declared"
    finally:
        declare_release_buckets()
