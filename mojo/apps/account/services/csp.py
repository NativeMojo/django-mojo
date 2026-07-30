"""
Content-Security-Policy for the framework-hosted auth pages.

Scope — deliberately narrow. Only the four pages rendered from
`account/auth_base.html` carry this header: the login, register, passkey
enrollment and contact pages. Those are the highest-value pages the framework
serves (login/register/passkey hold access and refresh tokens in
`localStorage`), and they are the only templates stamped with a nonce.

This is NOT a framework-wide default and must not become one. A nonce is only
valid for a response whose markup carries the same value, so blanket middleware
cannot know which templates are nonce-aware — it would silently break every
un-nonce'd inline block (`bouncer_challenge.html`, `bouncer_decoy.html`,
`email_change_confirm.html`, `email_verify_confirm.html`). Consuming
applications own the CSP for their own pages.

(Unrelated name collision: `render_ctx.css_nonce` on the bouncer challenge page
is an anti-automation class-name randomizer, not a CSP nonce.)

The nonce contract
------------------
`_auth_context()` mints one `csp_nonce` per request; the render helper stamps
the same value into the header. Every inline `<script>` in a template extending
`auth_base.html` — including any deployment override or `{% block page_script %}`
addition — must carry `nonce="{{ csp_nonce }}"` or it will not execute.

`{{ x|json_script:"id" }}` tags get NO nonce. Django emits them as
`type="application/json"`, which makes the element a *data block*: HTML's
prepare-the-script-element algorithm returns before the CSP inline-behavior
check, so it is neither executed nor policy-checked, and it is read via
`.textContent`, which CSP never inspects.

Why `style-src` and `img-src` are permissive
--------------------------------------------
`style-src` keeps `'unsafe-inline'` because the auth templates use style
attributes in ten places plus `<style>{{ custom_css|safe }}</style>`. Do NOT add
a nonce to `style-src`: a nonce there makes browsers ignore `'unsafe-inline'`,
which would break every one of those style attributes. `https:` covers a tenant
`custom_css_url` (already https-validated by `auth_config._validate_https_url`).

`img-src * data:` because `logo_url` / `hero_image_url` / `favicon_url` are
tenant-supplied with no scheme or host validation — any origin restriction
guarantees whitelabel breakage for near-zero gain once `script-src` is
nonce-locked. `data:` because `validate_custom_css` explicitly permits data URIs.

`frame-ancestors` is PER PAGE
-----------------------------
`'none'` on login / register / passkey. **Omitted entirely on `/contact`** —
`docs/web_developer/account/public_messages.md` documents embedding
`/contact?kind=<kind>` in an iframe as the recommended integration for an
external marketing site. Callers pass `frame_ancestors=""` to drop the
directive.

Settings (all file-only, all read PER REQUEST) — **OPT-IN, off by default**
--------------------------------------------------------------------------
| Key | Default | Meaning |
|---|---|---|
| `AUTH_CSP_ENABLED`     | `False` | `True` → send the header |
| `AUTH_CSP_REPORT_ONLY` | `False` | `True` → `Content-Security-Policy-Report-Only` |
| `AUTH_CSP_DIRECTIVES`  | `{}`    | per-directive merge over the defaults |

`AUTH_CSP_ENABLED` ships **False** so upgrading changes nothing. A CSP can only
break things — a deployment that overrides an auth template with its own inline
`<script>`, or iframes the login page, would start failing with no warning — and
that is not a cost to impose on someone who never asked for the header. The
nonce attributes are stamped into the templates either way; a `nonce` with no
CSP is inert, so the default really is a no-op.

Rollout, when you do want it: set `AUTH_CSP_ENABLED = True` **and**
`AUTH_CSP_REPORT_ONLY = True`, watch your browser console / report endpoint for
blocked inline scripts, fix or nonce them, then drop `AUTH_CSP_REPORT_ONLY`.

Merge semantics: a present key REPLACES that directive wholesale; an empty
value DROPS the directive; an unknown key is emitted as-is (so a deployment can
add `report-uri`). The per-request nonce is always appended to the final
`script-src` and cannot be removed — `AUTH_CSP_ENABLED=False` is the opt-out.

All three are read with `settings.get_static` (settings-FILE only), never
`settings.get(...)`: a security header is a deploy-time decision and a
DB/Redis `Setting` row — writable through the generic `/api/settings` REST
plane — must never be able to weaken it. Same precedent as `MOJO_TEST_MODE`.

They are read inside `default_directives()` / `build_policy()` / `apply()` on
every call, NOT at module import. Import-time reads would make the settings
undeployable without a process restart.
"""
from urllib.parse import urlsplit

