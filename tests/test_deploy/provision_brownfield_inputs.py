import shlex

from testit import helpers as th

from .brownfield_fixture import preserved_raw, raw_manifest


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

    # The same allowlist is what a stale manifest hits: a field this
    # django-mojo no longer implements is refused by name rather than
    # silently ignored, so nobody believes a retired control is still
    # enforced by something.
    retired = raw_manifest()
    retired["retired_cutover_role_arn"] = (
        "arn:aws:iam::123456789012:role/retired")
    message = _error(retired)
    th.assert_in("unknown key", message,
                 f"an unknown top-level key must fail closed: {message}")
    th.assert_in("retired_cutover_role_arn", message,
                 f"the refusal must name the offending field: {message}")

    secret = raw_manifest()
    secret["database"]["credential"]["password"] = "do-not-commit"
    message = _error(secret)
    th.assert_in("secret value", message,
                 f"a credential value must never enter the manifest: {message}")


@th.django_unit_test()
def test_balancer_health_paths_are_exact_digest_bound_and_defaulted(opts):
    from mojo.deploy.provision import brownfield_inputs
    from mojo.deploy.provision import spec as spec_module

    plain = brownfield_inputs.validate(raw_manifest())
    plain_spec = brownfield_inputs.to_spec(plain)
    th.assert_eq(plain_spec.api_health_path, spec_module.HEALTH_PATH_DEFAULT,
                 "an omitted API path must preserve the managed default")
    th.assert_eq(plain_spec.certbot_health_path,
                 spec_module.HEALTH_PATH_DEFAULT,
                 "an omitted certbot path must preserve the managed default")

    raw = raw_manifest()
    raw["load_balancer"]["api_health_path"] = "/api/maestro/node/ready"
    raw["load_balancer"]["certbot_health_path"] = "/certbot/ready"
    declared = brownfield_inputs.validate(raw)
    topology = brownfield_inputs.to_spec(declared)
    th.assert_eq(declared["load_balancer"]["api_health_path"],
                 "/api/maestro/node/ready",
                 "normalization must preserve the exact declared API path")
    th.assert_eq(declared["load_balancer"]["certbot_health_path"],
                 "/certbot/ready",
                 "normalization must preserve the exact declared certbot path")
    th.assert_eq(topology.api_health_path, "/api/maestro/node/ready",
                 "the API path must survive manifest-to-spec conversion")
    th.assert_eq(topology.certbot_health_path, "/certbot/ready",
                 "the certbot path must survive manifest-to-spec conversion")
    th.assert_true(declared["manifest_digest"] != plain["manifest_digest"],
                   "changing a health path must change the canonical manifest digest")


@th.django_unit_test()
def test_balancer_health_paths_reject_urls_injection_and_bad_lengths(opts):
    from mojo.deploy.provision import spec as spec_module

    invalid = (
        "", "api/ready", "https://maestromojo.com/api/ready",
        "//maestromojo.com/api/ready", "/api/ready?deep=1",
        "/api/ready#fragment", "/api ready", "/api/ready\nnext",
        "/api/ready\x00next", "/" + "a" * spec_module.HEALTH_PATH_MAX,
    )
    for value in invalid:
        raw = raw_manifest()
        raw["load_balancer"]["api_health_path"] = value
        message = _error(raw)
        th.assert_true(message is not None,
                       f"invalid health path {value!r} must fail closed")
        th.assert_in("api_health_path", message,
                     f"the rejection must name the invalid field: {message}")

    maximum = raw_manifest()
    maximum["load_balancer"]["api_health_path"] = (
        "/" + "a" * (spec_module.HEALTH_PATH_MAX - 1))
    th.assert_eq(_error(maximum), None,
                 "ELBv2's documented 1024-character maximum must be accepted")


