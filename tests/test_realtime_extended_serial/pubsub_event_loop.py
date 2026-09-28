"""Realtime pub/sub waits on the event loop, not in executor threads (#5750).

Each logged-in socket used to poll Redis pub/sub through run_in_executor for up
to a second at a time. With more idle sockets than default-executor threads,
every login, hook and permission check queued behind those polls. Pub/sub now
uses the redis.asyncio client, so an idle socket holds no thread.

These tests open real pub/sub connections against the slot's Redis and kill one
of them, so they stay in this serial package.
"""

import asyncio
import json
import time

from testit import helpers as th

TESTIT_TIER = "bug"

IDLE_SOCKETS = 50


class _Socket:
    scope = {"headers": [], "client": ("127.0.0.1", 12345)}

    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(json.loads(payload) if isinstance(payload, str) else payload)


def _handler():
    from mojo.apps.realtime.handler import WebSocketHandler

    handler = WebSocketHandler(_Socket(), "/ws/realtime/")
    handler.authenticated = True
    return handler


def _publish(connection_id, marker):
    from mojo.apps.realtime.channels import messages_channel
    from mojo.helpers.redis.client import get_connection

    get_connection().publish(messages_channel(connection_id), json.dumps({
        "type": "direct_message", "data": {"marker": marker}, "timestamp": 1,
    }))


async def _received(handler, marker, seconds=3.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if any(m.get("data", {}).get("marker") == marker for m in handler.websocket.sent):
            return True
        await asyncio.sleep(0.02)
    return False


async def _close_all(handlers):
    for handler in handlers:
        handler.running = False
    for handler in handlers:
        await handler.cleanup_connection()


# ---------------------------------------------------------------------------
# T1: the executor stays free while sockets sit idle
# ---------------------------------------------------------------------------

@th.django_unit_test("idle realtime sockets hold no executor thread (#5750)")
def test_idle_sockets_leave_executor_free(opts):
    from concurrent.futures import ThreadPoolExecutor

    async def scenario():
        loop = asyncio.get_running_loop()
        # Fewer threads than sockets, as on any busy process.
        loop.set_default_executor(ThreadPoolExecutor(max_workers=4))
        handlers = [_handler() for _ in range(IDLE_SOCKETS)]
        try:
            for handler in handlers:
                await handler.start_redis_messages()
            await asyncio.sleep(0.5)
            waits = []
            for _ in range(5):
                started = time.perf_counter()
                await loop.run_in_executor(None, lambda: None)
                waits.append((time.perf_counter() - started) * 1000)
            await loop.run_in_executor(None, _publish, handlers[7].connection_id, "t1")
            delivered = await _received(handlers[7], "t1")
            return waits, delivered
        finally:
            await _close_all(handlers)

    waits, delivered = asyncio.run(scenario())
    assert max(waits) < 100, (
        f"an executor call waited {max(waits):.0f} ms behind {IDLE_SOCKETS} idle sockets: {waits}")
    assert delivered, "an idle socket must still receive its direct message"


@th.django_unit_test("no realtime executor call touches pub/sub (#5750)")
def test_no_pubsub_in_executor(opts):
    import ast
    import inspect
    from mojo.apps.realtime import handler as handler_module

    tree = ast.parse(inspect.getsource(handler_module))
    offenders = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        nested = {node.name: node for node in ast.walk(func)
                  if isinstance(node, ast.FunctionDef) and node is not func}
        for node in ast.walk(func):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "run_in_executor" and len(node.args) > 1):
                continue
            target = node.args[1]
            source = ast.unparse(target)
            if isinstance(target, ast.Name) and target.id in nested:
                source = ast.unparse(nested[target.id])
            if "pubsub" in source or "get_message" in source:
                offenders.append(f"{func.name} line {node.lineno}")
    assert not offenders, f"pub/sub calls still run in the executor: {offenders}"


# ---------------------------------------------------------------------------
# T5: losing one pub/sub connection never blocks the loop or other sockets
# ---------------------------------------------------------------------------

