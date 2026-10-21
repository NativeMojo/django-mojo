"""Private diagnostic transports with a shared DNS/connect/send/read deadline.

No global socket hooks or detached workers. TLS is wrapped by the owning SDK
using the original hostname; only TCP dialing uses bounded numeric addresses.
"""
import io
import ipaddress
import math
import socket
import time


class BoundedTransportError(OSError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def remaining(deadline, clock=None):
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise BoundedTransportError("invalid_deadline")
    value = deadline - (clock or time.monotonic)()
    if value <= 0:
        raise BoundedTransportError("deadline_exceeded")
    return value


def connect_tcp(hostname, port, deadline, *, timeout=None, source_address=None,
                socket_options=None, resolver=None, socket_factory=None, clock=None):
    """Dial numeric addresses under one deadline; leave SNI/Host to the caller."""
    clock = clock or time.monotonic
    remaining(deadline, clock)
    try:
        if resolver is None:
            from mojo.helpers.dns.probe import resolve_addresses
            addresses = resolve_addresses(hostname, deadline, clock=clock)
        else:
            addresses = resolver(hostname, deadline)
    except Exception as exc:
        raise BoundedTransportError(getattr(exc, "code", "dns_resolution_unavailable")) from None
    remaining(deadline, clock)
    if source_address and source_address[0]:
        try:
            ipaddress.ip_address(source_address[0])
        except ValueError:
            raise BoundedTransportError("source_address_unsupported") from None
    factory = socket_factory or socket.socket
    last_error = None
    for address in addresses[:16]:
        budget = remaining(deadline, clock)
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            raise BoundedTransportError("dns_invalid_address") from None
        family = socket.AF_INET6 if parsed.version == 6 else socket.AF_INET
        raw = factory(family, socket.SOCK_STREAM)
        try:
            for option in socket_options or []:
                raw.setsockopt(*option)
            raw.settimeout(min(timeout, budget) if timeout is not None else budget)
            if source_address:
                raw.bind(source_address)
            raw.connect((str(parsed), int(port)))
            remaining(deadline, clock)
            return raw
        except (OSError, ValueError) as exc:
            last_error = exc
            raw.close()
    remaining(deadline, clock)
    if isinstance(last_error, BoundedTransportError):
        raise last_error
    raise BoundedTransportError("connection_unavailable") from None


class _SocketReader(io.RawIOBase):
    def __init__(self, bounded_socket):
        super().__init__()
        self.bounded_socket = bounded_socket

    def readable(self):
        return True

    def readinto(self, buffer):
        return self.bounded_socket.recv_into(buffer)


class DeadlineSocket:
    """Bound every I/O, including HTTP's buffered makefile reads and slow drips."""
    def __init__(self, target, deadline, *, clock=None, timeout=None):
        self.target = target
        self.deadline = deadline
        self.clock = clock or time.monotonic
        self.timeout = timeout if timeout is not None else target.gettimeout()

    def _io(self, method, *args, **kwargs):
        budget = remaining(self.deadline, self.clock)
        self.target.settimeout(min(self.timeout, budget) if self.timeout is not None else budget)
        value = getattr(self.target, method)(*args, **kwargs)
        remaining(self.deadline, self.clock)
        return value

    def recv(self, *args, **kwargs):
        return self._io("recv", *args, **kwargs)

    def recv_into(self, *args, **kwargs):
        return self._io("recv_into", *args, **kwargs)

    def send(self, *args, **kwargs):
        return self._io("send", *args, **kwargs)

    def sendall(self, *args, **kwargs):
        return self._io("sendall", *args, **kwargs)

    def makefile(self, mode="r", buffering=None, **kwargs):
        if mode not in ("r", "rb"):
            raise BoundedTransportError("socket_file_mode_unsupported")
        remaining(self.deadline, self.clock)
        raw = _SocketReader(self)
        return raw if buffering == 0 else io.BufferedReader(raw)

    def settimeout(self, value):
        self.timeout = value
        budget = remaining(self.deadline, self.clock)
        self.target.settimeout(min(value, budget) if value is not None else budget)

    def gettimeout(self):
        return self.target.gettimeout()

    def close(self):
        # Local cleanup remains safe after expiration; no extra network I/O.
        return self.target.close()

    def __getattr__(self, name):
        return getattr(self.target, name)


def bind_aws_client(client, deadline, clock=None, connector=None):
    """Install private botocore pool classes; keep original TLS SNI/Host values."""
    endpoint = getattr(client, "_endpoint", None)
    transport = getattr(endpoint, "http_session", None)
    if transport is None:
        # Injected collaborators own their own transport contract.
        return
    bind_aws_session(transport, deadline, clock=clock, connector=connector, url=endpoint.host)


def bind_aws_session(transport, deadline, *, clock=None, connector=None, url=None):
    """Also used for a private IMDS fetcher before SDK credential discovery."""
    from botocore.awsrequest import (
        AWSHTTPConnection, AWSHTTPSConnection, AWSHTTPConnectionPool, AWSHTTPSConnectionPool)
    if getattr(transport, "_manager", None) is None:
        raise BoundedTransportError("aws_transport_unsupported")
    proxy = getattr(transport, "_proxy_config", None)
    if proxy is not None and url and proxy.proxy_url_for(url):
        raise BoundedTransportError("proxy_transport_unsupported")
    clock = clock or time.monotonic
    connector = connector or connect_tcp

    class ConnectionMixin:
        def _new_conn(self):
            return connector(self.host, self.port, deadline,
                               timeout=self.timeout, source_address=self.source_address,
                               socket_options=self.socket_options, clock=clock)

        def connect(self):
            remaining(deadline, clock)
            super().connect()
            try:
                remaining(deadline, clock)
                self.sock = DeadlineSocket(self.sock, deadline, clock=clock)
            except Exception:
                self.close()
                raise

    class HTTPConnection(ConnectionMixin, AWSHTTPConnection):
        pass
    class HTTPSConnection(ConnectionMixin, AWSHTTPSConnection):
        pass
    class HTTPPool(AWSHTTPConnectionPool):
        ConnectionCls = HTTPConnection
    class HTTPSPool(AWSHTTPSConnectionPool):
        ConnectionCls = HTTPSConnection
    classes = {"http": HTTPPool, "https": HTTPSPool}
    transport._pool_classes_by_scheme = classes
    transport._manager.pool_classes_by_scheme = classes


def bind_redis_pool(pool, deadline, connector=None):
    """Private plain/TLS/Unix Redis connections; TLS wraps the original name."""
    from redis.connection import Connection, SSLConnection, UnixDomainSocketConnection
    original = pool.connection_class
    connector = connector or connect_tcp

    class NumericConnection(Connection):
        def _connect(self):
            options = [(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)]
            if self.socket_keepalive:
                options.append((socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1))
                options.extend((socket.IPPROTO_TCP, key, value)
                               for key, value in self.socket_keepalive_options.items())
            return connector(self.host, self.port, deadline,
                               timeout=self.socket_connect_timeout, socket_options=options)

    class DeadlineMixin:
        def _connect(self):
            if (getattr(self, "ssl_validate_ocsp", False)
                    or getattr(self, "ssl_validate_ocsp_stapled", False)):
                raise BoundedTransportError("ocsp_transport_unsupported")
            raw = super()._connect()
            raw.settimeout(min(self.socket_timeout or remaining(deadline), remaining(deadline)))
            return DeadlineSocket(raw, deadline, timeout=self.socket_timeout)

    if issubclass(original, SSLConnection):
        class PrivateConnection(DeadlineMixin, SSLConnection, NumericConnection):
            pass
    elif issubclass(original, Connection):
        class PrivateConnection(DeadlineMixin, NumericConnection):
            pass
    elif issubclass(original, UnixDomainSocketConnection):
        class PrivateConnection(DeadlineMixin, UnixDomainSocketConnection):
            pass
    else:
        raise BoundedTransportError("redis_connection_transport_unsupported")
    pool.connection_class = PrivateConnection
