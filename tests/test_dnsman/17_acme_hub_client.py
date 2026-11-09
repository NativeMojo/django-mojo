"""Downstream ACME hub transport: strict wire, config and retry contracts."""

TESTIT_TIER = "extended"

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




def _caught(error_class, func):
    try:
        func()
    except error_class as error:
        return error
    except Exception as error:
        assert False, (
            f"expected {error_class.__name__}, got {type(error).__name__}: {error}")
    assert False, f"expected {error_class.__name__} to be raised"














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


PROJECT_UUID = "0123456789abcdef0123456789abcdef"
PARTICIPANT_REF = "12345678-1234-4234-8234-123456789abc"
STAMP = "2026-10-20T12:00:00+00:00"


def _participant_data(controller=False, **overrides):
    data = _allocation_data(
        allocation_ref=TARGET, participant_ref=PARTICIPANT_REF, role="trusted",
        trust_state="active", scope=[DOMAIN, "*." + DOMAIN], can_publish=True,
        can_withdraw=True, revoked_at=None)
    if controller:
        data.update(consumer_project_uuid=PROJECT_UUID, consumer_label="Project")
    data.update(overrides)
    return data


def _capability_data(**overrides):
    data = dict(version=2, shared_validation=True, consumer_project_uuid=PROJECT_UUID,
                can_consume=True, can_manage_trust=True, trust_scope="apex_and_wildcard")
    data.update(overrides)
    return data


@th.django_unit_test("Client accepts existing short project identities without relaxing participant UUIDs")
def test_legacy_project_identity_client_contract(opts):
    from mojo.apps.dnsman.services import acme_hub_client as client
    project = "74a3c6699bed4ddf"
    caps = client.capabilities(transport=lambda *args: _capability_data(consumer_project_uuid=project))
    assert caps.consumer_project_uuid == project, "Authenticated capabilities must preserve the legacy project identity"
    allocations = client.list_allocations(DOMAIN, transport=lambda *args: dict(items=[dict(
        allocation_ref=TARGET, domain=DOMAIN, source=SOURCE, target=TARGET,
        owner_project_uuid=project, can_manage_trust=True)], next_cursor=None))
    assert allocations["items"][0].owner_project_uuid == project, "Controller allocation DTOs must accept an existing short owner identity"
    recipient = client.resolve_recipient(TARGET, project, transport=lambda operation, payload: dict(
        consumer_project_uuid=payload["consumer_project_uuid"], label="Original", active=True))
    assert recipient.consumer_project_uuid == project, "Recipient request and response must preserve exact project bytes"
    descriptor = dict(version=1, consumer_project_uuid=project, client_ref=CLIENT_REF, domain=DOMAIN, allocation_ref=TARGET)
    granted = client.grant(descriptor, transport=lambda *args: _participant_data(controller=True, consumer_project_uuid=project))
    assert granted.consumer_project_uuid == project and granted.participant_ref == PARTICIPANT_REF, "Legacy project grants must retain the full participant UUID"
    for invalid in (project.upper(), " " + project, project[:-1], project + "0", "g" * 16):
        _caught(client.AcmeHubResponseError, lambda: client.capabilities(transport=lambda *args: _capability_data(consumer_project_uuid=invalid)))
    _caught(client.AcmeHubResponseError, lambda: client.lookup(DOMAIN, TARGET, transport=lambda *args: dict(
        items=[_participant_data(participant_ref=project)], next_cursor=None)))
    _caught(client.AcmeHubRequestError, lambda: client.revoke(TARGET, project, transport=lambda *args: None))


def _alias_data(**overrides):
    data = dict(state="match", observed_target=TARGET, one_hop=True, checked_at=STAMP)
    data.update(overrides)
    return data