def _kill_client_on_port(port):
    from mojo.helpers.redis.client import get_connection

    r = get_connection()
    for line in r.execute_command("CLIENT", "LIST", "TYPE", "pubsub").splitlines():
        fields = dict(part.split("=", 1) for part in line.split() if "=" in part)
        if fields.get("addr", "").endswith(f":{port}"):
            r.execute_command("CLIENT", "KILL", "ID", fields["id"])
            return True
    return False


@th.django_unit_test("a killed pub/sub connection leaves the loop and other sockets working (#5750)")
def test_pubsub_connection_loss(opts):
    async def scenario():
        victim, other = _handler(), _handler()
        try:
            await victim.start_redis_messages()
            await other.start_redis_messages()
            await asyncio.sleep(0.2)
            port = victim.pubsub.connection._writer.get_extra_info("sockname")[1]
            killed = await asyncio.to_thread(_kill_client_on_port, port)

            # The loop stays responsive while the victim notices the loss.
            lags = []
            for _ in range(100):
                started = time.perf_counter()
                await asyncio.sleep(0.01)
                lags.append((time.perf_counter() - started - 0.01) * 1000)

            await asyncio.to_thread(_publish, other.connection_id, "t5")
            other_ok = await _received(other, "t5")
            # As before #5750: the affected socket's delivery task logs and ends.
            victim_stopped = victim._redis_task.done()
            return killed, max(lags), other_ok, victim_stopped
        finally:
            await _close_all([victim, other])

    killed, lag, other_ok, victim_stopped = asyncio.run(scenario())
    assert killed, "the test must find and kill the victim's pub/sub connection"
    assert victim_stopped, "the killed socket's delivery task must end, not spin"
    assert lag < 100, f"the event loop stalled {lag:.0f} ms after a pub/sub connection loss"
    assert other_ok, "another socket must keep receiving after one pub/sub connection dies"


# ---------------------------------------------------------------------------
# T6: the async client is built from the same settings as the sync one
# ---------------------------------------------------------------------------

def _getter(values):
    return lambda key, default=None: values.get(key, default)


@th.django_unit_test("the async pub/sub client uses the sync client's settings (#5750)")
def test_async_client_settings(opts):
    import redis.asyncio
    from mojo.helpers.redis import client

    cases = [
        {"REDIS_URL": "rediss://app-user:s%2Fecret@cache.example:6380/3",
         "REDIS_CONNECT_TIMEOUT": 4, "REDIS_SOCKET_TIMEOUT": 30},
        {"REDIS_SERVER": "cache.example", "REDIS_PORT": 6381, "REDIS_DB_INDEX": 2,
         "REDIS_USERNAME": "app-user", "REDIS_PASSWORD": "p@ss", "REDIS_SCHEME": "rediss"},
        {"REDIS_SERVER": "localhost", "REDIS_DB_INDEX": 5, "REDIS_MAX_CONN": 40},
        {"REDIS_SERVER": "localhost", "REDIS_PUBSUB_MAX_CONN": 900, "REDIS_CLUSTER": True},
    ]
    keys = ("host", "port", "db", "username", "password",
            "socket_connect_timeout", "socket_timeout")
    for values in cases:
        get = _getter(values)
        sync_pool = client._create_client(
            client._build_url(get=get), 1,
            float(get("REDIS_CONNECT_TIMEOUT", 2)),
            float(get("REDIS_SOCKET_TIMEOUT", 60))).connection_pool
        async_client = client._build_async_client(get)
        assert isinstance(async_client, redis.asyncio.Redis), (
            f"{values}: pub/sub must use a plain redis.asyncio client, even in cluster mode")
        async_pool = async_client.connection_pool
        for key in keys:
            assert async_pool.connection_kwargs.get(key) == sync_pool.connection_kwargs.get(key), (
                f"{values}: {key} differs: {async_pool.connection_kwargs.get(key)!r} "
                f"vs sync {sync_pool.connection_kwargs.get(key)!r}")
        assert async_pool.connection_kwargs.get("decode_responses") is True, values
        tls = sync_pool.connection_class.__name__ == "SSLConnection"
        assert (async_pool.connection_class.__name__ == "SSLConnection") == tls, (
            f"{values}: TLS must match the sync client")
        expected_max = int(values.get("REDIS_PUBSUB_MAX_CONN", values.get("REDIS_MAX_CONN", 500)))
        assert async_pool.max_connections == expected_max, (
            f"{values}: the async pool needs an explicit cap, got {async_pool.max_connections}")


