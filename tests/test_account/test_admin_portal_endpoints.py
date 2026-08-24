"""Every /api/edge/ path the admin portal calls must resolve to a registered endpoint.

The rest of the portal's asset contracts read the JavaScript as text and assert
what it says. None of them ask whether a URL the portal calls actually exists —
so a mistyped path is invisible until a person clicks the control and the panel
renders an error.

That is not hypothetical. The Deploys tab shipped calling
`/api/edge/webapp/release` and `/api/edge/webapp/deployment`; both resources are
registered at the top level (`/api/edge/release`, `/api/edge/deployment`), so
both 404'd and the tab had never once loaded. It was reported as "clicking
Deploys does nothing", which is exactly what a rejected fetch with no error
state looks like.

Scope: the `edge` prefix only. There, `@md.URL('webapp')` in the `edge` app maps
to `/api/edge/webapp` — the app prefix is added, the decorator carries the rest
(`mojo/decorators/http.py:258-268`). The `account` app's decorators already
carry an `account/` segment of their own, so the same arithmetic does not apply
and auditing it needs that prefix resolution worked out first. A narrow test
that is right beats a broad one that cries wolf.
"""

import re
from pathlib import Path

from testit import helpers as th

TESTIT_TIER = "admin"


ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "mojo/apps/account/admin_portal/assets"
EDGE_REST = ROOT / "mojo/apps/edge/rest"

# @md.URL('x'), @md.GET("x"), @md.POST('x') — the three that declare a path.
DECORATOR = re.compile(r"""@md\.(?:URL|GET|POST)\(\s*['"]([^'"]+)['"]""")

# A call the portal makes, up to the query string. A `${...}` interpolation is
# kept as a marker so it matches a `<int:pk>` segment instead of truncating.
#
# `<...>` is in the character class on purpose: comments document endpoints in
# their Django form (`/api/edge/release/deployment/<id>`), and a matcher that
# stopped at the `<` would truncate the path and report the comment as a broken
# call. A documented path is held to the same contract as a coded one.
SEGMENT = r"[A-Za-z0-9_/\-]"
CALL = re.compile(
    r"""/api/edge/({s}*(?:(?:\$\{{[^}}]*\}}|<[^>]+>){s}*)*)""".format(s=SEGMENT))

# Any placeholder — `<int:pk>` from a route, `<id>` from a doc comment.
CONVERTER = re.compile(r"<[^>]+>")


def _normalize(path):
    """Collapse converters and interpolations to one wildcard token per segment.

    `webapp/<int:pk>` and `webapp/${encodeURIComponent(app.id)}` both become
    `webapp/*`, so a declared dynamic segment matches a call that fills it.
    """
    path = CONVERTER.sub("*", path.strip("/"))
    path = re.sub(r"\$\{[^}]*\}", "*", path)
    return "/".join("*" if "*" in seg else seg
                    for seg in path.split("/") if seg != "")


@th.django_unit_test("every portal call to /api/edge/ resolves to a registered endpoint")
def test_portal_edge_calls_are_registered_contract(opts):
    assert EDGE_REST.is_dir(), \
        "the edge rest package moved — this test is auditing a directory that no longer exists"

    registered = set()
    for module in sorted(EDGE_REST.glob("*.py")):
        for raw in DECORATOR.findall(module.read_text()):
            registered.add(_normalize(raw))
    assert registered, \
        "no endpoints parsed from the edge rest package — the decorator pattern stopped " \
        "matching, so this test would pass by finding nothing"

    calls = {}
    for asset in sorted(ASSETS.rglob("*.js")):
        for path in CALL.findall(asset.read_text()):
            calls.setdefault(_normalize(path), set()).add(
                asset.relative_to(ASSETS).as_posix())
    assert calls, \
        "no /api/edge/ calls found in the portal assets — the call pattern stopped matching"

    unresolved = [f"/api/edge/{path} called from {', '.join(sorted(sources))}"
                  for path, sources in sorted(calls.items()) if path not in registered]
    assert not unresolved, \
        "the portal calls edge paths that are not registered, so they 404 at runtime and " \
        "the panel renders an error the moment someone clicks:\n  " + "\n  ".join(unresolved)


@th.django_unit_test("the Deploys tab reads release and deployment as top-level resources")
def test_deploys_tab_endpoint_paths_contract(opts):
    """The specific regression, pinned by name.

    `mojo/apps/edge/rest/web_app.py` groups these with WebApp in one module, but
    registers them as `release` and `deployment` — the module grouping is not the
    URL shape, and reading one off the other is what produced the bug.
    """
    page = (ASSETS / "features/webapps/page.js").read_text()

    assert "/api/edge/release?webapp=" in page, \
        "the Deploys tab no longer reads releases from /api/edge/release"
    assert "/api/edge/deployment?webapp=" in page, \
        "the Deploys tab no longer reads deploy history from /api/edge/deployment"
    assert "/api/edge/webapp/release" not in page, \
        "the Deploys tab reads /api/edge/webapp/release, which is not registered — " \
        "releases live at /api/edge/release, so this 404s and the tab never loads"
    assert "/api/edge/webapp/deployment" not in page, \
        "the Deploys tab reads /api/edge/webapp/deployment, which is not registered — " \
        "deployments live at /api/edge/deployment, so this 404s and the tab never loads"
