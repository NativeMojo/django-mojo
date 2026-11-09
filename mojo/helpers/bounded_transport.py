"""Private diagnostic transports with a shared DNS/connect/send/read deadline.

No global socket hooks or detached workers. TLS is wrapped by the owning SDK
using the original hostname; only TCP dialing uses bounded numeric addresses.
"""
import io
import ipaddress
import math
import socket
import ssl
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
            budget = remaining(deadline, clock)
            raw.settimeout(min(timeout, budget) if timeout is not None else budget)
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
        bounded_socket._acquire_file()

    def readable(self):
        return True

    def readinto(self, buffer):
        return self.bounded_socket.recv_into(buffer)

    def close(self):
        if not self.closed:
            try:
                super().close()
            finally:
                self.bounded_socket._release_file()


class DeadlineSocket:
    """Bound every I/O, including HTTP's buffered makefile reads and slow drips."""
    def __init__(self, target, deadline, *, clock=None, timeout=None):
        self.target = target
        self.deadline = deadline
        self.clock = clock or time.monotonic
        self.timeout = timeout if timeout is not None else target.gettimeout()
        self._file_refs = 0
        self._close_requested = False
        self._target_closed = False

    def _acquire_file(self):
        if self._close_requested:
            raise BoundedTransportError("connection_closed")
        self._file_refs += 1

    def _release_file(self):
        self._file_refs -= 1
        if self._close_requested and self._file_refs == 0:
            self._close_target()

    def _close_target(self):
        if not self._target_closed:
            self._target_closed = True
            self.target.close()

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
        # Mirror socket.makefile lifetime: Connection:close can release its
        # socket before the HTTP response body finishes reading the file.
        self._close_requested = True
        if self._file_refs == 0:
            self._close_target()

    def __getattr__(self, name):
        return getattr(self.target, name)


class DeadlineSSLContext:
    """Keep TLS configuration; refresh/check the budget at the actual handshake."""
    def __init__(self, target, deadline, clock=None):
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "deadline", deadline)
        object.__setattr__(self, "clock", clock or time.monotonic)

    def __getattr__(self, name):
        return getattr(self.target, name)

    def __setattr__(self, name, value):
        setattr(self.target, name, value)

    def wrap_socket(self, sock, *args, **kwargs):
        budget = remaining(self.deadline, self.clock)
        previous = sock.gettimeout()
        sock.settimeout(min(previous, budget) if previous is not None else budget)
        kwargs["do_handshake_on_connect"] = False
        wrapped = self.target.wrap_socket(sock, *args, **kwargs)
        try:
            budget = remaining(self.deadline, self.clock)
            wrapped.settimeout(min(previous, budget) if previous is not None else budget)
            # CPython's SSL handshake uses this as an overall handshake
            # timeout, not a fresh timeout for each protocol record.
            wrapped.do_handshake()
            remaining(self.deadline, self.clock)
            return wrapped
        except BaseException:
            wrapped.close()
            raise


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
    context = transport._manager.connection_pool_kw.get("ssl_context") if hasattr(transport._manager, "connection_pool_kw") else None
    if context is not None:
        if isinstance(context, DeadlineSSLContext):
            context = context.target
        transport._manager.connection_pool_kw["ssl_context"] = DeadlineSSLContext(context, deadline, clock)
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
            def _wrap_socket_with_ssl(self, raw):
                # The ordinary Redis TLS settings stay intact; extra OCSP
                # transports were refused before connecting above.
                context = ssl.create_default_context()
                context.check_hostname = self.check_hostname
                context.verify_mode = self.cert_reqs
                for flag in self.ssl_include_verify_flags or []:
                    context.verify_flags |= flag
                for flag in self.ssl_exclude_verify_flags or []:
                    context.verify_flags &= ~flag
                if self.certfile or self.keyfile:
                    context.load_cert_chain(self.certfile, self.keyfile, password=self.certificate_password)
                if self.ca_certs is not None or self.ca_path is not None or self.ca_data is not None:
                    context.load_verify_locations(cafile=self.ca_certs, capath=self.ca_path, cadata=self.ca_data)
                if self.ssl_min_version is not None:
                    context.minimum_version = self.ssl_min_version
                if self.ssl_ciphers:
                    context.set_ciphers(self.ssl_ciphers)
                return DeadlineSSLContext(context, deadline).wrap_socket(raw, server_hostname=self.host)
    elif issubclass(original, Connection):
        class PrivateConnection(DeadlineMixin, NumericConnection):
            pass
    elif issubclass(original, UnixDomainSocketConnection):
        class PrivateConnection(DeadlineMixin, UnixDomainSocketConnection):
            pass
    else:
        raise BoundedTransportError("redis_connection_transport_unsupported")
    pool.connection_class = PrivateConnection
