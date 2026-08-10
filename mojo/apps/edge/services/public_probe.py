"""DNS-pinned public HTTPS probe used by WebApp verification."""

import http.client
import ipaddress
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from urllib.parse import urlsplit


class UnsafePublicProbe(ValueError):
    pass


MAX_ADDRESSES = 8
_RESOLVER_POOL = ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="mojo-public-probe-dns")
_RESOLVER_SLOTS = threading.BoundedSemaphore(4)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname, port, address, timeout):
        super().__init__(hostname, port=port, timeout=timeout,
                         context=ssl.create_default_context())
        self._pinned_address = address

    def connect(self):
        sock = socket.create_connection(
            (self._pinned_address, self.port), self.timeout,
            self.source_address)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def public_addresses(hostname, port=443, timeout=3.0):
    """Resolve once and reject the entire answer set if any address is unsafe."""
    deadline = time.monotonic() + max(0.05, float(timeout))
    if not _RESOLVER_SLOTS.acquire(timeout=max(0.05, deadline - time.monotonic())):
        raise UnsafePublicProbe("hostname resolver capacity unavailable")
    try:
        future = _RESOLVER_POOL.submit(
            socket.getaddrinfo, hostname, port, 0, socket.SOCK_STREAM,
            socket.IPPROTO_TCP)
    except Exception:
        _RESOLVER_SLOTS.release()
        raise
    # A timed-out getaddrinfo cannot be killed. Keep its slot until the future
    # really completes so the executor queue and worker count stay bounded.
    future.add_done_callback(lambda _future: _RESOLVER_SLOTS.release())
    try:
        answers = future.result(timeout=max(0.05, deadline - time.monotonic()))
    except TimeoutError as exc:
        future.cancel()
        raise UnsafePublicProbe("hostname resolution timed out") from exc
    except socket.gaierror as exc:
        raise UnsafePublicProbe("hostname could not be resolved") from exc
    addresses = []
    for answer in answers:
        address = ipaddress.ip_address(answer[4][0])
        if not address.is_global:
            raise UnsafePublicProbe(
                "hostname resolved to a non-public address")
        value = str(address)
        if value not in addresses:
            addresses.append(value)
    if not addresses:
        raise UnsafePublicProbe("hostname had no usable address")
    if len(addresses) > MAX_ADDRESSES:
        raise UnsafePublicProbe("hostname resolved to too many addresses")
    return addresses


def probe_https_root(origin, timeout=3.0, max_body=65536):
    """GET exactly ``/`` without redirects through a pinned TLS connection."""
    parsed = urlsplit(origin)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or
            parsed.password or parsed.query or parsed.fragment or
            parsed.path not in ("", "/")):
        raise UnsafePublicProbe("probe requires an HTTPS origin")
    port = parsed.port or 443
    deadline = time.monotonic() + max(0.05, float(timeout))
    addresses = public_addresses(
        parsed.hostname, port, timeout=max(0.05, deadline - time.monotonic()))
    host_header = parsed.hostname
    if ":" in host_header:
        host_header = f"[{host_header}]"
    if port != 443:
        host_header = f"{host_header}:{port}"

    last_error = None
    for address in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            last_error = TimeoutError()
            break
        connection = _PinnedHTTPSConnection(
            parsed.hostname, port, address, remaining)
        try:
            connection.request(
                "GET", "/", headers={
                    "Host": host_header, "Accept": "text/html,*/*;q=0.1",
                    "Connection": "close",
                })
            response = connection.getresponse()
            if connection.sock is not None:
                connection.sock.settimeout(max(0.05, deadline - time.monotonic()))
            body = response.read(int(max_body) + 1)
            if len(body) > int(max_body):
                return {"ok": False, "status": response.status,
                        "reason": "response body exceeded the verification limit"}
            return {
                "ok": 200 <= response.status < 300,
                "status": response.status,
                "address": address,
                "redirected": 300 <= response.status < 400,
            }
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            last_error = exc
        finally:
            connection.close()
    return {"ok": False, "status": None,
            "reason": type(last_error).__name__ if last_error else "unavailable"}
