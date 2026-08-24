"""FileManager AssumeRole settings: storage, exposure, and who may write them.

``on_rest_save_field`` dispatches ``set_<key>`` for any key in the payload and
``FileManager.RestMeta.SAVE_PERMS`` is the group-level ``files`` permission, so
an ungated ``set_assume_role_arn`` would let any file admin point the platform's
own credentials at a role of their choosing — a confused deputy. Writing the
role settings therefore requires a superuser, and the external ID reads back
only as a boolean.
"""
import json

from unittest import mock

from testit import helpers as th
from testit.helpers import assert_eq, assert_true

ROLE_ARN = "arn:aws:iam::210987654321:role/tenant-fileman"
OTHER_ROLE_ARN = "arn:aws:iam::999999999999:role/attacker-controlled"
EXTERNAL_ID = "d0not-leak-this-external-id"


@th.django_unit_setup()
def setup_manager_role_settings(opts):
    from mojo.apps.account.models import User
    from mojo.apps.fileman.models import FileManager

    # Long-lived database — clear leftovers before creating.
    FileManager.objects.filter(name__startswith="fm_rolecfg_").delete()
    User.objects.filter(username__startswith="fm_rolecfg_").delete()

    opts.superuser = User.objects.create(
        username="fm_rolecfg_super",
        email="fm_rolecfg_super@example.com",
        is_superuser=True,
    )
    opts.member = User.objects.create(
        username="fm_rolecfg_member",
        email="fm_rolecfg_member@example.com",
        is_superuser=False,
    )

    s3_fm = FileManager(
        name="fm_rolecfg_s3",
        backend_type="s3",
        backend_url="s3://rolecfg-bucket/fileman/prefix",
        is_active=True,
    )
    s3_fm.save()
    opts.s3_fm_id = s3_fm.pk

    file_fm = FileManager(
        name="fm_rolecfg_file",
        backend_type="file",
        backend_url="file://",
        is_active=True,
    )
    file_fm.save()
    opts.file_fm_id = file_fm.pk


def _fm(fm_id):
    from mojo.apps.fileman.models import FileManager
    return FileManager.objects.get(pk=fm_id)


class _FakeRequest:
    def __init__(self, user):
        self.user = user


def _as_request_user(user):
    """Bind an active REST request so the model-level guards see an actor."""
    from mojo.models.rest import ACTIVE_REQUEST
    return ACTIVE_REQUEST, ACTIVE_REQUEST.set(_FakeRequest(user))


@th.django_unit_test("Role settings: assume_role_arn round-trips through settings")
def test_assume_role_arn_round_trips(opts):
    fm = _fm(opts.s3_fm_id)
    fm.set_assume_role_arn(ROLE_ARN)
    fm.set_external_id(EXTERNAL_ID)
    fm.save()

    reloaded = _fm(opts.s3_fm_id)
    assert_eq(reloaded.assume_role_arn, ROLE_ARN,
              f"assume_role_arn must persist, got {reloaded.assume_role_arn!r}")
    assert_eq(reloaded.get_setting("external_id"), EXTERNAL_ID,
              "the external id must persist for the backend to read")
    assert_true(reloaded.has_external_id,
                "has_external_id must report a configured external id")


@th.tier("core")
@th.django_unit_test("Role settings: a non-superuser cannot repoint the platform identity")
def test_non_superuser_cannot_set_assume_role_arn(opts):
    from mojo import errors as me

    fm = _fm(opts.s3_fm_id)
    fm.set_assume_role_arn(ROLE_ARN)
    fm.save()

    context, token = _as_request_user(opts.member)
    denied = False
    try:
        try:
            fm.set_assume_role_arn(OTHER_ROLE_ARN)
        except me.PermissionDeniedException:
            denied = True
        external_denied = False
        try:
            fm.set_external_id("attacker-external-id")
        except me.PermissionDeniedException:
            external_denied = True
    finally:
        context.reset(token)

    assert_true(denied,
                "A user holding only the group-level files permission must not be able "
                "to set assume_role_arn — that would repoint the platform's own AWS "
                "identity at a role they control")
    assert_true(external_denied,
                "set_external_id must be gated the same way as set_assume_role_arn")
    assert_eq(_fm(opts.s3_fm_id).assume_role_arn, ROLE_ARN,
              "The denied write must not have changed the stored role")


