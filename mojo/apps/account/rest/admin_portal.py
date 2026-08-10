"""Private delivery and bootstrap boundary for the built-in Admin portal."""

import mimetypes
import secrets
from urllib.parse import quote

from django.http import HttpResponse, HttpResponseRedirect

import mojo
from mojo import decorators as md
from mojo.apps.account.services import admin_assets, admin_features, admin_portal
from mojo.helpers.response import JsonResponse


_ADMIN_ROOT = f"/{admin_portal.ADMIN_PATH}"
_ADMIN_ROOT_SLASH = f"/{admin_portal.ADMIN_PATH}/"
_ADMIN_ASSET = f"/{admin_portal.ADMIN_PATH}/<path:asset>"
_ADMIN_SESSION = f"/{admin_portal.ADMIN_PATH}/_session"


def _headers(response, *, csp=None):
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    response["X-Content-Type-Options"] = "nosniff"
    response["Referrer-Policy"] = "same-origin"
    response["X-Frame-Options"] = "DENY"
    if csp:
        response["Content-Security-Policy"] = csp
    return response


def _gate():
    nonce = secrets.token_urlsafe(18)
    auth_path = "/auth?redirect=" + quote(_ADMIN_ROOT_SLASH, safe="/")
    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Admin access</title><style nonce="{nonce}">html{{color-scheme:light dark}}body{{margin:0;font:14px system-ui;background:#f5f7fa;color:#172033}}main{{max-width:360px;margin:18vh auto;padding:28px;background:Canvas;border:1px solid #d9dee8;border-radius:14px}}h1{{font-size:20px;margin:0 0 8px}}p{{line-height:1.5;color:#667085}}button{{font:inherit;padding:9px 14px;border:0;border-radius:8px;background:#4665e8;color:white;cursor:pointer}}</style></head>
<body><main><h1>Admin access</h1><p id="message">Checking your secure session…</p><button id="signin" hidden>Continue to sign in</button></main>
<script src="/api/account/static/mojo-auth.js"></script><script nonce="{nonce}">
(function(){{var msg=document.getElementById('message'),btn=document.getElementById('signin');var auth={auth_path!r};
function signIn(){{location.assign(auth)}}btn.onclick=signIn;
if(!window.MojoAuth){{msg.textContent='Authentication is unavailable.';return}}
MojoAuth.init({{baseURL:location.origin}});
var token=MojoAuth.getToken();if(!token||MojoAuth.getTokenType()!=='bearer'){{signIn();return}}
fetch('/api/account/admin/session',{{method:'POST',headers:{{Authorization:'Bearer '+token,'Content-Type':'application/json'}},body:'{{}}'}})
.then(function(r){{if(r.ok){{location.reload();return}}if(r.status===401&&MojoAuth.getRefreshToken()){{return MojoAuth.refreshToken().then(function(){{location.reload()}})}}throw new Error(r.status===403?'Your account does not have Admin access.':'Your session could not be verified.')}})
.catch(function(e){{msg.textContent=e.message;btn.hidden=false;btn.textContent='Sign in again'}})
}})();</script></body></html>"""
    response = HttpResponse(body, content_type="text/html; charset=utf-8")
    return _headers(
        response,
        csp=("default-src 'none'; style-src 'nonce-%s'; script-src 'self' "
             "'nonce-%s'; connect-src 'self'; base-uri 'none'; "
             "form-action 'none'; frame-ancestors 'none'") % (nonce, nonce),
    )


def _private_file(asset):
    path = admin_assets.asset_path(asset)
    if path is None:
        return _not_found()
    try:
        content = path.read_bytes()
    except (OSError, ValueError):
        return _not_found()
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if path.suffix in (".html", ".js", ".css"):
        content_type += "; charset=utf-8"
    response = HttpResponse(content, content_type=content_type)
    csp = None
    if path.suffix == ".html":
        csp = ("default-src 'self'; img-src 'self' data:; style-src 'self'; "
               "script-src 'self'; connect-src 'self'; base-uri 'none'; "
               "form-action 'self'; frame-ancestors 'none'")
    return _headers(response, csp=csp)


def _not_found():
    return _headers(HttpResponse(
        "Not found", status=404, content_type="text/plain; charset=utf-8"))


@md.GET(_ADMIN_ROOT)
@md.GET(_ADMIN_ROOT_SLASH)
@md.public_endpoint("Admin source gate; private bytes require a source session")
def on_admin_root(request):
    if request.path.rstrip("/") == _ADMIN_ROOT and not request.path.endswith("/"):
        return _headers(HttpResponseRedirect(_ADMIN_ROOT_SLASH))
    if admin_portal.validate(request) is None:
        return _gate()
    return _private_file("index.html")


@md.GET(_ADMIN_ASSET)
@md.public_endpoint("Admin assets return 404 without a valid source session")
def on_admin_asset(request, asset=None):
    if admin_portal.validate(request) is None:
        return _not_found()
    return _private_file(asset or "")


@md.POST("account/admin/session")
@md.denies_key_backed_session()
@md.requires_global_perms("view_admin", "manage_users", "admin")
def on_admin_session(request):
    session_id = admin_portal.issue(request)
    if session_id is None:
        return JsonResponse({"status": False, "error": "interactive JWT required"}, status=401)
    response = JsonResponse({"status": True, "data": {"path": _ADMIN_ROOT_SLASH}})
    admin_portal.set_cookie(response, session_id)
    return _headers(response)


@md.DELETE(_ADMIN_SESSION)
@md.public_endpoint("Revokes only the caller's path-scoped Admin source session")
def on_admin_session_revoke(request):
    admin_portal.revoke(request)
    response = JsonResponse({"status": True})
    admin_portal.delete_cookie(response)
    return _headers(response)


@md.GET("account/admin/bootstrap")
@md.denies_key_backed_session()
@md.requires_global_perms("view_admin", "manage_users", "admin")
def on_admin_bootstrap(request):
    memberships = request.user.members.filter(is_active=True).select_related("group")
    groups = [
        {"id": member.group_id, "name": member.group.name}
        for member in memberships if member.group.is_effectively_active()
    ]
    has = request.user.has_permission
    capabilities = {
        "setup": bool(request.user.is_superuser),
        "people": has(["view_users", "manage_users", "admin"]),
        "groups": has(["view_groups", "manage_groups", "admin"]),
        "network": has(["view_dns", "manage_dns", "security"]),
        "manage_network": has(["manage_dns", "security"]),
        "webapps": has(["view_dns", "manage_dns", "security"]),
        "manage_webapps": (
            has("manage_webapp") and has(["manage_dns", "security"])),
    }
    return {
        "version": mojo.__version__,
        "admin_path": _ADMIN_ROOT_SLASH,
        "user": {
            "id": request.user.pk,
            "display_name": request.user.display_name,
            "email": request.user.email,
            "is_superuser": request.user.is_superuser,
        },
        "groups": groups,
        "capabilities": capabilities,
        "features": admin_features.bootstrap_features(request, capabilities),
    }
