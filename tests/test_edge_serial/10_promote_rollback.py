"""
Release registration source derivation — serial half (maestro #2792).

Both cases install dummy AWS credentials via `th.server_settings` (the real
presign path signs locally against them), which reloads the shared test server,
so they run serially, out of the parallel `test_edge/10_promote_rollback.py`.
"""
from testit import helpers as th

from tests.test_edge._helpers import (
    declare_pools, cleanup, declare_release_buckets, login,
    make_group, make_manifest, make_release, make_webapp,
)


@th.django_unit_setup()
def setup_release_source(opts):
    from mojo.apps.account.models import ApiKey

    cleanup()
    ApiKey.objects.filter(name__startswith="webapp:").delete()
    declare_pools()
    declare_release_buckets()
    opts.group = make_group("edgepromote")


def _use_apikey(opts, token):
    opts.client.logout()
    opts.client.session.headers["Authorization"] = f"apikey {token}"


def _clear_apikey(opts):
    opts.client.session.headers.pop("Authorization", None)


@th.django_unit_test("an interactive session with the WebApp's SAVE_PERMS drives all three release calls")
def test_session_release_register_complete_status(opts):
    """The portal's Upload-a-build tab uses the SAME endpoints CI does, on a
    logged-in session: register -> complete -> deployment status. A holder of
    the WebApp's SAVE_PERMS (manage_dns) passes _authorized_webapp on all
    three with no deploy key linked.

    Register runs the real presign path against dummy AWS credentials (pure
    local signing — the 12_security_review CI happy path's exact pattern).
    Complete cannot HeadObject offline, so it exercises the idempotent
    short-circuit: a release already `uploaded` completes without S3 and
    starts deployment.
    """
    import base64

    from tests.test_edge._helpers import make_group_member

    site = make_webapp(opts.group, slug="sessionrel")
    # A group MEMBER holding manage_dns on the site's own group — the scoped
    # portal admin, not a global grant and not a superuser.
    _, member_email, member_pw, _ = make_group_member(
        ["manage_dns"], group=opts.group)
    login(opts, member_email, member_pw)

    manifest = make_manifest(["index.html", "app.js"])
    with th.server_settings(AWS_KEY="AKIAEDGETESTKEY00000",
                            AWS_SECRET="edge-test-secret-not-real",
                            AWS_REGION="us-west-2"):
        resp = opts.client.post("/api/edge/release", json=dict(
            webapp=site.pk, version="sess1", manifest=manifest))

    assert resp.status_code == 200, (
        "a manage_dns session could not register a release by hand: "
        f"{resp.status_code} {resp.body}")
    data = resp.json.get("data") or {}
    assert data.get("status") == "pending", f"the release is not pending: {data}"
    uploads = data.get("uploads") or []
    assert {u["path"] for u in uploads} == {"index.html", "app.js"}, \
        f"register did not mint one upload per manifest file: {uploads}"
    expected = base64.b64encode(bytes.fromhex(manifest[0]["sha256"])).decode()
    first = next(u for u in uploads if u["path"] == "index.html")
    assert first["headers"]["x-amz-checksum-sha256"] == expected, (
        "the response headers do not carry the base64 checksum the signed "
        f"URL binds: {first['headers']}")

    # Complete an already-verified release: same endpoint, no S3 round trip.
    verified = make_release(site, "sess2", status="uploaded")
    completed = opts.client.post("/api/edge/release/complete", json=dict(
        release=verified.pk))
    assert completed.status_code == 200, (
        "a manage_dns session could not complete a verified release: "
        f"{completed.status_code} {completed.body}")
    completed_data = completed.json.get("data") or {}
    deployment_id = completed_data.get("deployment")
    assert deployment_id, f"completion did not start a deployment: {completed_data}"

    status = opts.client.get(f"/api/edge/release/deployment/{deployment_id}")
    assert status.status_code == 200, (
        "a manage_dns session could not read its own deployment status: "
        f"{status.status_code} {status.body}")
    status_data = status.json.get("data") or {}
    assert status_data.get("webapp") == site.pk, \
        f"the deployment status is for another site: {status_data}"
    assert "terminal" in status_data and "success" in status_data, \
        f"the status payload lost the fields the portal polls on: {status_data}"


@th.django_unit_test("how a release arrived is derived at the boundary, never claimed")
def test_release_source_is_derived_at_the_boundary(opts):
    """`POST edge/release` is the last place that can tell the three ways
    apart. The server decides the CLASS from the credential; the body may only
    refine WITHIN that class, and only on a site actually wired to GitHub.

    Register runs the real presign path against dummy AWS credentials — the
    same local-signing pattern the session test above uses.
    """
    from mojo.apps.edge.models import WebAppRelease
    from mojo.apps.edge.services import webapp_keys
    from tests.test_edge._helpers import make_group_member

    wired = make_webapp(opts.group, slug="sourcewired")
    wired.github_repository = "NativeMojo/example-site"
    wired.save()
    _, _, wired_token, _ = webapp_keys.link(wired)

    bare = make_webapp(opts.group, slug="sourcebare")
    assert not bare.github_repository, \
        "the unwired fixture must have no repository for case (c) to mean anything"
    _, _, bare_token, _ = webapp_keys.link(bare)

    session_site = make_webapp(opts.group, slug="sourcesession")
    _, member_email, member_pw, _ = make_group_member(
        ["manage_dns"], group=opts.group)

    def register(site, version, body_source=None):
        payload = dict(webapp=site.pk, version=version,
                       manifest=make_manifest())
        if body_source is not None:
            payload["source"] = body_source
        resp = opts.client.post("/api/edge/release", json=payload)
        assert resp.status_code == 200, (
            f"register {version} failed: {resp.status_code} {resp.body}")
        # Read the STORED value back, not the response: the field is evidence
        # at rest, and an endpoint echoing its own input would prove nothing.
        return WebAppRelease.objects.get(
            webapp=site, version=version).source

    # One settings block for every case — each entry reloads the server.
    with th.server_settings(AWS_KEY="AKIAEDGETESTKEY00000",
                            AWS_SECRET="edge-test-secret-not-real",
                            AWS_REGION="us-west-2"):
        _use_apikey(opts, wired_token)
        try:
            # (a) A key on a GitHub-wired site, no marker: still just api.
            assert register(wired, "srcA") == "api", \
                "a key-authenticated register with no marker was not labelled api"
            # (b) The same key, with the Action's marker: github.
            assert register(wired, "srcB", "github") == "github", \
                "the shipped Action's marker did not label the release github"
            # (f) A key can never claim the interactive class.
            assert register(wired, "srcF", "upload") == "api", \
                "a machine credential claimed the browser-upload class"
        finally:
            _clear_apikey(opts)

        _use_apikey(opts, bare_token)
        try:
            # (c) The marker on a site with no repository stays honest.
            assert register(bare, "srcC", "github") == "api", \
                "a github marker was honored on a site with no GitHub repository"
        finally:
            _clear_apikey(opts)

        login(opts, member_email, member_pw)
        # (d) The portal's Upload-a-build path is upload by structure alone.
        assert register(session_site, "srcD") == "upload", \
            "an interactive session's release was not labelled upload"
        # (e) A session cannot relabel itself as a GitHub push.
        assert register(session_site, "srcE", "github") == "upload", \
            "an interactive session claimed a release came from a GitHub push"