# ---------------------------------------------------------------------------
# T7: cluster mode — a plain async subscriber hears PUBLISH from another node
# ---------------------------------------------------------------------------

# Redis cluster nodes also listen on a bus port, client port + 10000, so a
# client port above 55535 makes redis-server refuse to start.
CLUSTER_BUS_OFFSET = 10000
MAX_CLUSTER_PORT = 65535 - CLUSTER_BUS_OFFSET


def _port_free(port):
    import socket

    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _cluster_ports(count, candidates):
    """Return `count` client ports whose client and bus ports are all valid, distinct and free."""
    chosen, taken = [], set()
    for port in candidates:
        bus = port + CLUSTER_BUS_OFFSET
        if not 1024 <= port <= MAX_CLUSTER_PORT or port in taken or bus in taken:
            continue
        if _port_free(port) and _port_free(bus):
            chosen.append(port)
            taken.update((port, bus))
            if len(chosen) == count:
                return chosen
    raise RuntimeError(f"no {count} free cluster ports (client and client+{CLUSTER_BUS_OFFSET})")


@th.django_unit_test("cluster: test ports leave room for the cluster bus port (#5750)")
def test_cluster_port_allocation(opts):
    ports = _cluster_ports(3, [59163, 55536, 65535, *range(30000, 30200)])
    assert len(ports) == 3 and len(set(ports)) == 3, ports
    for port in ports:
        assert port <= MAX_CLUSTER_PORT, (
            f"client port {port} leaves no valid bus port ({port + CLUSTER_BUS_OFFSET})")
    taken = set(ports) | {p + CLUSTER_BUS_OFFSET for p in ports}
    assert len(taken) == 6, f"client and bus ports must not collide: {sorted(taken)}"
    try:
        _cluster_ports(1, [59163, 55536])
    except RuntimeError:
        pass
    else:
        raise AssertionError("ports above 55535 must never be chosen")


@th.django_unit_test("cluster: an async subscriber receives a publish sent to another node (#5750)")
@th.requires_extra("redis_cluster")
def test_cluster_pubsub_across_nodes(opts):
    """Needs redis-server on PATH; starts and removes a local 3-node cluster."""
    import os
    import random
    import shutil
    import subprocess
    import tempfile
    import redis
    import redis.asyncio
    from redis.cluster import RedisCluster

    workdir = tempfile.mkdtemp(prefix="rt5750-cluster-")
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
        cluster = RedisCluster(host="127.0.0.1", port=ports[0], decode_responses=True)
        channel = "rt5750:cluster:probe"
        publisher_node = next(n for n in cluster.get_primaries() if n.port != ports[0])

        async def scenario():
            # Same shape as get_async_connection() in cluster mode: a plain
            # client on the configured endpoint (the first node).
            subscriber = redis.asyncio.Redis(host="127.0.0.1", port=ports[0], decode_responses=True)
            pubsub = subscriber.pubsub()
            await pubsub.subscribe(channel)
            await pubsub.get_message(timeout=1.0)  # subscribe confirmation
            receivers = await asyncio.to_thread(
                cluster.publish, channel, "hello", target_nodes=publisher_node)
            message = None
            deadline = time.monotonic() + 3
            while message is None and time.monotonic() < deadline:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
            await pubsub.aclose()
            await subscriber.aclose()
            return receivers, message

        receivers, message = asyncio.run(scenario())
        assert publisher_node.port != ports[0], "the publish must go to a different node"
        assert message and message["data"] == "hello", (
            f"a subscriber on node {ports[0]} must receive a publish sent to node "
            f"{publisher_node.port}; got {message} (receivers on that node: {receivers})")
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
