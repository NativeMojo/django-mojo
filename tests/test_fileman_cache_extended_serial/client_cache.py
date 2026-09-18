"""S3 client construction must scale with storage configurations, not rows."""
from unittest import mock
from uuid import uuid4

from testit import helpers as th


@th.django_unit_test("S3 cache: separately loaded managers construct one client")
def test_fresh_managers_share_client(opts):
    from mojo.apps.fileman.backends import s3 as s3mod
    from mojo.apps.fileman.models import FileManager

    name = "fm_client_cache_rows"
    FileManager.objects.filter(name=name).delete()
    manager = FileManager.objects.create(
        name=name, backend_type="s3", backend_url="s3://cache-test/prefix")
    manager.set_secret("aws_key", "cache-test-key")
    manager.set_secret("aws_secret", uuid4().hex)
    manager.save()
    session = mock.Mock()
    session.client.return_value.generate_presigned_url.return_value = "https://signed.example/file"
    try:
        with mock.patch.object(s3mod, "get_session", return_value=session) as factory:
            backends = [FileManager.objects.get(pk=manager.pk).backend for _ in range(20)]
            urls = [backend.get_url("prefix/file", expires_in=3600) for backend in backends]
            assert len(urls) == 20, "Every row must still receive a URL"
            assert factory.call_count == 1, "Twenty ORM instances must construct one session"
            assert session.client.call_count == 1, "Twenty ORM instances must construct one client"
            assert all(b.client is backends[0].client for b in backends), "Clients must be reused"
    finally:
        manager.delete()


def _manager(pk=None, database="default", bucket="cache-test", **settings):
    from types import SimpleNamespace

    config = {"aws_key": "cache-test-key", "aws_secret": "cache-test-secret"}
    config.update(settings)
    return SimpleNamespace(
        pk=pk or uuid4().hex,
        _state=SimpleNamespace(db=database),
        backend_url=f"s3://{bucket}/prefix",
        primary_settings=config,
    )


@th.django_unit_test("S3 cache: manager and effective configuration stay isolated")
def test_configuration_separation(opts):
    from mojo.apps.fileman.backends import s3 as s3mod

    manager_id = uuid4().hex
    variants = [
        {},
        {"pk": uuid4().hex},
        {"database": "other"},
        {"bucket": "other-bucket"},
        {"aws_region": "eu-west-1"},
        {"endpoint_url": "https://storage.example.test"},
        {"aws_key": "different-key"},
        {"aws_secret": "rotated-secret"},
        {"signature_version": "s3"},
        {"addressing_style": "path"},
        {"assume_role_arn": "arn:aws:iam::123456789012:role/first"},
        {"assume_role_arn": "arn:aws:iam::123456789012:role/second"},
        {"assume_role_arn": "arn:aws:iam::123456789012:role/first", "external_id": "tenant-two"},
        {"assume_role_arn": "arn:aws:iam::123456789012:role/first", "role_session_name": "other-session"},
        {"assume_role_arn": "arn:aws:iam::123456789012:role/first", "assume_role_duration": 3600},
    ]
    clients = []
    with mock.patch.object(s3mod, "get_session", side_effect=lambda **kw: mock.Mock()), \
            mock.patch.object(s3mod, "get_assumed_session", side_effect=lambda *args, **kw: mock.Mock()):
        for variant in variants:
            values = {"pk": manager_id, **variant}
            backend = s3mod.S3StorageBackend(_manager(**values))
            client = backend.client
            assert all(client is not previous for previous in clients), f"Configuration must isolate client: {list(variant)}"
            twin = s3mod.S3StorageBackend(_manager(**values))
            assert twin.client is client, "Identical configuration must reuse its own client"
            clients.append(client)