@th.django_unit_test("Shared ACME client dispatches every v2 operation through fixed paths and exact payloads")
def test_shared_fixed_operations(opts):
    from mojo.apps.dnsman.services import acme_hub_client as client
    descriptor = dict(version=1, consumer_project_uuid=PROJECT_UUID, client_ref=CLIENT_REF,
                      domain=DOMAIN, allocation_ref=TARGET)
    cases = [
        ("capabilities", "/capabilities", {}, _capability_data(), lambda transport: client.capabilities(transport=transport)),
        ("lookup", "/consumer/lookup", {"domain": DOMAIN, "allocation_ref": TARGET, "cursor": "5"},
         dict(items=[_participant_data()], next_cursor=None), lambda transport: client.lookup(DOMAIN, TARGET, "5", transport=transport)),
        ("status", "/consumer/status", {"client_ref": CLIENT_REF}, _participant_data(alias=_alias_data()),
         lambda transport: client.status(CLIENT_REF, transport=transport)),
        ("join", "/consumer/join", {"domain": DOMAIN, "client_ref": CLIENT_REF, "allocation_ref": TARGET},
         _allocation_data(), lambda transport: client.join(DOMAIN, CLIENT_REF, TARGET, transport=transport)),
        ("list_allocations", "/allocation/list", {"domain": DOMAIN},
         dict(items=[dict(allocation_ref=TARGET, domain=DOMAIN, source=SOURCE, target=TARGET,
                         owner_project_uuid=PROJECT_UUID, can_manage_trust=True)], next_cursor=None),
         lambda transport: client.list_allocations(DOMAIN, transport=transport)),
        ("list_participants", "/trust/list", {"allocation_ref": TARGET},
         dict(items=[_participant_data(controller=True)], next_cursor=None),
         lambda transport: client.list_participants(TARGET, transport=transport)),
        ("resolve_recipient", "/trust/recipient", {"allocation_ref": TARGET, "consumer_project_uuid": PROJECT_UUID},
         dict(consumer_project_uuid=PROJECT_UUID, label="Project", active=True),
         lambda transport: client.resolve_recipient(TARGET, PROJECT_UUID, transport=transport)),
        ("grant", "/trust/grant", {"connection_request": descriptor}, _participant_data(controller=True),
         lambda transport: client.grant(descriptor, transport=transport)),
        ("revoke", "/trust/revoke", {"allocation_ref": TARGET, "participant_ref": PARTICIPANT_REF},
         _participant_data(controller=True, trust_state="revoked", revoked_at=STAMP, can_publish=False),
         lambda transport: client.revoke(TARGET, PARTICIPANT_REF, transport=transport)),
    ]
    config = client._Config(HUB_URL, HUB_KEY, 5, 30, 0)
    for operation, path, payload, result, call in cases:
        seen = []
        def post(url, **kwargs):
            seen.append((url, kwargs))
            return FakeResponse(_envelope(result))
        def transport(name, data):
            assert name == operation and data == payload, f"wrong operation or request shape: {name}, {data}"
            return client._wire(name, data, config, post)
        parsed = call(transport)
        assert parsed is not None, f"{operation} must return typed validated data"
        url, options = seen[0]
        assert url == HUB_URL + "/api/dnsman/acme" + path, f"{operation} escaped its fixed path: {url}"
        assert options["allow_redirects"] is False and options["stream"] is True, "new operations retain redirect and size defenses"
        assert options["timeout"] == (5, 30), "new operations retain bounded connection/read budget"
        assert options["headers"]["Authorization"] == "apikey " + HUB_KEY, "wire must use configured protected key"


@th.django_unit_test("Shared ACME client rejects unknown keys, forged scope and cross-request participants")
def test_shared_strict_participant_parsing(opts):
    from mojo.apps.dnsman.services import acme_hub_client as client
    malformed = [
        dict(unexpected=True), dict(participant_ref="not-a-uuid"), dict(client_ref="different"),
        dict(domain="different.example"), dict(source="_acme-challenge.wrong.example"),
        dict(target="other.hub.example.net"), dict(allocation_ref="other.hub.example.net"),
        dict(scope=[DOMAIN]), dict(scope=[DOMAIN, "*.evil.example"]), dict(role="admin"),
        dict(trust_state="pending"), dict(can_publish=1), dict(can_withdraw="yes"),
        dict(revoked_at=STAMP), dict(trust_state="revoked", revoked_at=STAMP),
        dict(trust_state="revoked", revoked_at="yesterday", can_publish=False),
    ]
    for overrides in malformed:
        data = _participant_data(**overrides)
        _caught(client.AcmeHubResponseError, lambda: client._participant(
            data, expected_client_ref=CLIENT_REF, expected_domain=DOMAIN, expected_allocation_ref=TARGET))
    for overrides in (dict(consumer_project_uuid="bad"), dict(consumer_project_uuid="a" * 32),
                      dict(consumer_label="x" * 201), dict(consumer_label=None)):
        _caught(client.AcmeHubResponseError, lambda: client._participant(
            _participant_data(controller=True, **overrides), controller=True, expected_consumer_uuid=PROJECT_UUID))
    _caught(client.AcmeHubResponseError, lambda: client.join(DOMAIN, CLIENT_REF, TARGET,
        transport=lambda operation, payload: _allocation_data(target="other.hub.example.net")))
    # Strict existing clients must reject additive metadata; v2 never extends
    # their four/six-field payloads.
    old = client._allocation(_allocation_data(), CLIENT_REF, expected_domain=DOMAIN)
    assert old.target == TARGET, "legacy strict parser must still accept the exact original allocation"
    _caught(client.AcmeHubResponseError, lambda: client._allocation(_participant_data(), CLIENT_REF))


