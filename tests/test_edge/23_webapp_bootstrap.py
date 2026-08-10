"""First-deploy WebApp and MOJO_DEPLOY_KEY bootstrap command."""

import hashlib
import io
import json

from django.core.management import call_command
from django.core.management.base import CommandError

from testit import helpers as th

from tests.test_edge._helpers import (
    RELEASE_BUCKET, cleanup, declare_pools, declare_release_buckets,
    make_certificate, make_domain, make_group, make_vhost, make_webapp,
)


@th.django_unit_setup()
def setup_bootstrap(opts):
    from mojo.apps.account.models import ApiKey

    cleanup()
    ApiKey.objects.filter(name__startswith="webapp:").delete()
    declare_pools()
    declare_release_buckets()
    opts.group = make_group("bootstrap")
    opts.domain = make_domain(group=opts.group)
    opts.certificate = make_certificate(opts.domain)
    opts.vhost = make_vhost(opts.domain, opts.certificate, label="portal")
    opts.json_domain = make_domain(group=opts.group)
    opts.json_certificate = make_certificate(opts.json_domain)
    opts.json_vhost = make_vhost(
        opts.json_domain, opts.json_certificate, label="")
    opts.foreign_domain = make_domain(group=opts.group)
    opts.foreign_certificate = make_certificate(opts.foreign_domain)
    opts.foreign_vhost = make_vhost(
        opts.foreign_domain, opts.foreign_certificate, label="")


def run_command(**options):
    stdout = io.StringIO()
    stderr = io.StringIO()
    call_command(
        "webapp_bootstrap", stdout=stdout, stderr=stderr, **options)
    return stdout.getvalue(), stderr.getvalue()


@th.django_unit_test("bootstrap creates WebApp and pipe-safe release-only key")
def test_create_and_token_only_output(opts):
    from mojo.apps.edge.models import WebApp

    stdout, stderr = run_command(
        slug="portal", group=opts.group.pk, vhost=opts.vhost.pk,
        bucket=RELEASE_BUCKET, token_only=True)
    token = stdout.strip()
    web_app = WebApp.objects.select_related("api_key").get(
        group=opts.group, slug="portal")

    assert token and "\n" not in token, "token-only output was not one token"
    assert web_app.api_key.token_hash == hashlib.sha256(token.encode()).hexdigest(), \
        "stdout was not the linked deployment token"
    assert web_app.api_key.permissions == {"release_webapp": True}, \
        f"bootstrap key has excess permissions: {web_app.api_key.permissions}"
    assert f"MOJO_WEBAPP_ID={web_app.pk}" in stderr, \
        "pipe mode did not send the WebApp id to stderr"
    assert token not in stderr, "the deployment token leaked into diagnostics"


@th.django_unit_test("existing WebApp can receive its first key by id")
def test_existing_webapp_by_id(opts):
    web_app = make_webapp(opts.group, slug="existing", vhost=None)

    stdout, _ = run_command(webapp=web_app.pk, token_only=True)

    web_app.refresh_from_db()
    assert web_app.api_key_id is not None, "existing WebApp was not linked"
    assert web_app.api_key.token_hash == hashlib.sha256(
        stdout.strip().encode()).hexdigest(), "returned token does not authenticate the key"


@th.django_unit_test("bootstrap refuses implicit key rotation")
def test_existing_key_requires_rotate(opts):
    from mojo.apps.account.models import ApiKey
    from mojo.apps.edge.services import webapp_keys

    web_app = make_webapp(opts.group, slug="already", vhost=None)
    _, original, _, _ = webapp_keys.link(web_app)

    try:
        run_command(webapp=web_app.pk, token_only=True)
        error = None
    except CommandError as exc:
        error = exc

    assert error is not None, "an existing key rotated without --rotate"
    web_app.refresh_from_db()
    original.refresh_from_db()
    assert web_app.api_key_id == original.pk and original.is_active, \
        "refused rotation changed the linked key"
    assert list(ApiKey.objects.filter(name="webapp:already").values_list(
        "pk", flat=True)) == [original.pk], \
        "refused rotation created another key for this WebApp"


@th.django_unit_test("explicit rotation revokes the previous key")
def test_explicit_rotation(opts):
    from mojo.apps.edge.services import webapp_keys

    web_app = make_webapp(opts.group, slug="rotate", vhost=None)
    _, previous, old_token, _ = webapp_keys.link(web_app)

    stdout, _ = run_command(
        webapp=web_app.pk, rotate=True, token_only=True)
    new_token = stdout.strip()

    web_app.refresh_from_db()
    previous.refresh_from_db()
    assert previous.is_active is False, "rotation left the previous key active"
    assert web_app.api_key_id != previous.pk, "rotation did not link a new key"
    assert new_token != old_token, "rotation returned the previous token"


@th.django_unit_test("bootstrap rejects a vhost outside the WebApp group")
def test_cross_group_vhost_refused(opts):
    from mojo.apps.account.models import ApiKey
    from mojo.apps.edge.models import WebApp

    other = make_group("bootstrap-other")
    try:
        run_command(
            slug="crossed", group=other.pk, vhost=opts.foreign_vhost.pk,
            bucket=RELEASE_BUCKET, token_only=True)
        error = None
    except CommandError as exc:
        error = exc

    assert error is not None, "bootstrap linked another group's vhost"
    assert not WebApp.objects.filter(group=other, slug="crossed").exists(), \
        "failed bootstrap left a WebApp behind"
    assert not ApiKey.objects.filter(name="webapp:crossed").exists(), \
        "failed bootstrap left its deployment key behind"


@th.django_unit_test("default output is structured and identifies creation")
def test_json_output(opts):
    stdout, stderr = run_command(
        slug="jsonsite", group=opts.group.pk, vhost=opts.json_vhost.pk,
        bucket=RELEASE_BUCKET)
    payload = json.loads(stdout)

    assert payload["webapp"] and payload["token"], \
        f"bootstrap JSON omitted required handoff fields: {payload}"
    assert payload["created"] is True and payload["rotated"] is False, \
        f"bootstrap JSON reported the wrong operation: {payload}"
    assert stderr == "", f"default output wrote unexpected diagnostics: {stderr}"
