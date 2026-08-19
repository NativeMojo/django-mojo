"""Stage-0 user data: small, credential-free, and pointing where the CLI puts things.

User data is the one part of a node that CANNOT be changed after launch, and it
is readable by anything on the box that can reach IMDS plus anyone with EC2
read access to the account. So there are exactly three things worth asserting
about it, and all three are here: it fits, it carries no key material, and the
URI it downloads from is the same URI the CLI uploads to.

That last one is a two-sided contract with `storage.ensure_bootstrap_payload`,
and both sides read `spec.names()` — the assertion is that neither side went
and built the string itself.
"""

from testit import helpers as th


REGION = "us-west-2"
PROJECT = "demo"
ENV = "prod"


def _spec(**overrides):
    from mojo.deploy.provision import spec as spec_module

    return spec_module.build(PROJECT, ENV, REGION,
                             preset=overrides.pop("preset", "micro"),
                             **overrides)


def _bootstrap_conf(text):
    """The key=value block the user data writes, parsed back out of it."""
    values = {}
    inside = False
    for line in text.splitlines():
        if line.startswith("cat > ") and "bootstrap.conf" in line:
            inside = True
            continue
        if inside:
            if line.strip() == "MOJOCONF":
                break
            if "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    return values


@th.django_unit_test("stage-0 user data fits EC2's limit with room to spare")
def test_user_data_is_small(opts):
    from mojo.deploy.provision import nodes

    script = nodes.stage0_user_data(_spec(), "demo1")
    size = len(script.encode("utf-8"))

    th.assert_true(size < nodes.USER_DATA_LIMIT,
                   f"user data is {size} bytes against EC2's hard "
                   f"{nodes.USER_DATA_LIMIT}-byte limit — a launch would be "
                   f"rejected after the network and the database already exist")
    th.assert_true(size <= nodes.USER_DATA_BUDGET,
                   f"user data is {size} bytes, past this package's own "
                   f"{nodes.USER_DATA_BUDGET}-byte budget. Anything that grows "
                   f"belongs in stage1.sh, which is downloaded and can be "
                   f"changed without replacing every instance")


@th.django_unit_test("stage-0 writes the five bootstrap.conf keys the config plane audits")
def test_user_data_writes_bootstrap_conf(opts):
    from mojo.deploy.provision import nodes
    from mojo.deploy.provision import spec as spec_module

    spec = _spec()
    names = spec_module.names(spec)
    script = nodes.stage0_user_data(spec, "demo1")
    values = _bootstrap_conf(script)

    th.assert_eq(values.get("AWS_REGION"), REGION,
                 "config_sync needs the region to build its S3 client")
    th.assert_eq(values.get("AWS_CONFIG_BUCKET"), names["config_bucket"],
                 "the bucket must be the one this environment publishes to")
    th.assert_eq(values.get("AWS_CONFIG_PREFIX"), names["config_prefix"],
                 "config_sync refuses to guess the prefix, so stage 0 must "
                 "state it — and it must match where render.py publishes")
    th.assert_eq(values.get("CONFIG_SYNC_OWNER"), "ec2-user:www",
                 "check_node.check_config_plane audits nodes against exactly "
                 "this owner: the app user writes django.conf, the web user "
                 "reads it, and 0600 would strand one of them")
    th.assert_eq(values.get("CONFIG_SYNC_RESTART"), "true",
                 "a published config that never restarts the app is a config "
                 "the fleet is not running")

    th.assert_true("install -m 0600 /dev/null" in script,
                   "bootstrap.conf must be created 0600 before anything is "
                   "written into it — it is the file the config plane's audit "
                   "checks the mode of")


@th.django_unit_test("stage-0 user data carries no credential of any kind")
def test_user_data_has_no_key_material(opts):
    from mojo.deploy.provision import nodes

    script = nodes.stage0_user_data(_spec(), "demo1")

    for forbidden in ("AWS_KEY", "AWS_SECRET", "AWS_ACCESS_KEY", "AKIA",
                      "aws_secret_access_key", "PRIVATE KEY", "password"):
        th.assert_true(forbidden not in script,
                       f"{forbidden} must never appear in user data — it is "
                       f"readable from IMDS by anything on the box and echoed "
                       f"back by describe-instance-attribute. The node reads "
                       f"S3 with its instance role instead")


@th.django_unit_test("stage-0 downloads stage 1 from exactly where the CLI publishes it")
def test_user_data_s3_uri_matches_the_upload(opts):
    from mojo.deploy.provision import nodes
    from mojo.deploy.provision import spec as spec_module

    spec = _spec()
    names = spec_module.names(spec)
    script = nodes.stage0_user_data(spec, "demo1")

    uri = f"s3://{names['config_bucket']}/{names['stage1_script_object']}"
    th.assert_true(uri in script,
                   f"user data must fetch {uri} — the same object "
                   f"storage.ensure_bootstrap_payload publishes. Both sides "
                   f"read spec.names(); a string built by hand on either side "
                   f"is a node that boots and downloads a 404")
    th.assert_true("exec bash /opt/api/var/stage1.sh" in script,
                   "stage 0 must hand over to stage 1 — everything of any size "
                   "lives there so it can be changed without replacing a node")


@th.django_unit_test("a node is never launched before its stage-1 payload exists")
def test_nodes_refuse_to_launch_without_the_payload(opts):
    from mojo.deploy.provision import discover, nodes

    class _EC2:
        def run_instances(self, **kwargs):
            raise AssertionError(
                "an instance must not be launched while its boot payload is "
                "unpublished — it would boot, bill, and do nothing")

        # Reserving the address is not the launch and is not what this test is
        # about; a reserved elastic IP with no instance is the normal
        # mid-bootstrap state.
        def allocate_address(self, **kwargs):
            return {"PublicIp": "203.0.113.7", "AllocationId": "eipalloc-1"}

        def associate_address(self, **kwargs):
            return {}

    clients = discover.Clients(session=None, ec2=_EC2())
    observed = discover.blank()
    observed.account_id = "123456789012"
    observed.ami_id = "ami-0123456789abcdef0"
    observed.public_subnet_ids = ["subnet-aaa"]
    observed.node_sg_id = "sg-aaa"
    # bootstrap_payload deliberately absent.

    findings, actions, result = nodes.ensure_nodes(
        clients, _spec(), observed, apply=True)

    codes = [finding.code for finding in findings]
    th.assert_true("node.payload" in codes,
                   f"the refusal must be reported, not silent — got {codes}")
    th.assert_eq(result.as_dict().get("instance_ids"), [],
                 "and no instance id may be reported")
