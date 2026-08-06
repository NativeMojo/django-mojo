"""Downstream ACME hub transport: strict wire, config and retry contracts."""

import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

from testit import helpers as th


HUB_URL = "https://hub.example.net"
HUB_KEY = "hub-test-key"
CLIENT_REF = "install-123"
CHALLENGE_REF = "order-456"
DOMAIN = "customer.example"
SOURCE = "_acme-challenge.customer.example"
TARGET = "0123456789abcdef.hub.example.net"


class FakeResponse(object):
    def __init__(self, payload=None, status_code=200, content_type="application/json",
                 content=None, headers=None):
        if content is None:
            content = json.dumps(payload).encode("utf-8")
        self.status_code = status_code
        self.content = content
        self.headers = dict(headers or {})
        self.closed = False
        if content_type is not None:
            self.headers["Content-Type"] = content_type

    def iter_content(self, chunk_size=1):
        for start in range(0, len(self.content), chunk_size):
            yield self.content[start:start + chunk_size]

    def close(self):
        self.closed = True


def _envelope(data, **extra):
    payload = {"status": True, "code": 200, "data": data, "server": "hub-test"}
    payload.update(extra)
    return payload


def _allocation_data(**overrides):
    data = {
        "client_ref": CLIENT_REF,
        "domain": DOMAIN,
        "source": SOURCE,
        "target": TARGET,
    }
    data.update(overrides)
    return data


def _challenge_data(**overrides):
    data = _allocation_data(
        challenge_ref=CHALLENGE_REF,
        active_value_count=1,
    )
    data.update(overrides)
    return data


@contextmanager
def _hub_settings(**overrides):
    from django.conf import settings as django_settings

    values = {
        "DNSMAN_ACME_HUB_URL": HUB_URL,
        "DNSMAN_ACME_HUB_API_KEY": HUB_KEY,
        "DNSMAN_ACME_HUB_CONNECT_TIMEOUT": 2,
        "DNSMAN_ACME_HUB_READ_TIMEOUT": 7,
        "DNSMAN_ACME_HUB_RETRIES": 1,
    }
    values.update(overrides)
    missing = object()
    old = {name: getattr(django_settings, name, missing) for name in values}
    try:
        for name, value in values.items():
            setattr(django_settings, name, value)
        yield
    finally:
        for name, value in old.items():
            if value is missing:
                delattr(django_settings, name)
            else:
                setattr(django_settings, name, value)


def _caught(error_class, func):
    try:
        func()
    except error_class as error:
        return error
    except Exception as error:
        assert False, (
            f"expected {error_class.__name__}, got {type(error).__name__}: {error}")
    assert False, f"expected {error_class.__name__} to be raised"


@th.django_unit_test("ACME hub client sends one exact authenticated allocation request")
def test_allocate_exact_request(opts):
    from mojo.apps.dnsman.services import acme_hub_client as client

    response = FakeResponse(_envelope(_allocation_data()))
    with _hub_settings(), mock.patch.object(
            client.requests, "post", return_value=response) as post:
        result = client.allocate("Customer.Example.", CLIENT_REF)

    assert isinstance(result, client.AcmeHubAllocation), \
        f"allocate must return the typed validated allocation, got {type(result).__name__}"
    assert result.success is True, "a validated allocation must report success"
    assert result.domain == DOMAIN, f"expected normalized domain {DOMAIN}, got {result.domain}"
    assert result.source == SOURCE, f"expected exact challenge source {SOURCE}, got {result.source}"
    assert result.target == TARGET, f"expected opaque target {TARGET}, got {result.target}"
    assert post.call_count == 1, f"a successful allocation should make one call, got {post.call_count}"
    args, kwargs = post.call_args
    assert args == (f"{HUB_URL}/api/dnsman/acme/delegation",), \
        f"allocation must use only the fixed endpoint, got {args}"
    assert kwargs["json"] == {"domain": DOMAIN, "client_ref": CLIENT_REF}, \
        f"allocation payload must be exact, got {kwargs['json']}"
    assert kwargs["headers"] == {
        "Authorization": f"apikey {HUB_KEY}", "Accept": "application/json"}, \
        f"allocation must send only the ApiKey auth and JSON accept headers, got {kwargs['headers']}"
    assert kwargs["timeout"] == (2.0, 7.0), \
        f"connect/read timeout tuple must be bounded and distinct, got {kwargs['timeout']}"
    assert kwargs["allow_redirects"] is False, "the ApiKey-bearing request must never follow redirects"
    assert kwargs["stream"] is True and response.closed is True, \
        "the response must be streamed through the byte cap and closed"


