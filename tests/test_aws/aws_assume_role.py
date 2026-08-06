"""Coverage for the cross-account AssumeRole session helper.

``get_assumed_session`` resolves a source identity through the normal
``get_session`` chain and then hands the botocore session a refreshable
assume-role credential. These tests exercise the wiring with fakes — no STS
call is ever made.
"""
from unittest import mock

from testit import helpers as th
from testit.helpers import assert_eq, assert_true

ROLE_ARN = "arn:aws:iam::123456789012:role/mojo-fileman"


class _FakeBotocoreSession:
    """Stand-in for boto3.Session._session."""

    def __init__(self, source="source-credentials"):
        self._source = source
        self._credentials = None
        self.created_clients = []

    def get_credentials(self):
        return self._source

    def create_client(self, *args, **kwargs):
        self.created_clients.append((args, kwargs))
        return mock.Mock(name="sts-client")


class _FakeSession:
    """Stand-in for boto3.Session."""

    def __init__(self, source="source-credentials"):
        self._session = _FakeBotocoreSession(source=source)


def _patched(source="source-credentials"):
    """Patch get_session + the botocore credential classes for one call."""
    from mojo.helpers.aws import client

    session = _FakeSession(source=source)
    return client, session, (
        mock.patch.object(client, "get_session", return_value=session),
        mock.patch.object(client, "AssumeRoleCredentialFetcher"),
        mock.patch.object(client, "DeferredRefreshableCredentials"),
    )


@th.django_unit_test("AssumeRole: the session gets a refreshable assume-role credential")
def test_assume_role_installs_refreshable_credentials(opts):
    client, session, patches = _patched()
    get_session_p, fetcher_p, deferred_p = patches

    with get_session_p as get_session, fetcher_p as fetcher_cls, deferred_p as deferred_cls:
        result = client.get_assumed_session(ROLE_ARN, region="us-west-2")

    assert_true(
        result is session,
        "get_assumed_session must return the same session object get_session built",
    )
    assert_eq(
        get_session.call_args.kwargs["region"], "us-west-2",
        "The region must reach the source session",
    )

    fetcher_kwargs = fetcher_cls.call_args.kwargs
    assert_eq(
        fetcher_kwargs["role_arn"], ROLE_ARN,
        "The fetcher must assume the requested role",
    )
    assert_true(
        fetcher_kwargs["source_credentials"] == "source-credentials",
        f"The source identity must be the resolved session credentials, got "
        f"{fetcher_kwargs['source_credentials']!r}",
    )
    assert_true(
        fetcher_kwargs["client_creator"] is session._session.create_client,
        "The fetcher must create its STS client from the source botocore session",
    )
    assert_true(
        fetcher_kwargs["cache"] is client._ASSUME_ROLE_CACHE,
        "The fetcher must share the module-level assume-role cache",
    )

    deferred_kwargs = deferred_cls.call_args.kwargs
    assert_eq(
        deferred_kwargs["method"], "assume-role",
        "The credential must be labeled as an assume-role credential",
    )
    assert_true(
        deferred_kwargs["refresh_using"] is fetcher_cls.return_value.fetch_credentials,
        "Refresh must go back through the fetcher — expiring role credentials are "
        "botocore's job, not a stale client's",
    )
    assert_true(
        session._session._credentials is deferred_cls.return_value,
        "The refreshable credential must be installed on the botocore session so "
        "clients created later pick up the assumed identity",
    )


@th.django_unit_test("AssumeRole: ExternalId is passed only when one is configured")
def test_external_id_is_passed_when_supplied(opts):
    client, session, patches = _patched()
    get_session_p, fetcher_p, deferred_p = patches

    with get_session_p, fetcher_p as fetcher_cls, deferred_p:
        client.get_assumed_session(ROLE_ARN, external_id="tenant-secret-42")

    extra_args = fetcher_cls.call_args.kwargs["extra_args"]
    assert_eq(
        extra_args.get("ExternalId"), "tenant-secret-42",
        f"A configured external id must reach STS, got {extra_args!r}",
    )


@th.django_unit_test("AssumeRole: ExternalId is omitted entirely when unset")
def test_external_id_is_absent_when_not_supplied(opts):
    client, session, patches = _patched()
    get_session_p, fetcher_p, deferred_p = patches

    with get_session_p, fetcher_p as fetcher_cls, deferred_p:
        client.get_assumed_session(ROLE_ARN)

    extra_args = fetcher_cls.call_args.kwargs["extra_args"]
    assert_true(
        "ExternalId" not in extra_args,
        f"STS rejects a null ExternalId — it must be omitted, not passed as None. "
        f"Got {extra_args!r}",
    )


@th.django_unit_test("AssumeRole: a default role session name and duration are applied")
def test_default_session_name_and_duration(opts):
    client, session, patches = _patched()
    get_session_p, fetcher_p, deferred_p = patches

    with get_session_p, fetcher_p as fetcher_cls, deferred_p:
        client.get_assumed_session(ROLE_ARN)

    extra_args = fetcher_cls.call_args.kwargs["extra_args"]
    assert_eq(
        extra_args.get("RoleSessionName"), client.DEFAULT_ROLE_SESSION_NAME,
        f"A default role session name must be supplied, got {extra_args!r}",
    )
    assert_eq(
        extra_args.get("DurationSeconds"), client.DEFAULT_ROLE_DURATION,
        f"A default role duration must be supplied, got {extra_args!r}",
    )


@th.django_unit_test("AssumeRole: an explicit session name and duration win")
def test_explicit_session_name_and_duration(opts):
    client, session, patches = _patched()
    get_session_p, fetcher_p, deferred_p = patches

    with get_session_p, fetcher_p as fetcher_cls, deferred_p:
        client.get_assumed_session(ROLE_ARN, session_name="fileman-7", duration="900")

    extra_args = fetcher_cls.call_args.kwargs["extra_args"]
    assert_eq(
        extra_args.get("RoleSessionName"), "fileman-7",
        f"An explicit role session name must win, got {extra_args!r}",
    )
    assert_eq(
        extra_args.get("DurationSeconds"), 900,
        f"An explicit duration must be coerced to an int, got {extra_args!r}",
    )


@th.django_unit_test("AssumeRole: an unresolvable source identity fails loudly")
def test_missing_source_identity_raises(opts):
    from botocore.exceptions import NoCredentialsError

    client, session, patches = _patched(source=None)
    get_session_p, fetcher_p, deferred_p = patches

    with get_session_p, fetcher_p, deferred_p:
        try:
            client.get_assumed_session(ROLE_ARN)
        except NoCredentialsError:
            pass
        else:
            assert False, (
                "A session with no resolvable source identity must raise "
                "NoCredentialsError, not fail later inside the fetcher"
            )


@th.django_unit_test("AssumeRole: an ambient source identity needs no static keys")
def test_ambient_source_identity(opts):
    client, session, patches = _patched()
    get_session_p, fetcher_p, deferred_p = patches

    with get_session_p as get_session, fetcher_p, deferred_p:
        client.get_assumed_session(ROLE_ARN)

    kwargs = get_session.call_args.kwargs
    assert_true(
        kwargs["access_key"] is None and kwargs["secret_key"] is None,
        f"With no keys configured the source identity must fall through to the "
        f"botocore default chain, got {kwargs!r}",
    )
