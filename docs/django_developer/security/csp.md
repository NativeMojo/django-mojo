# Content Security Policy — Hosted Auth Pages

> **This is OPT-IN and OFF by default.** `AUTH_CSP_ENABLED` ships **`False`**,
> so no `Content-Security-Policy` header is sent on any page unless your
> deployment asks for one. Everything below describes what you get **once you
> turn it on**.

The framework can emit a `Content-Security-Policy` header on the four pages it
hosts itself. The policy is nonce-based: `script-src` carries a fresh
per-request nonce and **no `'unsafe-inline'`**, so an injected `<script>` — one
that reached the page through a template variable, a reflected parameter, or a
stored value — has no nonce and cannot execute.

Implementation: `mojo/apps/account/services/csp.py`.

---

## Turning it on

```python
# settings/production.py — step 1: observe
AUTH_CSP_ENABLED = True
AUTH_CSP_REPORT_ONLY = True
```

1. Set **both** `AUTH_CSP_ENABLED = True` **and** `AUTH_CSP_REPORT_ONLY = True`.
   The browser evaluates the policy and reports violations but enforces nothing.
2. Load `/auth`, `/register`, `/passkey` and `/contact` and watch the browser
   console (or a `report-uri` added through `AUTH_CSP_DIRECTIVES`) for blocked
   inline scripts. **This matters most if you override the auth templates or add
   JS through `{% block page_script %}`** — see
   [Rolling this out](#rolling-this-out-on-a-deployment-with-overridden-templates).
3. Fix what shows up: stamp `nonce="{{ csp_nonce }}"` on every inline `<script>`
   your overrides introduce.
4. Drop `AUTH_CSP_REPORT_ONLY` (or set it `False`). The policy is now enforced.

### Why it is off by default

A CSP can only ever *break* things — a deployment that overrides an auth
template with its own inline `<script>`, or iframes the login page, would start
failing with no warning — and that is not a cost to impose on someone who never
asked for the header.

The `nonce="{{ csp_nonce }}"` attributes are stamped into the shipped templates
**either way**. A nonce with no CSP is inert, so the default really is a no-op:
nothing about the markup, the endpoints, or the framing behavior of any page
changes until you set `AUTH_CSP_ENABLED = True`.

---

## Which responses carry the header

**When `AUTH_CSP_ENABLED = True`** (with it unset or `False`, every row below is
"no"):

| Response | CSP | Why |
|---|---|---|
| `GET /auth` (login page) | yes, `frame-ancestors 'none'` | holds tokens in `localStorage` |
| `GET /register` | yes, `frame-ancestors 'none'` | same |
| `GET /passkey` | yes, `frame-ancestors 'none'` | same |
| `GET /contact` | yes, **no `frame-ancestors`** | documented as iframe-embeddable |
| `bouncer_challenge.html` / `bouncer_decoy.html` | **no** | un-nonce'd inline content by design |
| `token_landing_base.html` and the three landings it feeds (`email_verify_landing.html`, `email_change_landing.html`, `account_deactivate_landing.html`) | **no** | same — standalone pages, un-nonce'd inline `<style>`/`<script>` by design |
| every JSON API response | **no** | not a document |

The paths above are the defaults; they follow `BOUNCER_LOGIN_PATH`,
`BOUNCER_REGISTER_PATH`, `BOUNCER_PASSKEY_PATH` and `BOUNCER_CONTACT_PATH`.

`/auth`, `/register` and `/contact` are bouncer-gated: a cold client is served
the challenge page first, and **the challenge page carries no CSP**. The header
appears on the real page — after the challenge, or immediately when the client
presents a valid `mbp` pass cookie. `/passkey` is not gated, so its first
response already carries the header.

**Non-goal.** This is not a framework-wide CSP and must not become one. A nonce
is only valid for a response whose markup was stamped with the same value, so
blanket middleware cannot know which templates are nonce-aware — it would
silently break every un-nonce'd inline block in the framework. Consuming
applications own the CSP for their own pages.

---

## The default policy

```
default-src 'self';
base-uri 'none';
object-src 'none';
frame-ancestors 'none';           ← omitted on /contact
form-action 'self';
script-src 'self' 'nonce-<32 hex>';
style-src 'self' 'unsafe-inline' https: [<api_origin>];
img-src * data:;
font-src 'self' data:;
connect-src 'self' [<api_origin>]
```

Directives are emitted in a fixed order, so the header is byte-stable and
straightforward to assert on.

| Directive | Rationale |
|---|---|
| `default-src 'self'` | backstop for anything not named below |
| `base-uri 'none'` | no `<base>` tag exists in any auth template; an injected one would repoint every relative URL |
| `object-src 'none'` | no `<object>`/`<embed>` in any auth template |
| `frame-ancestors` | see [below](#frame-ancestors-is-per-page) |
| `form-action 'self'` | every form on these pages is JS-handled with no `action=` attribute |
| `script-src 'self' 'nonce-…'` | the headline control — see [the nonce contract](#the-nonce-contract) |
| `style-src` | permissive, see below |
| `img-src * data:` | permissive, see below |
| `font-src 'self' data:` | no CDN fonts, no `@import`, no `@font-face` in the theme CSS |
| `connect-src` | `mojo-auth.js` fetches only its `baseURL`; `contact.html` posts to the same origin |

`<api_origin>` is the `scheme://host[:port]` of `theme.api_base`, added only
when it is an absolute URL. It exists because a deployment may serve the hosted
pages from one host and the API from another: the theme stylesheet
(`mojo-auth-theme.css`) and every `fetch` then cross an origin boundary.

There is **no `report-uri`/`report-to` by default** — the framework ships no
collection endpoint. Add one through `AUTH_CSP_DIRECTIVES` (below).

### `frame-ancestors` is per page

`'none'` on **login / register / passkey**. Those pages hold access and refresh
tokens in `localStorage`, and nothing else stops an origin from framing them —
the framework ships no `X-Frame-Options` either, so with `AUTH_CSP_ENABLED`
unset or `False` these pages remain framable by any origin. Enabling the header
is what closes that.

**Omitted entirely on `/contact`.** Embedding `/contact?kind=<kind>` in an
iframe is the documented least-work integration for an external marketing site
(see `docs/web_developer/account/public_messages.md`). Adding `frame-ancestors`
there through `AUTH_CSP_DIRECTIVES` breaks that option; if you need it, prefer
naming the specific parents (`frame-ancestors https://www.example.com`) over
`'none'`.

### Why `style-src` and `img-src` are permissive

`style-src` keeps `'unsafe-inline'` because the auth templates use style
attributes in ten places, plus `<style>{{ custom_css }}</style>` for the
tenant's runtime CSS override. **Do not add a nonce to `style-src`**: a nonce
there makes browsers ignore `'unsafe-inline'`, which would break every one of
those style attributes. `https:` covers a tenant `custom_css_url`, which is
https-validated but can point at any host.

`img-src * data:` because `theme.logo_url`, `theme.hero_image_url` and
`theme.favicon_url` are tenant-supplied with no scheme or host validation. Any
origin restriction there guarantees whitelabel breakage for near-zero security
gain once `script-src` is nonce-locked. `data:` is included because
`validate_custom_css` explicitly permits data URIs.

The security value of this policy is concentrated in `script-src`,
`frame-ancestors`, `form-action`, `base-uri` and `object-src`. Those are strict.

---

## The nonce contract

`_auth_context()` mints one nonce per request:

```python
ctx['csp_nonce'] = secrets.token_hex(16)
```

`_render_with_csp()` renders the template and stamps the **same** value into the
header, so markup and policy can never disagree. Every inline `<script>` in
`auth_base.html` and its children carries it:

```html
<script nonce="{{ csp_nonce }}">
```

**If you override any of these templates, or add inline JS through
`{% block page_script %}` or `{% block extra_css %}`, you must stamp the nonce
yourself.** `csp_nonce` is always present in the context of these four pages.

Two things deliberately do **not** get a nonce:

- **`{{ x|json_script:"id" }}` blocks.** Django emits them as
  `<script id="…" type="application/json">`. A non-JS `type` makes the element a
  *data block*: HTML's prepare-the-script-element algorithm returns before the
  CSP inline-behavior check, so it is neither executed nor policy-checked, and
  it is read via `.textContent`, which CSP never inspects.
- **Style attributes and `<style>` tags.** See `style-src` above.

The external `<script src="{{ api_base }}/api/account/static/mojo-auth.js">` tag
**does** get the nonce. A nonce match authorizes an external load regardless of
origin, which is why a cross-origin `api_base` needs no host source in
`script-src`.

> Unrelated name collision: `render_ctx.css_nonce` on the bouncer challenge page
> is an anti-automation class-name randomizer, not a CSP nonce.

---

## Settings

All three are read with `settings.get_static` — the Django settings **file**
only — and are read **per request**, so a change takes effect on the next
response without a code change.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `AUTH_CSP_ENABLED` | bool | **`False`** | **`True` → send the header.** The opt-in switch; with it unset or `False` no CSP header is sent on any page |
| `AUTH_CSP_REPORT_ONLY` | bool | `False` | `True` → send `Content-Security-Policy-Report-Only` instead of the enforcing header. Only meaningful alongside `AUTH_CSP_ENABLED = True` |
| `AUTH_CSP_DIRECTIVES` | dict | `{}` | per-directive merge over the defaults |

They are deliberately **not** readable from the DB/Redis settings plane. A
security header is a deploy-time decision, and a `Setting` row — writable
through the generic `/api/settings` REST endpoints — must never be able to
weaken it. Same precedent as `MOJO_TEST_MODE` and
`AUTH_PHONE_VERIFY_DEV_BYPASS_CODE`.

### `AUTH_CSP_DIRECTIVES` merge rules

- A present key **replaces** that directive wholesale.
- An **empty value drops** the directive.
- An **unknown key is emitted as-is**, so you can add `report-uri`,
  `report-to`, `upgrade-insecure-requests`, etc.
- Values may be a string or a list of sources.
- **The per-request nonce is always appended to the final `script-src`** and
  cannot be removed — emptying `script-src` yields a nonce-only `script-src`.
  Leaving `AUTH_CSP_ENABLED` unset (or setting it `False`) is the only opt-out.

```python
# settings/production.py
AUTH_CSP_DIRECTIVES = {
    # add your own script host alongside the nonce
    "script-src": "'self' https://cdn.example.com",
    # collect violation reports
    "report-uri": "https://csp.example.com/report",
    # drop a directive entirely
    "font-src": "",
}
```

---

## Rolling this out on a deployment with overridden templates

Nothing here happens on upgrade — the header is off until you enable it. This
is the risk you take on **when you do**. If your project ships its own
`account/auth_base.html`, `login.html`, `register.html`, `passkey_enroll.html`
or `contact.html` — or adds inline JS through `{% block page_script %}` — those
scripts have no nonce and **will not run** once the policy is enforced.

Recommended sequence:

1. `AUTH_CSP_ENABLED = True` **and** `AUTH_CSP_REPORT_ONLY = True`. The browser
   evaluates the policy and reports violations but enforces nothing. Add a
   `report-uri` through `AUTH_CSP_DIRECTIVES`, or just watch the browser
   console. Exercise all four pages, including the framed `/contact` embed if
   you use one.
2. Add `nonce="{{ csp_nonce }}"` to every inline `<script>` your overrides
   introduce. Leave `json_script` tags alone.
3. Remove `AUTH_CSP_REPORT_ONLY`.

Setting `AUTH_CSP_ENABLED = False` (or removing it) puts you back to the shipped
default — no header at all. That is the rollback if something you missed breaks
in production, not the destination.

---

## Related Documentation

- [Hosted Auth Pages](../account/auth_pages.md) — templates, blocks, theming, overrides
- [Bouncer Architecture](../account/bouncer.md) — the gate in front of `/auth`, `/register`, `/contact`
- [Settings Reference](../helpers/settings_reference.md) — `AUTH_CSP_*` keys
- [Web Developer: Auth Pages](../../web_developer/account/auth_pages.md) — the consumer-facing view
- [Web Developer: Public Messages](../../web_developer/account/public_messages.md) — the `/contact` iframe integration