from mojo.helpers.settings import settings

HEADER = "Content-Security-Policy"
REPORT_ONLY_HEADER = "Content-Security-Policy-Report-Only"

# Fixed emission order, so the header is byte-stable and assertable.
DIRECTIVE_ORDER = (
    "default-src",
    "base-uri",
    "object-src",
    "frame-ancestors",
    "form-action",
    "script-src",
    "style-src",
    "img-src",
    "font-src",
    "connect-src",
)


def _clean(value):
    """Normalize a directive value to a single-line string.

    A list/tuple is joined with spaces (a JSON settings file naturally spells a
    source list that way). CR/LF are stripped: a directive value reaches an HTTP
    header, and a newline there is a response-splitting primitive.
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = " ".join(str(v) for v in value)
    else:
        value = str(value)
    return value.replace("\r", " ").replace("\n", " ").strip()


def _origin(url):
    """`scheme://netloc` for an absolute URL; `""` for empty or relative."""
    if not url:
        return ""
    try:
        parts = urlsplit(str(url))
    except Exception:
        return ""
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


def default_directives(api_base="", frame_ancestors="'none'"):
    """The shipped baseline policy, as an ordered directive dict.

    `api_base` may be cross-origin (the hosted pages can be served by a
    different host than the API). Its origin is added to `style-src` (the theme
    stylesheet) and `connect-src` (every `fetch` mojo-auth.js and contact.html
    issue). It is deliberately NOT added to `script-src` — the nonce on the
    `mojo-auth.js` tag already authorizes that load regardless of origin.

    `frame_ancestors` falsy → the directive is dropped (see the module docstring).
    """
    api_origin = _origin(api_base)
    style_src = "'self' 'unsafe-inline' https:"
    connect_src = "'self'"
    if api_origin:
        style_src = f"{style_src} {api_origin}"
        connect_src = f"{connect_src} {api_origin}"
    return {
        "default-src": "'self'",
        "base-uri": "'none'",
        "object-src": "'none'",
        "frame-ancestors": _clean(frame_ancestors),
        "form-action": "'self'",
        "script-src": "'self'",
        "style-src": style_src,
        "img-src": "* data:",
        "font-src": "'self' data:",
        "connect-src": connect_src,
    }


def build_policy(nonce, api_base="", frame_ancestors="'none'"):
    """Return the finished policy string for one response.

    baseline → merge `AUTH_CSP_DIRECTIVES` → append the nonce to `script-src`
    → emit `DIRECTIVE_ORDER` first, then any extra keys, skipping empty values.
    """
    directives = default_directives(api_base=api_base, frame_ancestors=frame_ancestors)

    overrides = settings.get_static('AUTH_CSP_DIRECTIVES', {}, kind="dict")
    if isinstance(overrides, dict):
        for key, value in overrides.items():
            name = _clean(key).lower()
            if name:
                directives[name] = _clean(value)

    # The nonce is not negotiable — a deployment that empties or omits
    # script-src still gets a nonce-only script-src. AUTH_CSP_ENABLED is the
    # only off switch.
    script_src = directives.get("script-src", "")
    nonce_source = f"'nonce-{nonce}'"
    directives["script-src"] = f"{script_src} {nonce_source}".strip()

    parts = []
    for name in DIRECTIVE_ORDER:
        value = directives.get(name, "")
        if value:
            parts.append(f"{name} {value}")
    for name, value in directives.items():
        if name in DIRECTIVE_ORDER:
            continue
        if value:
            parts.append(f"{name} {value}")
    return "; ".join(parts)


def apply(response, nonce, api_base="", frame_ancestors="'none'"):
    """Stamp the policy onto `response` and return it.

    Returns the response untouched unless `AUTH_CSP_ENABLED` is true — the
    header is OPT-IN, so this is a no-op for a deployment that has not asked
    for it. Sets the report-only header instead of the enforcing one when
    `AUTH_CSP_REPORT_ONLY` is true — the recommended first step for a
    deployment with overridden auth templates.
    """
    if not settings.get_static('AUTH_CSP_ENABLED', False, kind="bool"):
        return response
    policy = build_policy(nonce, api_base=api_base, frame_ancestors=frame_ancestors)
    if settings.get_static('AUTH_CSP_REPORT_ONLY', False, kind="bool"):
        response[REPORT_ONLY_HEADER] = policy
    else:
        response[HEADER] = policy
    return response
