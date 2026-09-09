import json
import resource
from unittest import mock

from testit import helpers as th


class _MemoryErrorBuffer:
    def read(self, unused_limit):
        raise MemoryError


class _MemoryErrorInput:
    buffer = _MemoryErrorBuffer()


@th.unit_test("broker address-space limit grows from the post-import baseline")
def test_adaptive_address_space_limit(opts):
    from mojo.deploy import firewall_broker as broker

    baseline = 260 * 1024 * 1024
    installed = []

    def set_limit(kind, value):
        installed.append((kind, value))

    target = broker._install_address_space_limit(
        baseline_bytes=baseline,
        get_limit=lambda unused_kind: (resource.RLIM_INFINITY,
                                       resource.RLIM_INFINITY),
        set_limit=set_limit,
    )

    th.assert_eq(
        target, baseline + broker.ADDRESS_SPACE_GROWTH_BYTES,
        "the old absolute 256 MiB cap must become post-import growth headroom")
    th.assert_true(
        target <= broker.MAX_ADDRESS_SPACE_BYTES,
        "the adaptive broker envelope exceeded its reviewed absolute ceiling")
    th.assert_eq(
        installed, [(resource.RLIMIT_AS, (target, target))],
        "the broker did not install one finite soft and hard address-space cap")


@th.unit_test("broker returns valid prebuilt JSON when stdin allocation fails")
def test_memory_error_response(opts):
    from mojo.deploy import firewall_broker as broker

    written = []

    def write(unused_descriptor, payload):
        written.append(payload)
        return len(payload)

    with mock.patch.object(broker, "_verify_caller"), \
            mock.patch.object(broker, "_install_address_space_limit"), \
            mock.patch.object(broker.sys, "stdin", _MemoryErrorInput()), \
            mock.patch.object(broker.os, "write", side_effect=write), \
            mock.patch.object(broker, "_acquire_host_lock") as lock:
        status = broker.main([])

    th.assert_eq(status, 1, "MemoryError must fail the root broker closed")
    lock.assert_not_called()
    response = json.loads(b"".join(written).decode("ascii"))
    th.assert_eq(response["ok"], False, "the resource error looked successful")
    th.assert_eq(
        response["error"]["code"], "broker_resource_exhausted",
        "the resource error must remain stable and machine-readable")
