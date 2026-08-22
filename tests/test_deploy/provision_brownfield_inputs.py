import shlex

from testit import helpers as th

from .brownfield_fixture import handoff_raw, raw_manifest


def _error(raw):
    from mojo.deploy.provision import brownfield_inputs, inputs
    try:
        brownfield_inputs.validate(raw)
    except inputs.EnvFileError as err:
        return str(err)
    return None


@th.django_unit_test()
def test_manifest_is_strict_secret_free_and_digest_stable(opts):
    from mojo.deploy.provision import brownfield_inputs

    first = brownfield_inputs.validate(raw_manifest())
    second = brownfield_inputs.validate(raw_manifest())
    th.assert_eq(first["manifest_digest"], second["manifest_digest"],
                 "canonical manifests must produce a stable digest")

    unknown = raw_manifest()
    unknown["network"]["guessed_vpc"] = True
    message = _error(unknown)
    th.assert_in("unknown key", message,
                 f"an unknown nested key must fail closed: {message}")

    secret = raw_manifest()
    secret["database"]["credential"]["password"] = "do-not-commit"
    message = _error(secret)
    th.assert_in("secret value", message,
                 f"a credential value must never enter the manifest: {message}")


@th.django_unit_test()
def test_manifest_rejects_ambiguous_roles_versions_and_accounts(opts):
    from mojo.deploy.provision import brownfield_inputs

    duplicate = raw_manifest()
    duplicate["nodes"]["items"][1]["name"] = "maestro-api-1"
    th.assert_in("duplicate", _error(duplicate),
                 "duplicate node names would make exact convergence ambiguous")

    unversioned = raw_manifest()
    unversioned["bootstrap"]["stage1"]["version_id"] = ""
    th.assert_in("missing", _error(unversioned),
                 "a mutable bootstrap object must be refused")

    wrong_account = raw_manifest()
    wrong_account["database"]["cluster_arn"] = wrong_account[
        "database"]["cluster_arn"].replace("123456789012", "999999999999")
    th.assert_in("not 123456789012", _error(wrong_account),
                 "a cross-account dependency ARN must fail before AWS")

    missing_profile = raw_manifest()
    missing_profile["nodes"]["profiles"] = {}
    th.assert_in("exactly one entry per node role", _error(missing_profile),
                 "every opaque role needs one explicit profile decision")

    parsed = brownfield_inputs.parse_arn(
        "arn:aws-us-gov:kms:us-gov-west-1:123456789012:key/abc", "key")
    th.assert_eq(parsed["partition"], "aws-us-gov",
                 f"partition parsing must preserve the complete ARN scope: {parsed}")


@th.django_unit_test()
def test_manifest_rejects_control_characters_in_rendered_values(opts):
    for value in ("bootstrap/evil\ncommand", "bootstrap/evil\rcommand",
                  "bootstrap/evil\x00command"):
        raw = raw_manifest()
        raw["bootstrap"]["stage1"]["key"] = value
        message = _error(raw)
        th.assert_in("control character", message,
                     f"{value!r} must never reach root user data: {message}")


@th.django_unit_test()
def test_manifest_rejects_string_booleans_and_prefix_containment(opts):
    raw = raw_manifest()
    raw["nodes"]["items"][0]["serving_target"] = "false"
    message = _error(raw)
    th.assert_in("JSON boolean", message,
                 f"string truthiness must never select a serving node: {message}")

    raw = raw_manifest()
    raw["storage"]["fleet_config"]["prefix"] = "config/live/shadow"
    message = _error(raw)
    th.assert_in("overlaps read-only config", message,
                 f"a writable child of live config must be rejected: {message}")

    raw = raw_manifest()
    raw["storage"]["fleet_config"]["prefix"] = "config"
    message = _error(raw)
    th.assert_in("overlaps read-only config", message,
                 f"a writable parent of live config must be rejected: {message}")


@th.django_unit_test()
def test_brownfield_dns_boundary_is_explicit_serialized_false(opts):
    missing = raw_manifest()
    missing.pop("manage_dns")
    message = _error(missing)
    th.assert_in("manage_dns", message,
                 f"omission must be ambiguous and fail closed: {message}")

    enabled = raw_manifest()
    enabled["manage_dns"] = True
    message = _error(enabled)
    th.assert_in("explicitly false", message,
                 f"true must never reach a brownfield AWS client: {message}")