@th.django_unit_test("S3 cache: rotated credentials change the URL signing identity")
def test_rotated_credentials_sign_new_urls(opts):
    import boto3
    from urllib.parse import parse_qs, urlparse
    from mojo.apps.fileman.backends import s3 as s3mod

    def session_factory(access_key, secret_key, region):
        return boto3.Session(aws_access_key_id=access_key,
                             aws_secret_access_key=secret_key, region_name=region)

    manager_id = uuid4().hex
    with mock.patch.object(s3mod, "get_session", side_effect=session_factory) as factory:
        old = s3mod.S3StorageBackend(_manager(pk=manager_id, aws_key="OLDKEY"))
        new = s3mod.S3StorageBackend(_manager(pk=manager_id, aws_key="NEWKEY"))
        for backend, expected in ((old, "OLDKEY"), (new, "NEWKEY")):
            url = backend.get_url("prefix/file", expires_in=3600)
            credential = parse_qs(urlparse(url).query)["X-Amz-Credential"][0]
            assert credential.startswith(expected + "/"), "URL must use the manager's current signing identity"
        assert factory.call_count == 2, "Credential rotation must create a new signing session"


@th.django_unit_test("S3 cache: simultaneous misses construct exactly one client")
def test_concurrent_client_creation(opts):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from mojo.apps.fileman.backends import s3 as s3mod

    manager_id = uuid4().hex
    backends = [s3mod.S3StorageBackend(_manager(pk=manager_id)) for _ in range(8)]
    ready = Barrier(len(backends))
    session = mock.Mock()

    def get_client(backend):
        ready.wait(timeout=10)
        return backend.client

    with mock.patch.object(s3mod, "get_session", return_value=session) as factory:
        with ThreadPoolExecutor(max_workers=len(backends)) as pool:
            clients = list(pool.map(get_client, backends))
        assert factory.call_count == 1, "Concurrent cache misses must construct one session"
        assert session.client.call_count == 1, "Concurrent cache misses must construct one client"
        assert all(client is clients[0] for client in clients), "All callers must receive the shared client"


@th.django_unit_test("S3 cache: eviction bounds retained clients without closing active clients")
def test_cache_eviction(opts):
    from collections import OrderedDict
    from mojo.apps.fileman.backends import s3 as s3mod

    first_manager = _manager()
    sessions = [mock.Mock() for _ in range(4)]
    with mock.patch.object(s3mod, "_S3_CLIENT_CACHE", OrderedDict()), \
            mock.patch.object(s3mod, "_S3_CLIENT_CACHE_SIZE", 2), \
            mock.patch.object(s3mod, "get_session", side_effect=sessions) as factory:
        first = s3mod.S3StorageBackend(first_manager)
        original = first.client
        second = s3mod.S3StorageBackend(_manager()).client
        third = s3mod.S3StorageBackend(_manager()).client
        reloaded = s3mod.S3StorageBackend(first_manager).client
        assert factory.call_count == 4, "An evicted manager must rebuild on its next fresh load"
        assert len(s3mod._S3_CLIENT_CACHE) <= 2, "The cache must respect its configured capacity"
        assert reloaded is not original, "The oldest cached entry must be evicted"
        assert first.client is original, "Eviction must preserve already active backend clients"
        for client in (original, second, third):
            assert not client.close.called, "Eviction must not close clients another request may still use"


@th.django_unit_test("S3 cache: failed construction can be retried")
def test_failed_construction_retries(opts):
    from mojo.apps.fileman.backends import s3 as s3mod

    session = mock.Mock()
    client = mock.Mock()
    session.client.side_effect = [RuntimeError("local construction failure"), client]
    backend = s3mod.S3StorageBackend(_manager())
    with mock.patch.object(s3mod, "get_session", return_value=session):
        try:
            backend.client
        except RuntimeError:
            pass
        else:
            raise AssertionError("The initial construction error must reach the caller")
        assert backend.client is client, "A failed build must not poison subsequent requests"
        twin = s3mod.S3StorageBackend(backend.file_manager)
        assert twin.client is client, "The successful retry must become reusable"
        assert session.client.call_count == 2, "Only the failed build and successful retry should run"


