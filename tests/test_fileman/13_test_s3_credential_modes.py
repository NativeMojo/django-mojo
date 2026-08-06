"""The S3 backend resolves credentials through exactly one path.

The backend used to build a separate ``boto3.Session`` for its client, its
resource, and the account public-access probe. Three sessions meant the audit
could describe a different account than the one the bucket calls ran against,
and there was no way to reach a role in another account at all.

Everything now goes through ``_build_session()``: static keys, ambient
credentials, or ``sts:AssumeRole`` when ``assume_role_arn`` is configured.
No test here touches AWS — the session factories are patched.
"""
from unittest import mock

from testit import helpers as th
from testit.helpers import assert_eq, assert_true

ROLE_ARN = "arn:aws:iam::210987654321:role/tenant-fileman"


@th.django_unit_setup()
def setup_s3_credential_modes(opts):
    from mojo.apps.fileman.models import FileManager

    # Long-lived database — clear leftovers before creating.
    FileManager.objects.filter(name__startswith="fm_credmode_").delete()

    def _make(name, secrets):
        fm = FileManager(
            name=name,
            backend_type="s3",
            backend_url="s3://credmode-bucket/fileman/prefix",
            is_active=True,
        )
        fm.save()
        for key, value in secrets.items():
            fm.set_secret(key, value)
        fm.save()
        return fm.pk

    opts.static_fm_id = _make("fm_credmode_static", {
        "aws_key": "AKIACREDMODE",
        "aws_secret": "credmode-secret",
        "aws_region": "us-west-1",
    })
    opts.keyless_fm_id = _make("fm_credmode_keyless", {
        "aws_region": "us-east-2",
    })
    opts.role_fm_id = _make("fm_credmode_role", {
        "aws_region": "eu-west-1",
        "assume_role_arn": ROLE_ARN,
        "external_id": "credmode-external-id",
    })
    opts.halfpair_fm_id = _make("fm_credmode_halfpair", {
        "aws_key": "AKIAHALFPAIR",
        "aws_region": "us-east-1",
    })


def _backend(fm_id):
    """A fresh backend every time — FileManager memoizes the one it built."""
    from mojo.apps.fileman.models import FileManager
    return FileManager.objects.get(pk=fm_id).backend


def _fake_session(s3_client=None):
    """A boto3.Session stand-in whose .client()/.resource() are recorded."""
    session = mock.Mock(name="session")
    session.client.return_value = s3_client or mock.Mock(name="s3-client")
    session.resource.return_value = mock.Mock(name="s3-resource")
    return session


@th.django_unit_test("S3 creds: static keys still reach boto3 unchanged")
def test_static_keys_are_passed_through(opts):
    from mojo.apps.fileman.backends import s3 as s3mod

    with mock.patch.object(s3mod, "get_session", return_value=_fake_session()) as get_session, \
            mock.patch.object(s3mod, "get_assumed_session") as get_assumed:
        backend = _backend(opts.static_fm_id)
        assert_true(backend.client is not None, "the S3 client must be constructed")

    kwargs = get_session.call_args.kwargs
    assert_eq(kwargs["access_key"], "AKIACREDMODE",
              f"A configured access key must still reach boto3, got {kwargs!r}")
    assert_eq(kwargs["secret_key"], "credmode-secret",
              f"A configured secret must still reach boto3, got {kwargs!r}")
    assert_eq(kwargs["region"], "us-west-1",
              f"The configured region must still reach boto3, got {kwargs!r}")
    assert_eq(get_assumed.call_count, 0,
              "A manager with no role configured must not call sts:AssumeRole")


@th.django_unit_test("S3 creds: a keyless manager falls through to the default chain")
def test_keyless_manager_uses_default_chain(opts):
    from mojo.apps.fileman.backends import s3 as s3mod

    with mock.patch.object(s3mod, "get_session", return_value=_fake_session()) as get_session, \
            mock.patch.object(s3mod, "get_assumed_session") as get_assumed:
        backend = _backend(opts.keyless_fm_id)
        assert_true(backend.client is not None, "the S3 client must be constructed")

    kwargs = get_session.call_args.kwargs
    assert_true(kwargs["access_key"] is None and kwargs["secret_key"] is None,
                f"A keyless manager must pass no static credentials, got {kwargs!r}")
    assert_eq(get_assumed.call_count, 0,
              "A manager with no role configured must not call sts:AssumeRole")


