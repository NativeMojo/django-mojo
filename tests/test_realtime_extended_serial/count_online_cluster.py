"""#8352: count_online end to end with get_redis replaced, and on a Redis cluster.

The default-tier tests (tests/test_realtime/count_online.py) may not patch
production code; these do, so they stay in this serial package. The cluster
test needs redis-server on PATH and starts and removes a local 3-node cluster.
"""
import time
from unittest import mock

from testit import helpers as th
from testit.helpers import assert_eq, assert_true

from .pubsub_event_loop import CLUSTER_BUS_OFFSET, MAX_CLUSTER_PORT, _cluster_ports

TESTIT_TIER = "bug"

USER_TYPE = "t8352s"


def _key(uid):
    return f"realtime:online:{USER_TYPE}:{uid}"


class _CountingClient:
    """Forwards exists() to `inner` and records each call's keys."""

    def __init__(self, inner):
        self.inner = inner
        self.calls = []

    def exists(self, *keys):
        self.calls.append(keys)
        return self.inner.exists(*keys)


def _no_redis():
    raise AssertionError("empty input must not reach get_redis")


@th.django_unit_test("count_online: empty input never calls get_redis (#8352)")
def test_count_online_empty_touches_no_redis(opts):
    from mojo.apps.realtime import manager

    with mock.patch.object(manager, "get_redis", _no_redis):
        assert_eq(manager.count_online(USER_TYPE, []), 0, "an empty list counts 0")
        assert_eq(manager.count_online(USER_TYPE, ()), 0, "an empty tuple counts 0")
        assert_eq(manager.count_online(USER_TYPE, iter([])), 0, "an empty iterator counts 0")


@th.django_unit_test("count_online: one EXISTS per chunk end to end; 10,000 ids take 10 calls (#8352)")
def test_count_online_calls_per_chunk(opts):
    from mojo.apps.realtime import manager
    from mojo.helpers.redis import get_connection

    redis = get_connection()
    online = [7, 42, 9999]
    redis.delete(*[_key(uid) for uid in online])
    try:
        for uid in online:
            redis.sadd(_key(uid), f"c{uid}")
        client = _CountingClient(redis)
        with mock.patch.object(manager, "get_redis", lambda: client):
            assert_eq(manager.count_online(USER_TYPE, range(10000)), 3, "three of 10,000 ids are online")
            assert_eq([len(call) for call in client.calls], [1000] * 10, "10,000 ids take 10 EXISTS calls of 1,000")
            client.calls.clear()
            assert manager.count_online(USER_TYPE, [7, "7", 7, "8"]) == 1
            assert_eq(client.calls, [(_key(7), _key(8))], "7, '7' and 7 collapse to one key: one call with keys 7 and 8")
    finally:
        redis.delete(*[_key(uid) for uid in online])


@th.django_unit_test("count_online: a Redis error from get_redis's client reaches the caller (#8352)")
def test_count_online_error_end_to_end(opts):
    import redis as redis_lib
    from mojo.apps.realtime import manager

    class _Failing:
        calls = 0

        def exists(self, *keys):
            self.calls += 1
            if self.calls == 2:
                raise redis_lib.exceptions.ConnectionError("chunk 2 down")
            return 3

    client = _Failing()
    with mock.patch.object(manager, "get_redis", lambda: client):
        try:
            result = manager.count_online(USER_TYPE, range(6), chunk_size=3)
        except redis_lib.exceptions.ConnectionError:
            pass
        else:
            raise AssertionError(f"the error must reach the caller, got {result!r}")
    assert_eq(client.calls, 2, "no third chunk after the failing second")


@th.django_unit_test("cluster: count_online is exact and costs at most one exchange per node per chunk (#8352)")
@th.requires_extra("redis_cluster")
def test_count_online_on_cluster(opts):
    """Needs redis-server on PATH; starts and removes a local 3-node cluster."""
    import os
    import random
    import shutil
    import subprocess
    import tempfile
    import redis
    from redis.cluster import NodeCommands, RedisCluster
    from mojo.apps.realtime import manager

    workdir = tempfile.mkdtemp(prefix="rt8352-cluster-")
    ports = _cluster_ports(3, random.sample(range(20000, MAX_CLUSTER_PORT + 1), 500))
    procs = []

    def node_log(port):
        path = os.path.join(workdir, f"redis-{port}.log")
        return open(path).read()[-2000:] if os.path.exists(path) else "(no log)"

    def wait_until(ready, what, seconds=10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            for port, proc in zip(ports, procs):
                assert proc.poll() is None, (
                    f"redis-server on {port} exited ({proc.returncode}) {what}:\n{node_log(port)}")
            if ready():
                return
            time.sleep(0.2)
        raise AssertionError(f"timed out {what}:\n" + "\n".join(
            f"--- {port}\n{node_log(port)}" for port in ports))

    def all_ping():
        for port in ports:
            try:
                redis.Redis(host="127.0.0.1", port=port, socket_timeout=1).ping()
            except redis.RedisError:
                return False
        return True

    def cluster_ok():
        info = redis.Redis(host="127.0.0.1", port=ports[0], decode_responses=True).execute_command(
            "CLUSTER", "INFO")
        return "cluster_state:ok" in str(info)

    try:
        for port in ports:
            procs.append(subprocess.Popen(
                ["redis-server", "--port", str(port),
                 "--cluster-port", str(port + CLUSTER_BUS_OFFSET), "--cluster-enabled", "yes",
                 "--cluster-config-file", f"nodes-{port}.conf", "--save", "",
                 "--appendonly", "no", "--dir", workdir, "--logfile", f"redis-{port}.log"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        wait_until(all_ping, "waiting for the nodes to answer PING")
        created = subprocess.run(
            ["redis-cli", "--cluster", "create", *[f"127.0.0.1:{p}" for p in ports],
             "--cluster-yes"], capture_output=True, text=True, timeout=30)
        assert created.returncode == 0, (
            f"redis-cli --cluster create failed:\n{created.stdout}\n{created.stderr}")
        wait_until(cluster_ok, "waiting for cluster_state:ok")
        cluster = RedisCluster(host="127.0.0.1", port=ports[0], read_from_replicas=False)

        uids = list(range(1, 51))
        online = uids[::2]
        for uid in online:
            if uid % 4 == 1:
                cluster.sadd(_key(uid), f"c{uid}")
            else:
                cluster.set(_key(uid), '{"connection_ids": ["c"]}')
        slots = {cluster.keyslot(_key(uid)) for uid in uids}
        assert len(slots) > 3, f"the ids must spread across slots, got {len(slots)}"

        real_write = NodeCommands.write
        writes = []

        def counting_write(self):
            writes.append(self)
            return real_write(self)

        chunk_size = 20
        chunks = -(-len(uids) // chunk_size)
        with mock.patch.object(manager, "get_redis", lambda: cluster), \
                mock.patch.object(NodeCommands, "write", counting_write):
            assert_eq(manager.count_online(USER_TYPE, uids, chunk_size=chunk_size), len(online), "the cluster count is exact")
        assert chunks < len(writes) <= chunks * len(ports), (
            f"{chunks} chunks over {len(ports)} nodes must take more than {chunks} and at most "
            f"{chunks * len(ports)} node exchanges, took {len(writes)}")
        cluster.close()
    finally:
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(workdir, ignore_errors=True)
