"""Private numeric dialing and absolute socket deadlines, no real network."""
import io
from types import SimpleNamespace
from testit import helpers as th


class Socket:
    def __init__(self, clock=None):
        self.clock = clock
        self.calls = []
        self.timeout = 60
    def settimeout(self, value):
        self.timeout = value
        self.calls.append(("timeout", value))
    def gettimeout(self):
        return self.timeout
    def setsockopt(self, *args):
        self.calls.append(("option", args))
    def connect(self, address):
        self.calls.append(("connect", address))
    def recv_into(self, buffer, *args):
        if self.clock is not None:
            self.clock[0] += .4
        self.calls.append(("recv", len(buffer)))
        buffer[0:1] = b"x"
        return 1
    def sendall(self, value):
        if self.clock is not None:
            self.clock[0] += .4
        self.calls.append(("send", value))
    def close(self):
        self.calls.append(("close",))


@th.django_unit_test()
def test_numeric_connect_and_expired_resolver(opts):
    from mojo.helpers.bounded_transport import connect_tcp, BoundedTransportError
    clock, resolutions, created = [0], [], []
    raw = Socket()
    def resolver(host, deadline):
        resolutions.append((host, deadline))
        return ["192.0.2.1"]
    def factory(*args):
        created.append(args)
        return raw
    result = connect_tcp("metrics.example", 443, 1, timeout=.25, resolver=resolver,
                          socket_factory=factory, clock=lambda: clock[0])
    assert result is raw and resolutions == [("metrics.example", 1)], "numeric dialing must consume the shared bounded resolver"
    assert ("connect", ("192.0.2.1", 443)) in raw.calls, "TCP connect must receive a numeric address, without a second hostname lookup"
    assert raw.timeout <= .25, "resolved connect must preserve the remaining connection budget"
    def expired(host, deadline):
        clock[0] = 2
        return ["192.0.2.1"]
    before = len(created)
    try:
        connect_tcp("metrics.example", 443, 1, resolver=expired, socket_factory=factory, clock=lambda: clock[0])
    except BoundedTransportError as exc:
        assert exc.code == "deadline_exceeded", "expired DNS evidence must carry the shared deadline reason"
    else:
        raise AssertionError("late DNS cannot start a TCP connection")
    assert len(created) == before, "no socket can be created after DNS exhausts the deadline"


@th.django_unit_test()
def test_slow_drip_makefile_and_send_cannot_renew_deadline(opts):
    from mojo.helpers.bounded_transport import DeadlineSocket, BoundedTransportError
    clock = [0]
    raw = Socket(clock)
    bounded = DeadlineSocket(raw, 1, clock=lambda: clock[0])
    stream = bounded.makefile("rb")
    try:
        stream.read(5)
    except BoundedTransportError as exc:
        assert exc.code == "deadline_exceeded", "slow-drip buffered HTTP reads must expire the absolute deadline"
    else:
        raise AssertionError("each received byte cannot renew the transfer budget")
    reads = len([call for call in raw.calls if call[0] == "recv"])
    assert reads == 3, "deadline must stop receiving after the late third chunk"
    try:
        bounded.sendall(b"after-expiry")
    except BoundedTransportError:
        pass
    else:
        raise AssertionError("expired sockets cannot begin another send")
    assert not any(call[0] == "send" for call in raw.calls), "post-expiry send must never reach the socket"
    stream.close()
    bounded.close()
    assert raw.calls[-1] == ("close",), "expired socket cleanup must remain local and complete"


@th.django_unit_test()
def test_private_aws_and_redis_tls_preserve_original_hostname(opts):
    import time
    from botocore.awsrequest import AWSHTTPSConnectionPool
    from redis.connection import SSLConnection
    from mojo.helpers.bounded_transport import bind_aws_client, bind_redis_pool, DeadlineSocket
    calls = []
    raw = Socket()
    def connector(host, port, deadline, **kwargs):
        calls.append((host, port, deadline))
        return raw
    original_aws = AWSHTTPSConnectionPool.ConnectionCls
    manager = SimpleNamespace(pool_classes_by_scheme={"https": AWSHTTPSConnectionPool})
    transport = SimpleNamespace(_manager=manager, _proxy_config=None)
    client = SimpleNamespace(_endpoint=SimpleNamespace(http_session=transport, host="https://metrics.example"))
    deadline = time.monotonic() + 1
    bind_aws_client(client, deadline, connector=connector)
    connection = manager.pool_classes_by_scheme["https"].ConnectionCls("metrics.example", port=443, timeout=.25)
    assert connection._new_conn() is raw, "private AWS connection must use the injected numeric dialer"
    assert connection.host == "metrics.example", "AWS TLS SNI and signed Host must retain the original hostname"
    assert AWSHTTPSConnectionPool.ConnectionCls is original_aws, "private AWS adapters cannot modify global SDK pools"
    pool = SimpleNamespace(connection_class=SSLConnection)
    bind_redis_pool(pool, deadline, connector=connector)
    redis = pool.connection_class(host="cache.example", port=6379, socket_connect_timeout=.25, socket_timeout=.25)
    wrapped_hosts = []
    def wrap(sock):
        wrapped_hosts.append(redis.host)
        return sock
    redis._wrap_socket_with_ssl = wrap
    result = redis._connect()
    assert isinstance(result, DeadlineSocket), "Redis authentication/parser I/O must receive the absolute-deadline socket"
    assert wrapped_hosts == ["cache.example"] and redis.host == "cache.example", "Redis TLS must wrap the original hostname after numeric TCP dialing"
    assert calls[0][0] == "metrics.example" and calls[1][0] == "cache.example", "each private SDK must pass its own original identity to bounded resolution"
