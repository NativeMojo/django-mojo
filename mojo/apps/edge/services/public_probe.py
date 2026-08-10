"""DNS-pinned public HTTPS probe used by WebApp verification."""

import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urlsplit


class UnsafePublicProbe(ValueError):
    pass


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


def public_addresses(hostname, port=443):
    """Resolve once and reject the entire answer set if any address is unsafe."""
    try:
        answers = socket.getaddrinfo(
            hostname, port, type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP)
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
    return addresses


def probe_https_root(origin, timeout=3.0, max_body=65536):
    """GET exactly ``/`` without redirects through a pinned TLS connection."""
    parsed = urlsplit(origin)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or
            parsed.password or parsed.query or parsed.fragment or
            parsed.path not in ("", "/")):
        raise UnsafePublicProbe("probe requires an HTTPS origin")
    port = parsed.port or 443
    addresses = public_addresses(parsed.hostname, port)
    host_header = parsed.hostname
    if ":" in host_header:
        host_header = f"[{host_header}]"
    if port != 443:
        host_header = f"{host_header}:{port}"

    last_error = None
    for address in addresses:
        connection = _PinnedHTTPSConnection(
            parsed.hostname, port, address, float(timeout))
        try:
            connection.request(
                "GET", "/", headers={
                    "Host": host_header, "Accept": "text/html,*/*;q=0.1",
                    "Connection": "close",
                })
            response = connection.getresponse()
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