@th.django_unit_test("ACME hub publish and withdraw carry immutable references only")
def test_publish_withdraw_wire_shapes(opts):
    from mojo.apps.dnsman.services import acme_hub_client as client

    publish_response = FakeResponse(_envelope(_challenge_data(active_value_count=2)))
    withdraw_response = FakeResponse(_envelope(_challenge_data(active_value_count=0)))
    with _hub_settings(), mock.patch.object(
            client.requests, "post", side_effect=[publish_response, withdraw_response]) as post:
        published = client.publish(
            CLIENT_REF, CHALLENGE_REF, ["digest-b", "digest-a", "digest-a"])
        withdrawn = client.withdraw(CLIENT_REF, CHALLENGE_REF)

    assert isinstance(published, client.AcmeHubChallengeResult), \
        f"publish must return the typed challenge result, got {type(published).__name__}"
    assert published.success is True and published.active_value_count == 2, \
        f"publish should return validated success/count, got {published}"
    assert withdrawn.success is True and withdrawn.active_value_count == 0, \
        f"withdraw should return validated idempotent success/count, got {withdrawn}"
    publish_args, publish_kwargs = post.call_args_list[0]
    assert publish_args == (f"{HUB_URL}/api/dnsman/acme/challenge/publish",), \
        f"publish must use the fixed endpoint, got {publish_args}"
    assert publish_kwargs["json"] == {
        "client_ref": CLIENT_REF,
        "challenge_ref": CHALLENGE_REF,
        "values": ["digest-a", "digest-b"],
    }, f"publish must send the complete normalized immutable lease, got {publish_kwargs['json']}"
    withdraw_args, withdraw_kwargs = post.call_args_list[1]
    assert withdraw_args == (f"{HUB_URL}/api/dnsman/acme/challenge/withdraw",), \
        f"withdraw must use the fixed endpoint, got {withdraw_args}"
    assert withdraw_kwargs["json"] == {
        "client_ref": CLIENT_REF, "challenge_ref": CHALLENGE_REF}, \
        f"withdraw must address only the immutable lease, got {withdraw_kwargs['json']}"


@th.django_unit_test("ACME hub config is file-only, strict and explicitly discoverable")
def test_config_file_only_and_url_safety(opts):
    from mojo.apps.dnsman.services import acme_hub_client as client

    with _hub_settings(DNSMAN_ACME_HUB_URL=None, DNSMAN_ACME_HUB_API_KEY=None):
        assert client.is_available() is False, \
            "the capability must report unavailable when both file settings are absent"

    with _hub_settings(DNSMAN_ACME_HUB_URL=HUB_URL, DNSMAN_ACME_HUB_API_KEY=None):
        error = _caught(client.AcmeHubConfigurationError, client.is_available)
    assert "API_KEY" in str(error), f"partial configuration should name the missing key, got {error}"

    bad_urls = [
        "http://hub.example.net",
        "https://user:secret@hub.example.net",
        "https://hub.example.net/prefix",
        "https://hub.example.net?redirect=elsewhere",
        "https://hub.example.net#fragment",
        "https://hub.example.net\\@elsewhere.example",
        " https://hub.example.net",
    ]
    for url in bad_urls:
        with _hub_settings(DNSMAN_ACME_HUB_URL=url):
            error = _caught(client.AcmeHubConfigurationError, client.is_available)
        assert url not in str(error), "unsafe configured URLs must never be reflected into errors"

    bad_config = [
        {"DNSMAN_ACME_HUB_API_KEY": " key-with-space"},
        {"DNSMAN_ACME_HUB_CONNECT_TIMEOUT": 0},
        {"DNSMAN_ACME_HUB_CONNECT_TIMEOUT": "nan"},
        {"DNSMAN_ACME_HUB_READ_TIMEOUT": 121},
        {"DNSMAN_ACME_HUB_RETRIES": 2},
    ]
    for overrides in bad_config:
        with _hub_settings(**overrides):
            _caught(client.AcmeHubConfigurationError, client.is_available)

    response = FakeResponse(_envelope(_allocation_data()))
    with _hub_settings(DNSMAN_ACME_HUB_URL="http://127.0.0.1:8765"), \
            mock.patch.object(client.requests, "post", return_value=response) as post:
        client.allocate(DOMAIN, CLIENT_REF)
    assert post.call_args.args[0].startswith("http://127.0.0.1:8765/"), \
        "plain HTTP must be allowed only for the explicit loopback development origin"

    class StaticOnly(object):
        def __init__(self):
            self.calls = []

        def get_static(self, name, default=None):
            self.calls.append(name)
            values = {
                "DNSMAN_ACME_HUB_URL": HUB_URL,
                "DNSMAN_ACME_HUB_API_KEY": HUB_KEY,
            }
            return values.get(name, default)

        def get(self, *args, **kwargs):
            assert False, "ACME hub client configuration must never use the DB-backed settings.get"

    static = StaticOnly()
    with mock.patch.object(client, "settings", static), \
            mock.patch.object(client.requests, "post", return_value=response):
        client.allocate(DOMAIN, CLIENT_REF)
    assert "DNSMAN_ACME_HUB_URL" in static.calls and "DNSMAN_ACME_HUB_API_KEY" in static.calls, \
        f"required settings must be read at call time via get_static, got {static.calls}"


