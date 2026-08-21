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