@th.django_unit_test("S3 cache: resources remain private to each backend")
def test_resources_are_backend_local(opts):
    from mojo.apps.fileman.backends import s3 as s3mod

    manager = _manager()
    session = mock.Mock()
    session.resource.side_effect = [mock.Mock(), mock.Mock()]
    with mock.patch.object(s3mod, "get_session", return_value=session) as factory:
        first = s3mod.S3StorageBackend(manager)
        second = s3mod.S3StorageBackend(manager)
        first_resource = first.resource
        second_resource = second.resource
        assert first_resource is not second_resource, "Boto3 resources must not be shared across backends"
        assert first.resource is first_resource, "Each backend must retain its own resource"
        assert first.client is second.client, "Separate resources must still share the safe low-level client"
        assert factory.call_count == 1, "Resource construction must use the shared session"
        assert session.resource.call_count == 2, "Each backend must construct exactly one resource"


@th.django_unit_test("S3 cache: shared assumed credentials refresh and bound signed URL lifetime")
def test_assumed_credentials_refresh(opts):
    import boto3
    from datetime import datetime, timedelta, timezone
    from urllib.parse import parse_qs, urlparse
    from botocore.credentials import DeferredRefreshableCredentials
    from mojo.apps.fileman.backends import s3 as s3mod

    now = [datetime.now(timezone.utc)]
    refreshes = []

    def refresh():
        refreshes.append(len(refreshes) + 1)
        return {
            "access_key": f"ROLEKEY{len(refreshes)}",
            "secret_key": "local-refresh-secret",
            "token": f"local-token-{len(refreshes)}",
            "expiry_time": (now[0] + timedelta(hours=1)).isoformat(),
        }

    session = boto3.Session(aws_access_key_id="SOURCE", aws_secret_access_key="source-secret",
                            region_name="us-east-1")
    session._session._credentials = DeferredRefreshableCredentials(
        refresh_using=refresh, method="assume-role", time_fetcher=lambda: now[0])
    manager = _manager(assume_role_arn="arn:aws:iam::123456789012:role/cache-test")
    with mock.patch.object(s3mod, "get_assumed_session", return_value=session) as factory:
        first = s3mod.S3StorageBackend(manager)
        first_query = parse_qs(urlparse(first.get_url("prefix/file", expires_in=7200)).query)
        assert first_query["X-Amz-Credential"][0].startswith("ROLEKEY1/"), "Initial signing must resolve deferred role credentials"
        assert 1 <= int(first_query["X-Amz-Expires"][0]) <= 3600, "Signed URL must not outlive role credentials"
        now[0] += timedelta(minutes=55)
        second = s3mod.S3StorageBackend(manager)
        next_query = parse_qs(urlparse(second.get_url("prefix/file", expires_in=7200)).query)
        assert next_query["X-Amz-Credential"][0].startswith("ROLEKEY2/"), "Cached client must sign with refreshed credentials"
        assert next_query["X-Amz-Security-Token"] == ["local-token-2"], "Refreshed token must accompany the new identity"
        assert 1 <= int(next_query["X-Amz-Expires"][0]) <= 3600, "Refreshed credentials must still bound URL lifetime"
        assert second.client is first.client, "Refreshing credentials must not reconstruct the client"
        assert factory.call_count == 1, "Fresh ORM backends must reuse the assumed session"
        assert len(refreshes) == 2, "Credentials should refresh once when the refresh window is reached"


@th.django_unit_test("S3 cache: inherited backends replace their process-owned clients and resources")
def test_inherited_backend_rebuilds(opts):
    from mojo.apps.fileman.backends import s3 as s3mod

    parent_session = mock.Mock()
    child_session = mock.Mock()
    backend = s3mod.S3StorageBackend(_manager())
    with mock.patch.object(s3mod, "get_session", side_effect=[parent_session, child_session]) as factory:
        parent_client = backend.client
        parent_resource = backend.resource
        # Exercise the registered child hook and instance guard without forking
        # the multithreaded test runner or changing process-wide os.getpid.
        s3mod._reset_client_cache_after_fork()
        backend._process_id = -1
        assert backend.client is not parent_client, "An inherited backend must replace the parent's client"
        assert backend.resource is not parent_resource, "An inherited backend must replace the parent's resource"
        assert backend._build_session() is child_session, "Expiry checks must use the new process's session"
        assert factory.call_count == 2, "The child must build one replacement session"
        twin = s3mod.S3StorageBackend(backend.file_manager)
        assert twin.client is backend.client, "Fresh child backends must reuse the replacement client"