@th.django_unit_test()
def test_balancer_client_ip_posture_is_strict_optional_and_digest_bound(opts):
    from mojo.deploy.provision import brownfield_inputs

    plain = brownfield_inputs.validate(raw_manifest())
    plain_spec = brownfield_inputs.to_spec(plain)
    th.assert_eq(plain_spec.api_preserve_client_ip, None,
                 "omission must preserve AWS/framework behavior")
    th.assert_eq(plain_spec.certbot_preserve_client_ip, None,
                 "the HTTP target group must also remain undeclared by default")
    th.assert_eq(plain_spec.nlb_security_group_id, None,
                 "omission must preserve the legacy NLB create request")

    raw = raw_manifest()
    raw["load_balancer"]["api_preserve_client_ip"] = False
    raw["load_balancer"]["certbot_preserve_client_ip"] = True
    declared = brownfield_inputs.validate(raw)
    topology = brownfield_inputs.to_spec(declared)
    th.assert_eq(topology.api_preserve_client_ip, False,
                 "an explicit false value must survive normalization")
    th.assert_eq(topology.certbot_preserve_client_ip, True,
                 "an explicit true value must survive normalization")
    th.assert_true(declared["manifest_digest"] != plain["manifest_digest"],
                   "client-IP posture must be bound into the manifest digest")

    for invalid in (None, 0, 1, "false", "true", [], {}):
        bad = raw_manifest()
        bad["load_balancer"]["api_preserve_client_ip"] = invalid
        message = _error(bad)
        th.assert_in("api_preserve_client_ip", message,
                     f"non-boolean {invalid!r} must fail closed: {message}")

    secured = raw_manifest()
    secured["load_balancer"]["security_group_id"] = (
        "sg-3123456789abcdef0")
    secured_manifest = brownfield_inputs.validate(secured)
    secured_spec = brownfield_inputs.to_spec(secured_manifest)
    th.assert_eq(secured_spec.nlb_security_group_id,
                 "sg-3123456789abcdef0",
                 "the irreversible NLB create-time SG must reach the spec")
    th.assert_true(
        secured_manifest["manifest_digest"] != plain["manifest_digest"],
        "the exact NLB security group must be manifest-digest bound")
    for invalid in (None, "", "sg-short", "SG-3123456789abcdef0",
                    "sg-3123456789abcdefg", 7):
        bad = raw_manifest()
        bad["load_balancer"]["security_group_id"] = invalid
        message = _error(bad)
        th.assert_in("security_group_id", message,
                     f"invalid NLB SG {invalid!r} must fail closed: {message}")


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
def test_node_request_service_is_strict_optional_and_digest_bound(opts):
    from mojo.deploy.provision import brownfield_inputs

    omitted = brownfield_inputs.validate(raw_manifest())
    th.assert_true(
        "request_service" not in omitted["nodes"]["items"][0],
        "normalization must not rewrite an omitted compatibility declaration")

    for selected in (True, False):
        raw = raw_manifest()
        raw["nodes"]["items"][0]["request_service"] = selected
        declared = brownfield_inputs.validate(raw)
        topology = brownfield_inputs.to_spec(declared)
        th.assert_eq(
            topology.node_declarations[0]["request_service"], selected,
            f"the exact {selected!r} lifecycle must survive manifest-to-spec")
        th.assert_true(
            declared["manifest_digest"] != omitted["manifest_digest"],
            "an explicit per-node lifecycle must change the manifest digest")

    for invalid in ("false", 0, 1, None, [], {}):
        raw = raw_manifest()
        raw["nodes"]["items"][0]["request_service"] = invalid
        message = _error(raw)
        th.assert_in(
            "request_service", message,
            f"invalid request_service {invalid!r} must name its field: {message}")
        th.assert_in(
            "JSON boolean", message,
            f"invalid request_service {invalid!r} must fail closed: {message}")


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
def test_preserved_allocations_are_optional_and_validated_exactly(opts):
    from mojo.deploy.provision import brownfield_inputs

    th.assert_eq(_error(raw_manifest()), None,
                 "omitting nlb_eip_allocations must remain a valid manifest")
    th.assert_true(
        "nlb_eip_allocations" not in brownfield_inputs.validate(raw_manifest()),
        "an omitted declaration must not be invented during normalization")

    one_az = brownfield_inputs.validate(preserved_raw())
    th.assert_eq(one_az["nlb_eip_allocations"],
                 {"us-west-2a": "eipalloc-0123456789abcdef0"},
                 "a single-AZ declaration must normalize unchanged")
    two_az = brownfield_inputs.validate(preserved_raw(single=False))
    th.assert_eq(two_az["nlb_eip_allocations"], {
        "us-west-2a": "eipalloc-0123456789abcdef0",
        "us-west-2b": "eipalloc-1123456789abcdef0"},
        "both selected AZs must normalize without changing allocation ids")

    duplicate = preserved_raw(single=False)
    duplicate["nlb_eip_allocations"]["us-west-2b"] = (
        "eipalloc-0123456789abcdef0")
    th.assert_in("unique", _error(duplicate),
                 "one allocation must never be mapped into two AZs")

    outside = preserved_raw()
    outside["nlb_eip_allocations"] = {
        "us-west-2c": "eipalloc-0123456789abcdef0"}
    message = _error(outside)
    th.assert_in("outside the selected", message,
                 f"an AZ with no selected NLB subnet must fail: {message}")