@th.django_unit_test()
def test_preserved_eip_contract_is_complete_exact_and_nlb_canaried(opts):
    from mojo.deploy.provision import brownfield_inputs

    raw = handoff_raw(single=False)
    manifest = brownfield_inputs.validate(raw)
    th.assert_eq(manifest["nlb_eip_allocations"], {
        "us-west-2a": "eipalloc-0123456789abcdef0",
        "us-west-2b": "eipalloc-1123456789abcdef0"},
        "one or both selected AZs must normalize without changing allocation ids")

    duplicate = handoff_raw(single=False)
    duplicate["nlb_eip_allocations"]["us-west-2b"] = (
        "eipalloc-0123456789abcdef0")
    th.assert_in("unique", _error(duplicate),
                 "one allocation must never be mapped into two AZs")

    missing_role = handoff_raw()
    missing_role.pop("eip_handoff_role_arn")
    th.assert_in("role", _error(missing_role),
                 "partial handoff declarations must fail closed")

    node_only = handoff_raw()
    node_only["eip_handoff_canaries"][0]["target"] = "node"
    node_only["eip_handoff_canaries"][0]["addresses"] = ["172.31.1.20"]
    message = _error(node_only)
    th.assert_in("NLB shadow/application canary", message,
                 f"node readiness alone cannot prove the public path: {message}")


@th.django_unit_test()
def test_handoff_canary_request_rejects_credential_material(opts):
    requests = (
        "GET / HTTP/1.1\r\nAuthorization: Bearer do-not-store\r\n\r\n",
        "GET / HTTP/1.1\r\nCookie: session=do-not-store\r\n\r\n",
        "GET /?token=do-not-store HTTP/1.1\r\n\r\n",
        "GET / HTTP/1.1\r\nX-Debug: secret=do-not-store\r\n\r\n",
    )
    for request in requests:
        raw = handoff_raw()
        raw["eip_handoff_canaries"][0]["request"] = request
        message = _error(raw)
        th.assert_in(
            "credential-bearing", message,
            f"raw canary credentials must never enter a manifest: {message}")


@th.django_unit_test()
def test_handoff_spec_derives_bounded_journal_coordinates(opts):
    from mojo.deploy.provision import brownfield_inputs

    topology = brownfield_inputs.to_spec(
        brownfield_inputs.validate(handoff_raw()), project_root="/srv/maestro")
    th.assert_eq(topology.manage_dns, False,
                 "the serialized DNS boundary must survive manifest-to-spec")
    th.assert_eq(topology.eip_handoff_local_journal,
                 "/srv/maestro/var/provision/eip-handoffs/"
                 "maestro-prod-shadow.json",
                 "local recovery state must stay below the project root")
    th.assert_eq(topology.eip_handoff_prefix,
                 "fleets/shadow/eip-handoffs/maestro-prod-shadow",
                 "remote recovery state must stay below the fleet-owned prefix")


@th.django_unit_test()
def test_pinned_download_shell_quotes_every_object_value(opts):
    from mojo.deploy.provision import nodes

    attacks = ("bootstrap/it's.sh", "bootstrap/$(touch pwned)",
               "bootstrap/`touch pwned`", "--leading-option")
    for key in attacks:
        reference = {"bucket": "maestro-prod-config", "key": key,
                     "version_id": "version1", "sha256": "a" * 64}
        rendered = "\n".join(nodes._pinned_download_lines(
            reference, "/opt/api/var/stage1.sh", "us-west-2"))
        tokens = shlex.split(rendered.replace("\\\n", " "))
        th.assert_in(f"--key={key}", tokens,
                     f"{key!r} must survive as one --key value: {tokens}")
        th.assert_eq("touch" in tokens, False,
                     f"{key!r} must not split into an executable token: {tokens}")


@th.django_unit_test()
def test_to_spec_is_separate_and_managed_defaults_do_not_move(opts):
    from mojo.deploy.provision import brownfield_inputs, spec as spec_module

    managed = spec_module.build("maestro", "prod", "us-west-2", preset="small")
    before = spec_module.names(managed)
    fleet = brownfield_inputs.to_spec(
        brownfield_inputs.validate(raw_manifest()))
    th.assert_eq(spec_module.names(managed), before,
                 "building a fleet spec must not change managed topology defaults")
    th.assert_eq(spec_module.names(fleet)["nodes"],
                 ["maestro-api-1", "maestro-api-2"],
                 "brownfield nodes must come from exact declarations")
    tags = spec_module.node_tags(fleet, fleet.node_declarations[0])
    th.assert_eq(tags["mojo:fleet"], "shadow",
                 "fleet ownership must be present at resource creation")
    th.assert_eq(tags["mojo:application-role"], "api",
                 "the opaque application role must be tagged at creation")
    th.assert_eq(spec_module.validate_names(fleet), [],
                 "a validated manifest must pass the separate fleet name seam")
    th.assert_eq(fleet.bootstrap_objects["live_config"]["version_id"],
                 "configversion1",
                 "the exact live config must survive manifest-to-spec conversion")
