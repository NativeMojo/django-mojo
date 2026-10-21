"""Retained stdlib HTTP owner. Missing integration is never a success fixture.

Production entry binds one fixed UDS and loads only fixed root-owned policy.
The nonroot loopback fixture below cannot enable any operational authority.
"""
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import os
from pathlib import Path
import re
import secrets
import socket
import socketserver
import stat
import threading
import time
from urllib.parse import urlsplit

if __package__:
    from . import acme_hub_operator_protocol as wire
    from . import acme_hub_operator_profile as profiles
    from . import acme_hub_operator_runtime as runtime
else:
    import acme_hub_operator_protocol as wire
    import acme_hub_operator_profile as profiles
    import acme_hub_operator_runtime as runtime

SOCKET_PATH = '/run/mojo-acme-operator/http.sock'
ASSET_ROOT = runtime.ROOT + '/ui'
ASSETS = {'': ('index.html', 'text/html; charset=utf-8'),
          'index.html': ('index.html', 'text/html; charset=utf-8'),
          'api.js': ('api.js', 'text/javascript; charset=utf-8'),
          'wizard.js': ('wizard.js', 'text/javascript; charset=utf-8'),
          'styles.css': ('styles.css', 'text/css; charset=utf-8')}
COOKIE = '__Host-mojo_acme_operator'
SESSION_SECONDS = 900
MAX_SESSIONS = 128
ERROR_STATUS = {'invalid_request': 400, 'invalid_frame': 400, 'origin_refused': 403,
    'csrf_refused': 403, 'not_authenticated': 401, 'operator_not_authorized': 403,
    'reauth_required': 403, 'generation_changed': 409, 'replay_refused': 409,
    'request_expired': 410}


class Sessions:
    def __init__(self):
        self.values = {}
        self.lock = threading.RLock()

    def get(self, headers):
        cookies = []
        for raw in headers.get_all('Cookie', []):
            cookies.extend(item.strip().split('=', 1) for item in raw.split(';') if '=' in item)
        found = [value for key, value in cookies if key == COOKIE]
        wire.require(len(found) <= 1, 'session_unavailable')
        token = found[0] if found else None
        wire.require(token is None or re.fullmatch(r'[0-9a-f]{64}', token), 'session_unavailable')
        now = time.monotonic()
        with self.lock:
            self.values = {key: item for key, item in self.values.items() if item['expires'] > now}
            if token not in self.values:
                wire.require(len(self.values) < MAX_SESSIONS, 'session_unavailable')
                token = secrets.token_hex(32)
                self.values[token] = {'csrf': secrets.token_hex(32), 'expires': now + SESSION_SECONDS}
            return token, self.values[token]

    def revoke(self, token):
        with self.lock:
            self.values.pop(token, None)


