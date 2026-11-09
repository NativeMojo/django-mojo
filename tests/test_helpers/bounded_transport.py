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


@th.django_unit_test()
def test_connection_close_preserves_real_http_body_file_lifetime(opts):
    import http.client
    import socket
    import time
    from mojo.helpers.bounded_transport import DeadlineSocket
    client, peer = socket.socketpair()
    connection = http.client.HTTPConnection("fixture.example", timeout=1)
    response = None
    try:
        client.settimeout(1)
        connection.sock = DeadlineSocket(client, time.monotonic() + 2)
        connection.request("GET", "/")
        peer.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\n")
        response = connection.getresponse()
        assert connection.sock is None, "HTTPConnection must exercise its real Connection:close owner release"
        assert client.fileno() >= 0, "a live body makefile must retain the socket after connection close"
        # Body bytes arrive only AFTER the connection releases its socket.
        peer.sendall(b"hello")
        assert response.read() == b"hello", "Connection:close body must remain readable under the absolute deadline"
        response.close()
        assert client.fileno() == -1, "closing the final body file must release the underlying socket"
    finally:
        if response is not None:
            response.close()
        connection.close()
        client.close()
        peer.close()


@th.django_unit_test()
def test_real_tls_handshake_drip_obeys_remaining_budget_after_late_tcp(opts):
    import socket
    import ssl
    import threading
    import time
    from botocore.awsrequest import AWSHTTPSConnectionPool
    from redis.connection import SSLConnection
    from mojo.helpers.bounded_transport import bind_aws_client, bind_redis_pool
    for driver in ("aws", "redis"):
        client, peer = socket.socketpair()
        client.settimeout(1)
        peer.settimeout(1)
        stop = threading.Event()
        progress = []
        def drip():
            try:
                hello = peer.recv(8192)
                if hello:
                    progress.append("client_hello")
                # Deliberately incomplete TLS record header. Progress must not
                # give the SSL handshake a fresh timeout for each fragment.
                for part in (b"\x16", b"\x03", b"\x03", b"\x00"):
                    if stop.wait(.025):
                        break
                    peer.sendall(part)
                    progress.append("fragment")
                stop.wait(1)
            except OSError:
                pass
        thread = threading.Thread(target=drip)
        thread.start()
        started = time.monotonic()
        deadline = started + .2
        def connector(*args, **kwargs):
            time.sleep(.12)
            return client
        connection = None
        try:
            if driver == "aws":
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                manager = SimpleNamespace(connection_pool_kw={"ssl_context": context})
                transport = SimpleNamespace(_manager=manager, _proxy_config=None)
                sdk = SimpleNamespace(_endpoint=SimpleNamespace(http_session=transport, host="https://fixture.example"))
                bind_aws_client(sdk, deadline, connector=connector)
                connection = manager.pool_classes_by_scheme["https"].ConnectionCls(
                    "fixture.example", port=443, timeout=1,
                    ssl_context=manager.connection_pool_kw["ssl_context"])
                try:
                    connection.connect()
                except OSError:
                    pass
                else:
                    raise AssertionError("an incomplete TLS handshake cannot complete")
            else:
                pool = SimpleNamespace(connection_class=SSLConnection)
                bind_redis_pool(pool, deadline, connector=connector)
                connection = pool.connection_class(host="fixture.example", port=6379,
                                                   socket_timeout=1, socket_connect_timeout=1,
                                                   ssl_cert_reqs="none", ssl_check_hostname=False)
                try:
                    connection._connect()
                except OSError:
                    pass
                else:
                    raise AssertionError("an incomplete Redis TLS handshake cannot complete")
            elapsed = time.monotonic() - started
            assert elapsed < .6, f"{driver} TLS must use the remaining deadline after late TCP, took {elapsed:.3f}s"
            assert "client_hello" in progress and "fragment" in progress, f"{driver} fixture must exercise actual TLS handshake writes and fragmented reads"
        finally:
            stop.set()
            if connection is not None:
                connection.close() if hasattr(connection, "close") else connection.disconnect()
            client.close()
            peer.close()
            thread.join(1)
        assert not thread.is_alive(), f"{driver} fixture must leave no detached handshake worker"
