"""
Static file serving for mojo-auth assets.

Django's static file system is not used in production. These endpoints serve
mojo-auth.js and mojo-auth.css directly from the account app's static directory
with browser caching headers. This is the only static content the framework
serves — everything else comes from CDN or the project's own static pipeline.
"""
import hashlib
import os

from django.http import HttpResponse

from mojo import decorators as md
from mojo.helpers import logit
from mojo.helpers.settings import settings

logger = logit.get_logger('bouncer', 'bouncer.log')

# Resolve the static directory once at import time
_STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'static', 'account')

_CONTENT_TYPES = {
    'js': 'application/javascript; charset=utf-8',
    'css': 'text/css; charset=utf-8',
}

_CACHE_MAX_AGE = settings.get_static('BOUNCER_CACHE_MAX_AGE', 86400)


# filepath -> ((mtime_ns, size), sha256[:10]); recomputed only when the file changes.
_VERSIONS = {}


def _content_version(filepath, content, stat):
    """The first ten hex digits of the file's SHA-256 — the `?v=` the hosted
    templates load it with (tests/test_auth/hosted_script_version.py).

    `stat` must come from the same open handle `content` was read from, so a
    deploy swapping the file mid-request can never store the old hash under
    the new file's key."""
    key = (stat.st_mtime_ns, stat.st_size)
    cached = _VERSIONS.get(filepath)
    if cached and cached[0] == key:
        return cached[1]
    version = hashlib.sha256(content).hexdigest()[:10]
    _VERSIONS[filepath] = (key, version)
    return version


def _serve_static(filename, version=None, debug=None):
    """Serve one account static file.

    `version` is the request's `?v=`. A version that is not this file's
    content hash means the page and the server disagree — a mixed deploy, or
    a page rendered against other files — so that response is `no-store`:
    it must never be cached for a day under a key the page will reuse.
    `debug` defaults to settings.DEBUG (a test seam).
    """
    ext = filename.rsplit('.', 1)[-1] if '.' in filename else ''
    content_type = _CONTENT_TYPES.get(ext, 'application/octet-stream')

    filepath = os.path.join(_STATIC_DIR, filename)
    if not os.path.isfile(filepath):
        return HttpResponse('Not found', status=404, content_type='text/plain')
    with open(filepath, 'rb') as f:
        stat = os.fstat(f.fileno())
        content = f.read()

    resp = HttpResponse(content, content_type=content_type)
    debug = settings.DEBUG if debug is None else debug
    mismatched = bool(version) and version != _content_version(filepath, content, stat)
    if debug or mismatched:
        resp['Cache-Control'] = 'no-store'
    else:
        resp['Cache-Control'] = f'public, max-age={_CACHE_MAX_AGE}'
    return resp


def _version(request):
    return str(getattr(request, 'DATA', {}).get('v', '') or '')


@md.GET('account/static/mojo-auth.js')
@md.public_endpoint("Serves mojo-auth.js — the authentication webapp")
def on_mojo_auth_js(request):
    return _serve_static('mojo-auth.js', _version(request))


@md.GET('account/static/mojo-auth.css')
@md.public_endpoint("Serves mojo-auth.css — authentication stylesheet")
def on_mojo_auth_css(request):
    return _serve_static('mojo-auth.css', _version(request))


@md.GET('account/static/mojo-auth-theme.css')
@md.public_endpoint("Serves mojo-auth-theme.css — responsive hosted-auth presets")
def on_mojo_auth_theme_css(request):
    return _serve_static('mojo-auth-theme.css', _version(request))


@md.GET('account/static/mojo-bouncer.js')
@md.public_endpoint("Serves mojo-bouncer.js — embeddable bot-detection gate")
def on_mojo_bouncer_js(request):
    return _serve_static('mojo-bouncer.js', _version(request))


@md.GET('account/static/mojo-hosted-bouncer.js')
@md.public_endpoint("Hosted recovery controls and scoped form-token provider")
def on_mojo_hosted_bouncer_js(request):
    return _serve_static('mojo-hosted-bouncer.js', _version(request))


@md.GET('account/static/mojo-bouncer.css')
@md.public_endpoint("Serves mojo-bouncer.css — bouncer overlay stylesheet")
def on_mojo_bouncer_css(request):
    return _serve_static('mojo-bouncer.css', _version(request))


@md.GET('account/static/mojo-sentinel.js')
@md.public_endpoint("Serves mojo-sentinel.js — in-session bot-detection telemetry")
def on_mojo_sentinel_js(request):
    return _serve_static('mojo-sentinel.js', _version(request))