class Guardian:
    """Pure-phase real owner: cannot enable deferred ceremony by injection."""
    def __init__(self):
        self.sessions = Sessions()
        self.revision = 0
        self.profile = None
        try:
            self.profile = profiles.load()
        except wire.Refused:
            pass

    def status(self, session, fixture=False):
        result = wire.unavailable_status(self.profile, session['csrf'])
        if fixture:
            result['facts'][0]['items'][0]['classification'] = 'fixture'
            result['facts'][0]['source'] = 'unprivileged_source_fixture'
        return result

    def dispatch(self, method, route, body, token, session, fixture=False):
        if method == 'GET' and route == 'status':
            return self.status(session, fixture)
        if method == 'GET' and route == 'bootstrap/status':
            return self.status(session, fixture)['bootstrap']
        wire.require(method == 'POST', 'invalid_request')
        if route == 'logout':
            wire.fields(body, ())
            self.sessions.revoke(token)
            return wire.unavailable_status(self.profile)
        if route == 'session/options':
            wire.validate_options(body)
            wire.require(body['expected_revision'] == self.revision, 'generation_changed')
            raise wire.Refused('dependent_integration_unavailable')
        if route == 'session/verify':
            wire.validate_assertion(body)
            raise wire.Refused('dependent_integration_unavailable')
        if route in wire.ACTIONS:
            wire.validate_action(route, body)
            wire.require(body['expected_revision'] == self.revision, 'generation_changed')
            # No callable/provider/DB/UID fake seam: real coordinator+account
            # integration must be reviewed before this dispatcher can enable it.
            raise wire.Refused('dependent_integration_unavailable')
        if route == 'bootstrap/candidate':
            wire.fields(body, ('account_uuid', 'credential_id', 'profile_id', 'expected_revision', 'replay_key'))
            wire.uuid_text(body['account_uuid']); wire.text(body['credential_id'], 2048)
            wire.identifier(body['profile_id']); wire.uuid_text(body['replay_key'])
            wire.require(wire.revision(body['expected_revision']) == self.revision, 'generation_changed')
            raise wire.Refused('database_enrollment_unavailable')
        if route == 'bootstrap/possession':
            wire.fields(body, ('candidate_id', 'expected_revision', 'replay_key'))
            wire.uuid_text(body['candidate_id']); wire.uuid_text(body['replay_key'])
            wire.require(wire.revision(body['expected_revision']) == self.revision, 'generation_changed')
            raise wire.Refused('dependent_integration_unavailable')
        if route == 'bootstrap/approve-envelope':
            wire.fields(body, ('manifest', 'oidc_assertion'))
            wire.require(type(body['manifest']) is dict)
            wire.text(body['oidc_assertion'], 16384)
            raise wire.Refused('independent_approval_unavailable')
        raise wire.Refused('invalid_request')


class Handler(BaseHTTPRequestHandler):
    server_version = 'MojoGuardian'
    sys_version = ''
    protocol_version = 'HTTP/1.0'

    def log_message(self, *args):
        # Never log URLs/body/cookies/assertions/provider material.
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(3)

    def _headers(self, status, content_type, length, token=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(length))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Cross-Origin-Opener-Policy', 'same-origin')
        self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        self.send_header('Permissions-Policy', 'publickey-credentials-get=(self), publickey-credentials-create=()')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'; worker-src 'none'; form-action 'self'")
        if token is not None:
            self.send_header('Set-Cookie', f'{COOKIE}={token}; Secure; HttpOnly; SameSite=Strict; Path=/')
        self.end_headers()

    def _json(self, data=None, error=None, token=None):
        raw = wire.encoded(wire.envelope(data, error, self.server.guardian.revision))
        self._headers(200 if error is None else ERROR_STATUS.get(error, 503), 'application/json', len(raw), token)
        self.wfile.write(raw)

    def _origin(self, api=False, machine=False):
        hosts = self.headers.get_all('Host', [])
        expected = urlsplit(self.server.origin).netloc
        wire.require(len(hosts) == 1 and hosts[0] == expected, 'origin_refused')
        if not self.server.fixture:
            wire.require(self.headers.get_all('X-Mojo-Guardian-Transport', []) == ['https'], 'origin_refused')
        origins = self.headers.get_all('Origin', [])
        wire.require(len(origins) <= 1 and (not origins or origins[0] == self.server.origin), 'origin_refused')
        if api:
            wire.require(self.headers.get_all('Sec-Fetch-Site', []) == ['same-origin'], 'origin_refused')
        if machine:
            wire.require(not origins and not self.headers.get_all('Sec-Fetch-Site', [])
                         and not self.headers.get_all('Cookie', []), 'origin_refused')
        elif self.command == 'POST':
            wire.require(origins == [self.server.origin], 'origin_refused')

    def _route(self):
        parsed = urlsplit(self.path)
        wire.require(not parsed.scheme and not parsed.netloc and not parsed.fragment, 'invalid_request')
        if parsed.path.startswith(wire.API_PREFIX):
            wire.require(not parsed.query, 'invalid_request')
            return True, parsed.path[len(wire.API_PREFIX):]
        wire.require(parsed.path.startswith(wire.PREFIX), 'invalid_request')
        name = parsed.path[len(wire.PREFIX):]
        wire.require(name in ASSETS, 'invalid_request')
        if parsed.query:
            wire.require(name == '' and parsed.query.startswith('operation='), 'invalid_request')
            wire.uuid_text(parsed.query[len('operation='):])
        return False, name

    def do_GET(self):
        try:
            api, route = self._route()
            self._origin(api)
            if api:
                token, session = self.server.guardian.sessions.get(self.headers)
                self._json(self.server.guardian.dispatch('GET', route, None, token, session, self.server.fixture), token=token)
            else:
                name, content_type = ASSETS[route]
                if self.server.fixture:
                    path = self.server.assets / name
                    wire.require(path.is_file() and not path.is_symlink(), 'runtime_unavailable')
                    raw = path.read_bytes()
                    wire.require(0 < len(raw) <= 1024 * 1024, 'runtime_unavailable')
                else:
                    raw = runtime.read_owned(ASSET_ROOT + '/' + name, 0o444, 1024 * 1024)
                self._headers(200, content_type, len(raw))
                self.wfile.write(raw)
        except (wire.Refused, OSError) as error:
            self._json(error=error.code if isinstance(error, wire.Refused) else 'runtime_unavailable')

    def do_POST(self):
        try:
            api, route = self._route()
            wire.require(api)
            machine = route == 'bootstrap/approve-envelope'
            self._origin(not machine, machine=machine)
            wire.require(not self.headers.get_all('Transfer-Encoding', []))
            lengths = self.headers.get_all('Content-Length', [])
            wire.require(len(lengths) == 1 and lengths[0].isdigit())
            length = int(lengths[0]); wire.require(0 < length <= wire.MAX_FRAME)
            wire.require(self.headers.get_all('Content-Type', []) == ['application/json'])
            token, session = (None, None) if machine else self.server.guardian.sessions.get(self.headers)
            if not machine:
                csrf = self.headers.get_all('X-Mojo-Acme-CSRF', [])
                wire.require(len(csrf) == 1 and secrets.compare_digest(csrf[0], session['csrf']), 'csrf_refused')
            raw = self.rfile.read(length)
            wire.require(len(raw) == length)
            data = self.server.guardian.dispatch('POST', route, wire.decode(raw), token, session, self.server.fixture)
            self._json(data, token=token)
        except (wire.Refused, OSError) as error:
            self._json(error=error.code if isinstance(error, wire.Refused) else 'session_unavailable')

    def do_OPTIONS(self):
        self._json(error='origin_refused')