@th.django_unit_test("Role settings: a superuser may set the role")
def test_superuser_can_set_assume_role_arn(opts):
    fm = _fm(opts.s3_fm_id)

    context, token = _as_request_user(opts.superuser)
    try:
        fm.set_assume_role_arn(OTHER_ROLE_ARN)
        fm.save()
    finally:
        context.reset(token)

    assert_eq(_fm(opts.s3_fm_id).assume_role_arn, OTHER_ROLE_ARN,
              "A superuser must be able to configure the role")

    # Restore the fixture role for the remaining tests.
    fm = _fm(opts.s3_fm_id)
    fm.set_assume_role_arn(ROLE_ARN)
    fm.save()


@th.django_unit_test("Role settings: the raw external id is never serialized")
def test_external_id_is_not_exposed_in_the_graph(opts):
    fm = _fm(opts.s3_fm_id)
    fm.set_external_id(EXTERNAL_ID)
    fm.save()

    graph = _fm(opts.s3_fm_id).to_dict(graph="default")
    encoded = json.dumps(graph, default=str)

    assert_true(EXTERNAL_ID not in encoded,
                "The external id exists to be unguessable by whoever already knows the "
                f"role ARN — it must never appear in a serialized graph: {encoded}")
    assert_eq(graph.get("has_external_id"), True,
              f"The graph must report only that an external id is set, got {graph!r}")
    assert_eq(graph.get("assume_role_arn"), ROLE_ARN,
              f"The role ARN itself is not secret and must be readable, got {graph!r}")


@th.django_unit_test("Role settings: changing the role invalidates cached access evidence")
def test_fingerprint_changes_with_the_role(opts):
    fm = _fm(opts.s3_fm_id)
    fm.set_assume_role_arn(ROLE_ARN)
    fm.save()
    before = _fm(opts.s3_fm_id).public_access_config_fingerprint()

    fm = _fm(opts.s3_fm_id)
    fm.set_assume_role_arn(OTHER_ROLE_ARN)
    fm.save()
    after = _fm(opts.s3_fm_id).public_access_config_fingerprint()

    assert_true(before != after,
                "The effective identity changed, so cached public-access evidence must "
                f"be invalidated — fingerprint stayed {before}")

    fm = _fm(opts.s3_fm_id)
    fm.set_assume_role_arn(ROLE_ARN)
    fm.save()


@th.tier("core")
@th.django_unit_test("Role settings: _s3_client refuses a non-S3 manager readably")
def test_s3_client_rejects_a_filesystem_manager(opts):
    fm = _fm(opts.file_fm_id)
    try:
        fm._s3_client()
    except ValueError as err:
        assert_true("S3" in str(err),
                    f"The error must say the manager is not an S3 backend, got {err!r}")
    except Exception as err:
        assert False, (
            f"A filesystem manager must fail with a readable ValueError, not "
            f"{type(err).__name__}: {err}"
        )
    else:
        assert False, "CORS management must not be attempted on a filesystem manager"


@th.django_unit_test("Role settings: _s3_client uses the backend's own client")
def test_s3_client_delegates_to_the_backend(opts):
    from mojo.apps.fileman.backends import s3 as s3mod

    session = mock.Mock(name="session")
    session.client.return_value = mock.Mock(name="s3-client")

    with mock.patch.object(s3mod, "get_assumed_session", return_value=session), \
            mock.patch.object(s3mod, "get_session", return_value=session):
        fm = _fm(opts.s3_fm_id)
        client = fm._s3_client()
        assert_true(client is fm.backend.client,
                    "CORS calls must run through the same client as uploads — reading "
                    "this manager's own aws_key ignored the parent's settings, so a "
                    "child manager used a different identity than its uploads")