@th.django_unit_test("ACME hub retries only bounded idempotent ambiguity and gateway failures")
def test_bounded_retry_policy(opts):
    import requests as real_requests
    from mojo.apps.dnsman.services import acme_hub_client as client

    success = FakeResponse(_envelope(_allocation_data()))
    with _hub_settings(), mock.patch.object(
            client.requests, "post",
            side_effect=[real_requests.exceptions.ReadTimeout("ambiguous"), success]) as post:
        result = client.allocate(DOMAIN, CLIENT_REF)
    assert result.success is True and post.call_count == 2, \
        f"one read ambiguity should retry the identical allocation once, got {post.call_count} calls"
    first = post.call_args_list[0]
    second = post.call_args_list[1]
    assert first == second, "a retry must replay the exact same URL, headers, payload and timeouts"

    broken_body = FakeResponse(_envelope(_allocation_data()))
    broken_body.iter_content = mock.Mock(
        side_effect=real_requests.exceptions.ChunkedEncodingError("partial response"))
    success_after_body = FakeResponse(_envelope(_allocation_data()))
    with _hub_settings(), mock.patch.object(
            client.requests, "post", side_effect=[broken_body, success_after_body]) as post:
        result = client.allocate(DOMAIN, CLIENT_REF)
    assert result.success is True and post.call_count == 2, \
        "a partial/read-ambiguous response body should retry the identical request once"
    assert broken_body.closed is True, "a partial response must be closed before retrying"

    unavailable = FakeResponse({"ignored": True}, status_code=503)
    with _hub_settings(), mock.patch.object(
            client.requests, "post", side_effect=[unavailable, success]) as post:
        client.allocate(DOMAIN, CLIENT_REF)
    assert post.call_count == 2, "HTTP 503 should be retried exactly once"

    for status in (400, 401, 403, 409, 429):
        rejected = FakeResponse({"ignored": True}, status_code=status)
        with _hub_settings(), mock.patch.object(
                client.requests, "post", return_value=rejected) as post:
            error = _caught(
                client.AcmeHubResponseError,
                lambda: client.allocate(DOMAIN, CLIENT_REF))
        assert post.call_count == 1, f"HTTP {status} must never be retried"
        assert error.http_status == status and error.retriable is False, \
            f"HTTP {status} should map to a typed non-retriable failure, got {error.__dict__}"

    with _hub_settings(), mock.patch.object(
            client.requests, "post",
            side_effect=real_requests.exceptions.ConnectionError("still ambiguous")) as post:
        error = _caught(
            client.AcmeHubTransportError,
            lambda: client.allocate(DOMAIN, CLIENT_REF))
    assert post.call_count == 2, f"transport ambiguity must stop after one retry, got {post.call_count}"
    assert error.retriable is True, "an exhausted ambiguous transport failure should stay retriable"