class RetainedServer(socketserver.UnixStreamServer):
    def __init__(self):
        wire.require(os.geteuid() == 0, 'operator_not_authorized')
        self.guardian = Guardian()
        wire.require(self.guardian.profile is not None, 'profile_unavailable')
        self.origin = self.guardian.profile['wizard_origin']
        self.fixture = False
        self.assets = Path(ASSET_ROOT)
        parent = runtime.open_parent(str(Path(SOCKET_PATH).parent))
        try:
            # Never unlink another running/stale owner's socket automatically.
            wire.require(not os.path.lexists(SOCKET_PATH), 'session_unavailable')
            super().__init__(SOCKET_PATH, Handler)
            os.chmod(SOCKET_PATH, 0o600)
        finally:
            os.close(parent)


class UnavailableSourceFixture(HTTPServer):
    """Nonroot loopback visual/refusal harness. Cannot enroll or enable actions."""
    def __init__(self, assets):
        wire.require(os.geteuid() != 0, 'operator_not_authorized')
        self.guardian = Guardian()
        self.guardian.profile = None
        self.fixture = True
        self.assets = Path(assets).resolve(strict=True)
        super().__init__(('127.0.0.1', 0), Handler)
        self.origin = 'http://127.0.0.1:' + str(self.server_port)


def main():
    wire.require(os.geteuid() == 0, 'operator_not_authorized')
    with RetainedServer() as server:
        server.serve_forever(poll_interval=0.2)


if __name__ == '__main__':
    try:
        main()
    except (wire.Refused, OSError):
        raise SystemExit('guarded service unavailable') from None
