"""
Static file serving for mojo-auth assets.

Django's static file system is not used in production. These endpoints serve
mojo-auth.js and mojo-auth.css directly from the account app's static directory
with browser caching headers. This is the only static content the framework
serves — everything else comes from CDN or the project's own static pipeline.
"""
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


def _serve_static(filename):
    ext = filename.rsplit('.', 1)[-1] if '.' in filename else ''
    content_type = _CONTENT_TYPES.get(ext, 'application/octet-stream')

    filepath = os.path.join(_STATIC_DIR, filename)
    if not os.path.isfile(filepath):
        return HttpResponse('Not found', status=404, content_type='text/plain')
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    resp = HttpResponse(content, content_type=content_type)
    if settings.DEBUG:
        resp['Cache-Control'] = 'no-store'
    else:
        resp['Cache-Control'] = f'public, max-age={_CACHE_MAX_AGE}'
    return resp


@md.GET('account/static/mojo-auth.js')
@md.public_endpoint("Serves mojo-auth.js — the authentication webapp")
def on_mojo_auth_js(request):
    return _serve_static('mojo-auth.js')


@md.GET('account/static/mojo-auth.css')
@md.public_endpoint("Serves mojo-auth.css — authentication stylesheet")
def on_mojo_auth_css(request):
    return _serve_static('mojo-auth.css')


@md.GET('account/static/mojo-auth-theme.css')
@md.public_endpoint("Serves mojo-auth-theme.css — dark premium auth theme")
def on_mojo_auth_theme_css(request):
    return _serve_static('mojo-auth-theme.css')
