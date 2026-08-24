"""
The node proof REST endpoint — serial half (maestro #2792).

`test_node_proof_rest_no_secret_shape` installs EDGE_NODE_ID via
`th.server_settings`, which reloads the shared test server, so it runs serially,
out of the parallel `test_edge/5_desired_state.py`.
"""
from testit import helpers as th

from tests.test_edge._helpers import cleanup, declare_pools, make_group


@th.django_unit_setup()
def setup_node_proof(opts):
    from mojo.apps.account.models import ApiKey

    cleanup()
    ApiKey.objects.filter(name__startswith="edge_node_test_").delete()
    declare_pools()


def _use_apikey(opts, token):
    opts.client.logout()
    opts.client.session.headers["Authorization"] = f"apikey {token}"


def _clear_apikey(opts):
    opts.client.session.headers.pop("Authorization", None)


@th.django_unit_test("node proof REST response exposes metadata and no secrets")
def test_node_proof_rest_no_secret_shape(opts):
    from mojo.apps.account.models import ApiKey

    group = make_group("edgeproofkey")
    _, token = ApiKey.create_for_group(
        group, "edge_node_test_proof", permissions={"edge_node": True})
    _use_apikey(opts, token)
    try:
        with th.server_settings(EDGE_NODE_ID="edge-rest-proof"):
            resp = opts.client.get("/api/edge/proof?pools=default,staging")
        assert resp.status_code == 200, \
            f"edge node could not read safe local proof: {resp.status_code} {resp.body}"
        proof = resp.json.get("data") or {}
        assert set(proof) == {
            "node_id", "django_mojo_version", "platform_sha",
            "platform_deployment", "observed_at", "pools"}, \
            f"proof grew an unreviewed response surface: {proof}"
        blob = str(proof).lower()
        assert all(marker not in blob for marker in (
            "private_key", "api_key", "credential", "token", "secret")), \
            f"proof response leaked secret-bearing fields: {proof}"
    finally:
        _clear_apikey(opts)