@th.django_unit_test("S3 creds: assume_role_arn selects the STS path")
def test_role_manager_uses_assume_role(opts):
    from mojo.apps.fileman.backends import s3 as s3mod

    with mock.patch.object(s3mod, "get_session") as get_session, \
            mock.patch.object(s3mod, "get_assumed_session",
                              return_value=_fake_session()) as get_assumed:
        backend = _backend(opts.role_fm_id)
        assert_true(backend.client is not None, "the S3 client must be constructed")

    assert_eq(get_session.call_count, 0,
              "With a role configured the backend must not build a plain session — "
              "the source identity is resolved inside get_assumed_session")
    args, kwargs = get_assumed.call_args
    assert_eq(args[0], ROLE_ARN, f"The configured role must be assumed, got {args!r}")
    assert_eq(kwargs["external_id"], "credmode-external-id",
              f"The configured external id must be forwarded, got {kwargs!r}")
    assert_eq(kwargs["region"], "eu-west-1",
              f"The configured region must be forwarded, got {kwargs!r}")
    assert_true(str(kwargs["session_name"]).startswith("django-mojo-fileman-"),
                f"The role session name must be derived per manager, got {kwargs!r}")


@th.django_unit_test("S3 creds: one session backs both the client and the resource")
def test_session_is_built_once(opts):
    from mojo.apps.fileman.backends import s3 as s3mod

    with mock.patch.object(s3mod, "get_session", return_value=_fake_session()) as get_session:
        backend = _backend(opts.static_fm_id)
        backend.client
        backend.resource
        backend.client

    assert_eq(get_session.call_count, 1,
              f"The client and the resource must share one session, "
              f"got {get_session.call_count} sessions")


@th.django_unit_test("S3 creds: the account public-access probe shares the backend session")
def test_account_public_access_block_shares_session(opts):
    from mojo.apps.fileman.backends import s3 as s3mod

    assert_true(not hasattr(s3mod, "boto3"),
                "The S3 backend must not construct boto3 sessions of its own")

    sts = mock.Mock(name="sts")
    sts.get_caller_identity.return_value = {"Account": "210987654321"}
    s3control = mock.Mock(name="s3control")
    s3control.get_public_access_block.return_value = {
        "PublicAccessBlockConfiguration": {"RestrictPublicBuckets": True}
    }
    session = mock.Mock(name="session")
    session.client.side_effect = lambda service, **kwargs: {
        "sts": sts, "s3control": s3control
    }.get(service, mock.Mock(name=service))

    with mock.patch.object(s3mod, "get_session", return_value=session) as get_session:
        backend = _backend(opts.static_fm_id)
        result = backend._get_account_public_access_block()

    assert_eq(result, {"RestrictPublicBuckets": True},
              f"The account Public Access Block must be returned, got {result!r}")
    assert_eq(get_session.call_count, 1,
              "The public-access probe must reuse the backend session — a second "
              "session would audit a different identity than the bucket calls use")


@th.django_unit_test("S3 creds: a half-configured key pair fails with a readable error")
def test_test_connection_reports_partial_credentials(opts):
    backend = _backend(opts.halfpair_fm_id)
    try:
        backend.test_connection()
    except ValueError as err:
        assert_true("secret" in str(err).lower(),
                    f"The error must name the missing half of the pair, got {err!r}")
    except Exception as err:
        assert False, (
            f"A half-configured key pair must surface as a readable ValueError, "
            f"got {type(err).__name__}: {err}"
        )
    else:
        assert False, "A half-configured key pair must not report a working connection"


@th.django_unit_test("S3 creds: presigned URLs never outlive assumed-role credentials")
def test_presigned_url_expiry_is_clamped_under_a_role(opts):
    from mojo.apps.fileman.backends import s3 as s3mod

    credentials = mock.Mock(name="credentials")
    credentials._seconds_remaining.return_value = 600
    botocore_session = mock.Mock(name="botocore-session")
    botocore_session.get_credentials.return_value = credentials

    s3_client = mock.Mock(name="s3-client")
    s3_client.generate_presigned_url.return_value = "https://signed.example/object"
    session = _fake_session(s3_client=s3_client)
    session._session = botocore_session

    with mock.patch.object(s3mod, "get_assumed_session", return_value=session):
        backend = _backend(opts.role_fm_id)
        backend.get_url("fileman/prefix/object.txt", expires_in=3600)

    kwargs = s3_client.generate_presigned_url.call_args.kwargs
    assert_eq(kwargs["ExpiresIn"], 600,
              f"A URL signed with temporary credentials dies when they expire, so "
              f"ExpiresIn must be clamped to the remaining lifetime, got {kwargs!r}")


@th.django_unit_test("S3 creds: static-key presigned URLs keep the requested lifetime")
def test_presigned_url_expiry_is_untouched_without_a_role(opts):
    from mojo.apps.fileman.backends import s3 as s3mod

    s3_client = mock.Mock(name="s3-client")
    s3_client.generate_presigned_url.return_value = "https://signed.example/object"

    with mock.patch.object(s3mod, "get_session",
                           return_value=_fake_session(s3_client=s3_client)):
        backend = _backend(opts.static_fm_id)
        backend.get_url("fileman/prefix/object.txt", expires_in=3600)

    kwargs = s3_client.generate_presigned_url.call_args.kwargs
    assert_eq(kwargs["ExpiresIn"], 3600,
              f"Long-lived static credentials must not shorten a presigned URL, "
              f"got {kwargs!r}")