@th.django_unit_test("Shared ACME client bounds pages and validates alias observations and capability identities")
def test_shared_pages_alias_and_capabilities(opts):
    from mojo.apps.dnsman.services import acme_hub_client as client
    for data in [dict(items=[_participant_data()] * 21, next_cursor=None), dict(items={}, next_cursor=None),
                 dict(items=[], next_cursor="1"), dict(items=[], next_cursor="-1"),
                 dict(items=[], next_cursor=1), dict(items=[], next_cursor="9" * 30),
                 dict(items=[_participant_data(), _participant_data()], next_cursor=None),
                 dict(items=[], next_cursor=None, total=0)]:
        _caught(client.AcmeHubResponseError, lambda: client.lookup(DOMAIN, transport=lambda operation, payload: data))
    for alias in [dict(unexpected=True), dict(state="ok"), dict(observed_target=None), dict(one_hop=False),
                  dict(checked_at="yesterday"), dict(checked_at="2026-10-20"),
                  dict(state="lookup_failed"), dict(state="mismatch")]:
        _caught(client.AcmeHubResponseError, lambda: client.status(CLIENT_REF,
            transport=lambda operation, payload: _participant_data(alias=_alias_data(**alias))))
    for alias in [_alias_data(), _alias_data(state="lookup_failed", observed_target=None, one_hop=None),
                  _alias_data(state="mismatch", observed_target="wrong.example", one_hop=True),
                  _alias_data(state="mismatch", observed_target=TARGET, one_hop=False)]:
        assert client.status(CLIENT_REF, transport=lambda operation, payload: _participant_data(alias=alias))["alias"] == alias, "valid bounded observations must parse"
    for overrides in [dict(version=True), dict(version=1), dict(shared_validation=False),
                      dict(consumer_project_uuid=None), dict(consumer_project_uuid="invalid"),
                      dict(trust_scope="all_dns"), dict(can_consume=1), dict(can_manage_trust="yes"), dict(extra=True)]:
        _caught(client.AcmeHubResponseError, lambda: client.capabilities(
            transport=lambda operation, payload: _capability_data(**overrides)))
    for cursor in ("-1", "0", "1?domain=other.example", 2, "01", "9" * 30):
        _caught(client.AcmeHubRequestError, lambda: client.lookup(DOMAIN, cursor=cursor))


@th.django_unit_test("Shared ACME client accepts only allowlisted errors and old hub detection never allocates")
def test_shared_safe_errors_and_old_hub(opts):
    from mojo.apps.dnsman.services import acme_hub_client as client
    config = client._Config(HUB_URL, HUB_KEY, 5, 30, 0)
    for token, statuses in client._SAFE_ERRORS.items():
        statuses = statuses if isinstance(statuses, tuple) else (statuses,)
        for status in statuses:
            response = FakeResponse(dict(status=False, code=status, error=token), status_code=status)
            error = _caught(client.AcmeHubResponseError, lambda: client._wire("grant", {}, config, lambda *args, **kwargs: response))
            assert error.kind == token and str(error) == token, f"safe machine error must preserve only its allowlisted token: {error.kind}"
    response = FakeResponse(dict(status=False, code=403, error="private remote text API_KEY=never-persist"), status_code=403)
    error = _caught(client.AcmeHubResponseError, lambda: client._wire("grant", {}, config, lambda *args, **kwargs: response))
    assert "private remote" not in str(error) and error.kind == "http", "remote prose must never enter persisted error fields"
    for status in (301, 302, 307, 308):
        response = FakeResponse({}, status_code=status, headers={"Location": "https://alternate.example"})
        error = _caught(client.AcmeHubResponseError, lambda: client._wire("lookup", {}, config, lambda *args, **kwargs: response))
        assert error.kind == "redirect", "configured hub redirects must remain refused"
    for url in ("https://hub.example.net/other", "https://user:key@hub.example.net", "http://alternate.example", "https://hub.example.net?target=https://alternate.example"):
        _caught(client.AcmeHubConfigurationError, lambda: client._normalized_base_url(url))
    response = FakeResponse({}, status_code=404)
    calls = []
    def post(url, **kwargs):
        calls.append(url)
        return response
    error = _caught(client.AcmeHubResponseError, lambda: client.capabilities(
        transport=lambda name, payload: client._wire(name, payload, config, post)))
    assert error.kind == "shared_validation_unsupported", "capabilities 404 has one bounded unsupported meaning"
    assert calls == [HUB_URL + "/api/dnsman/acme/capabilities"], "unsupported v2 must never invoke allocate or another hub"
    response = FakeResponse(content=b"x" * (client.MAX_SHARED_RESPONSE_BYTES + 1))
    _caught(client.AcmeHubResponseError, lambda: client._wire("lookup", {}, config, lambda *args, **kwargs: response))


@th.django_unit_test("Shared ACME connection requests contain only the frozen nonsecret descriptor")
def test_shared_descriptor_input(opts):
    from mojo.apps.dnsman.services import acme_hub_client as client
    descriptor = dict(version=1, consumer_project_uuid=PROJECT_UUID, client_ref=CLIENT_REF,
                      domain=DOMAIN, allocation_ref=TARGET)
    for changes in [dict(version=2), dict(version=True), dict(version=1.0), dict(hub_url="https://alternate.example"),
                    dict(api_key="never"), dict(consumer_project_uuid="invalid"), dict(client_ref=""),
                    dict(domain="*.customer.example"), dict(allocation_ref="https://hub.example.net")]:
        _caught(client.AcmeHubRequestError, lambda: client.grant(dict(descriptor, **changes)))
    _caught(client.AcmeHubResponseError, lambda: client.grant(descriptor,
        transport=lambda operation, payload: _participant_data(controller=True, consumer_project_uuid="a" * 32)))
    _caught(client.AcmeHubResponseError, lambda: client.revoke(TARGET, PARTICIPANT_REF,
        transport=lambda operation, payload: _participant_data(controller=True, trust_state="active")))