@th.django_unit_test()
def test_malformed_preserved_allocations_are_bounded_field_named_errors(opts):
    # Every one of these used to reach a set() or a regex with a value that
    # could not be hashed or matched. A raw TypeError escaping validation
    # would leave the CLI's bounded EnvFileError boundary and traceback.
    shapes = (
        ({}, "non-empty"),
        ([], "non-empty"),
        ("eipalloc-0123456789abcdef0", "non-empty"),
        ({"us-west-2a": []}, "nlb_eip_allocations.us-west-2a"),
        ({"us-west-2a": {}}, "nlb_eip_allocations.us-west-2a"),
        ({"us-west-2a": ["eipalloc-0123456789abcdef0"]},
         "nlb_eip_allocations.us-west-2a"),
        ({"us-west-2a": 7}, "nlb_eip_allocations.us-west-2a"),
        ({"us-west-2a": None}, "nlb_eip_allocations.us-west-2a"),
        ({"us-west-2a": "eni-0123456789abcdef0"},
         "is not an eipalloc id"),
        ({"us-west-2a": "eipalloc-nothex"}, "is not an eipalloc id"),
        ({"": "eipalloc-0123456789abcdef0"}, "invalid AZ"),
        ({" us-west-2a": "eipalloc-0123456789abcdef0"}, "invalid AZ"),
    )
    for value, expected in shapes:
        raw = preserved_raw()
        raw["nlb_eip_allocations"] = value
        message = _error(raw)
        th.assert_true(message is not None,
                       f"{value!r} must be rejected, not accepted silently")
        th.assert_in("nlb_eip_allocations", message,
                     f"{value!r} must be rejected by field name: {message}")
        th.assert_in(expected, message,
                     f"{value!r} must name why it is wrong: {message}")


@th.django_unit_test()
def test_preserved_allocations_survive_manifest_to_spec(opts):
    from mojo.deploy.provision import brownfield_inputs

    topology = brownfield_inputs.to_spec(
        brownfield_inputs.validate(preserved_raw()),
        project_root="/srv/maestro")
    th.assert_eq(topology.manage_dns, False,
                 "the serialized DNS boundary must survive manifest-to-spec")
    th.assert_eq(topology.nlb_eip_allocations,
                 {"us-west-2a": "eipalloc-0123456789abcdef0"},
                 "balancer preparation reads this declaration off the spec")

    plain = brownfield_inputs.to_spec(
        brownfield_inputs.validate(raw_manifest()))
    th.assert_eq(plain.nlb_eip_allocations, {},
                 "an omitted declaration must reach the spec as empty, so "
                 "preparation allocates fixed addresses as before")


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
    th.assert_true("mojo:request-service" not in tags,
                   "omission must preserve the pre-feature provider tag shape")
    explicit = dict(fleet.node_declarations[0], request_service=True)
    explicit_tags = spec_module.node_tags(fleet, explicit)
    th.assert_eq(explicit_tags["mojo:request-service"], "true",
                 "an explicit framework request role must be tagged at launch")
    th.assert_eq(spec_module.validate_names(fleet), [],
                 "a validated manifest must pass the separate fleet name seam")
    th.assert_eq(fleet.bootstrap_objects["live_config"]["version_id"],
                 "configversion1",
                 "the exact live config must survive manifest-to-spec conversion")