@th.django_unit_test("ACME hub refuses redirects and malformed or cross-request replies")
def test_strict_response_validation(opts):
    from mojo.apps.dnsman.services import acme_hub_client as client

    redirect = FakeResponse({}, status_code=302, headers={"Location": "https://elsewhere.example"})
    with _hub_settings(), mock.patch.object(client.requests, "post", return_value=redirect) as post:
        error = _caught(
            client.AcmeHubResponseError,
            lambda: client.allocate(DOMAIN, CLIENT_REF))
    assert post.call_count == 1 and error.kind == "redirect", \
        "redirects must be refused without a second ApiKey-bearing request"

    malformed = [
        FakeResponse(None, content=b"not-json"),
        FakeResponse({}, content_type="text/html", content=b"{}"),
        FakeResponse([], content=json.dumps([]).encode()),
        FakeResponse({"status": True, "code": 200, "data": _allocation_data(), "extra": True}),
        FakeResponse(_envelope(_allocation_data()), content=b"{}", headers={
            "Content-Length": str(client.MAX_RESPONSE_BYTES + 1)}),
    ]
    for response in malformed:
        with _hub_settings(), mock.patch.object(client.requests, "post", return_value=response):
            _caught(
                client.AcmeHubResponseError,
                lambda: client.allocate(DOMAIN, CLIENT_REF))

    wrong_replies = [
        _allocation_data(client_ref="another-install"),
        _allocation_data(domain="another.example", source="_acme-challenge.another.example"),
        _allocation_data(source="_acme-challenge.attacker.example"),
        _allocation_data(target=SOURCE),
        _allocation_data(target="UPPER.hub.example.net"),
    ]
    for data in wrong_replies:
        with _hub_settings(), mock.patch.object(
                client.requests, "post", return_value=FakeResponse(_envelope(data))):
            _caught(
                client.AcmeHubResponseError,
                lambda: client.allocate(DOMAIN, CLIENT_REF))

    wrong_challenge = _challenge_data(challenge_ref="another-order")
    with _hub_settings(), mock.patch.object(
            client.requests, "post", return_value=FakeResponse(_envelope(wrong_challenge))):
        _caught(
            client.AcmeHubResponseError,
            lambda: client.publish(CLIENT_REF, CHALLENGE_REF, ["digest-a"]))


@th.django_unit_test("ACME hub failures remain typed, actionable and secret-safe")
def test_safe_typed_failures(opts):
    import requests as real_requests
    from mojo.apps.dnsman.services import acme_hub_client as client

    secret_key = "key-that-must-never-appear"
    secret_value = "txt-value-that-must-never-appear"
    unsafe_url = "https://sensitive-host.example.net"
    leaked_exception = real_requests.exceptions.ConnectionError(
        f"wire failure {secret_key} {secret_value} {unsafe_url}")
    with _hub_settings(
            DNSMAN_ACME_HUB_URL=unsafe_url,
            DNSMAN_ACME_HUB_API_KEY=secret_key), \
            mock.patch.object(client.requests, "post", side_effect=leaked_exception):
        error = _caught(
            client.AcmeHubTransportError,
            lambda: client.publish(CLIENT_REF, CHALLENGE_REF, [secret_value]))
    rendered = str(error)
    assert error.kind == "transport" and error.retriable is True, \
        f"transport failures must carry typed retry metadata, got {error.__dict__}"
    assert secret_key not in rendered, "the configured ApiKey must never appear in a failure"
    assert secret_value not in rendered, "TXT values must never appear in a failure"
    assert unsafe_url not in rendered, "the configured URL must never appear unsanitized in a failure"

    failure = FakeResponse({
        "status": False,
        "code": 403,
        "error": f"remote leaked {secret_key} {secret_value}",
        "server": "hub-test",
    })
    with _hub_settings(), mock.patch.object(client.requests, "post", return_value=failure):
        error = _caught(
            client.AcmeHubResponseError,
            lambda: client.publish(CLIENT_REF, CHALLENGE_REF, [secret_value]))
    assert str(error) == "ACME hub authentication was rejected (HTTP 403)", \
        f"remote error bodies must map to one bounded safe message, got {error}"


@th.django_unit_test("ACME hub client never becomes a DnsProvider or adapter")
def test_not_a_general_dns_provider(opts):
    from mojo.apps.dnsman.services import acme_hub_client as client
    from mojo.apps.dnsman.services import dns
    from mojo.apps.dnsman.services.providers import DnsProvider, Route53Provider

    client_classes = [
        value for value in vars(client).values()
        if isinstance(value, type) and value.__module__ == client.__name__]
    assert not any(issubclass(value, DnsProvider) for value in client_classes), \
        "the challenge-specific hub transport must never implement DnsProvider"
    assert not hasattr(client, "list_records") and not hasattr(client, "upsert_record"), \
        "the hub transport must expose no arbitrary DNS read/write surface"

    domain = SimpleNamespace(
        provider="route53", name="direct.example", status="active",
        hosted_zone_id="ZONE-DIRECT")
    with mock.patch.object(client, "allocate") as allocate:
        adapter = dns.get_adapter(domain)
    assert isinstance(adapter, Route53Provider), \
        f"direct Route53 dispatch must remain unchanged, got {type(adapter).__name__}"
    assert not allocate.called, "direct provider selection must never consult the ACME hub client"
