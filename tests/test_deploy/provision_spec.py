"""The topology as data: presets, names, tags, validation, cost.

Nothing here touches AWS, which is the point — `spec.py` is what `plan.apply`
runs as step 0, before the first API call, so every rule it enforces has to be
checkable without one.
"""

from testit import helpers as th


PROJECT = "wmx"
ENV = "prod"
REGION = "us-west-2"


def _spec(**overrides):
    from mojo.deploy.provision import spec as spec_module
    preset = overrides.pop("preset", "small")
    return spec_module.build(PROJECT, ENV, REGION, preset=preset, **overrides)


@th.django_unit_test("every preset is internally coherent")
def test_presets_are_coherent(opts):
    from mojo.deploy.provision import spec as spec_module

    for name in spec_module.PRESET_NAMES:
        preset = spec_module.PRESETS[name]
        th.assert_true(preset["node_count"] >= 1,
                       f"preset {name} must run at least one node")
        th.assert_true(preset["db_readers"] >= 0,
                       f"preset {name} cannot have negative readers")
        th.assert_true(preset["cache_replicas"] >= 0,
                       f"preset {name} cannot have negative cache replicas")
        th.assert_true(preset["node_type"] in spec_module.COST_TABLE,
                       f"preset {name} uses node type {preset['node_type']} "
                       f"which has no price in COST_TABLE — the estimate would "
                       f"silently report it as free")
        th.assert_true(preset["db_type"] in spec_module.COST_TABLE,
                       f"preset {name} db type {preset['db_type']} has no price")
        th.assert_true(preset["cache_type"] in spec_module.COST_TABLE,
                       f"preset {name} cache type {preset['cache_type']} has no "
                       f"price")

    th.assert_eq(spec_module.PRESETS["micro"]["node_count"], 1,
                 "micro is the single-node preset")
    th.assert_eq(spec_module.PRESETS["micro"]["balancer"], False,
                 "micro must not build a load balancer — one node has nothing "
                 "to balance across")
    th.assert_true(spec_module.PRESETS["small"]["balancer"],
                   "small runs two nodes and must have a balancer, or the "
                   "second node is unreachable")


@th.django_unit_test("two availability zones, because Aurora requires two")
def test_az_count_is_two(opts):
    from mojo.deploy.provision import spec as spec_module

    th.assert_eq(spec_module.AZ_COUNT, 2,
                 "an Aurora DB subnet group spans at least two AZs; anything "
                 "less cannot create a cluster at all")
    names = spec_module.names(_spec())
    th.assert_eq(len(names["public_subnets"]), spec_module.AZ_COUNT,
                 "one public subnet per zone")
    th.assert_eq(len(names["private_subnets"]), spec_module.AZ_COUNT,
                 "one private subnet per zone")


@th.django_unit_test("an instance type resolves to the right architecture")
def test_architecture_for(opts):
    from mojo.deploy.provision import spec as spec_module

    cases = {
        "t3.small": "x86_64",
        "t3.medium": "x86_64",
        "m6i.large": "x86_64",
        "c5.xlarge": "x86_64",
        "t4g.medium": "arm64",
        "db.t4g.medium": "arm64",
        "db.r6g.xlarge": "arm64",
        "cache.t4g.micro": "arm64",
        "cache.r7g.large": "arm64",
        "c7gn.large": "arm64",
    }
    for instance_type, expected in cases.items():
        th.assert_eq(spec_module.architecture_for(instance_type), expected,
                     f"{instance_type} resolves to the wrong architecture — "
                     f"the AMI parameter is chosen from this, so a wrong "
                     f"answer produces an instance that will not boot")


@th.django_unit_test("wants_balancer follows the preset, the override and the node count")
def test_wants_balancer(opts):
    from mojo.deploy.provision import spec as spec_module

    th.assert_eq(spec_module.wants_balancer(_spec(preset="micro")), False,
                 "micro must not want a balancer")
    th.assert_eq(spec_module.wants_balancer(_spec(preset="small")), True,
                 "small must want a balancer")
    th.assert_eq(
        spec_module.wants_balancer(_spec(preset="micro", want_balancer=True)),
        True, "an explicit want_balancer=True must override the preset")
    th.assert_eq(
        spec_module.wants_balancer(_spec(preset="small", want_balancer=False)),
        False, "an explicit want_balancer=False must override the preset")


@th.django_unit_test("names() carries the three log groups, and they are the only source")
def test_names_carry_the_log_groups(opts):
    from mojo.deploy.provision import spec as spec_module

    names = spec_module.names(_spec())
    th.assert_eq(sorted(names["log_groups"]), ["app", "cloud-init", "nginx"],
                 "exactly three log groups are declared")
    th.assert_eq(names["log_groups"]["nginx"], "/mojo/wmx-prod/nginx",
                 "the nginx log group name is what the node agent config is "
                 "generated from — it cannot drift")
    th.assert_eq(names["log_groups"]["app"], "/mojo/wmx-prod/app",
                 "the app log group name is wrong")
    th.assert_eq(names["log_groups"]["cloud-init"], "/mojo/wmx-prod/cloud-init",
                 "the cloud-init log group name is wrong")
    th.assert_eq(names["log_group_prefix"], "/mojo/wmx-prod",
                 "the prefix is what the node role's policy is scoped to")
    for name in names["log_groups"].values():
        th.assert_true(name.startswith(names["log_group_prefix"] + "/"),
                       f"{name} must sit under the prefix the IAM policy "
                       f"grants, or the agent cannot write to it")


@th.django_unit_test("hostnames follow the existing fleet convention")
def test_hostnames(opts):
    from mojo.deploy.provision import spec as spec_module

    names = spec_module.names(_spec(preset="medium"))
    th.assert_eq(names["nodes"], ["wmx1", "wmx2", "wmx3", "wmx4"],
                 "hostnames are project + 1-based index, which is what "
                 "PRIMARY_BALANCER_HOST in django.conf is compared against")


@th.django_unit_test("the tag set carries the mojo namespace, the legacy keys and managed-by")
def test_tags(opts):
    from mojo.deploy.provision import spec as spec_module

    tags = spec_module.TAGS(_spec(), "node")
    th.assert_eq(tags["mojo:project"], PROJECT, "mojo:project must be the slug")
    th.assert_eq(tags["mojo:env"], ENV, "mojo:env must be the slug")
    th.assert_eq(tags["mojo:role"], "node", "mojo:role must be the role")
    th.assert_eq(tags["Project"], PROJECT,
                 "the legacy Project key is what existing dashboards read")
    th.assert_eq(tags["Env"], ENV, "the legacy Env key is missing")
    th.assert_eq(tags["managed-by"], "django-mojo",
                 "managed-by must match mojo/apps/aws/services/aws_check.py's "
                 "OWNERSHIP_TAGS byte for byte, or the two layers disagree "
                 "about what belongs to django-mojo")


@th.django_unit_test("owns() is tag-based and never adopts by name alone")
def test_owns_requires_matching_tags(opts):
    from mojo.deploy.provision import spec as spec_module

    spec = _spec()
    mine = spec_module.tag_list(spec, "node", name="wmx1")
    th.assert_true(spec_module.owns(mine, spec),
                   "a resource carrying our project and env tags is ours")
    th.assert_true(spec_module.owns(mine, spec, role="node"),
                   "the role filter must match when it is asked for")
    th.assert_eq(spec_module.owns(mine, spec, role="balancer"), False,
                 "a node's tags must not satisfy a balancer role filter")
    th.assert_eq(spec_module.owns([{"Key": "Name", "Value": "wmx1"}], spec),
                 False,
                 "a resource named like ours but carrying none of our tags "
                 "must never be adopted")
    th.assert_eq(spec_module.owns([], spec), False,
                 "an untagged resource is never ours")


@th.django_unit_test("a valid topology reports no naming problems")
def test_validate_names_accepts_a_valid_spec(opts):
    from mojo.deploy.provision import spec as spec_module

    th.assert_eq(spec_module.validate_names(_spec()), [],
                 "wmx-prod/small must be nameable")


@th.django_unit_test("the 32-character load balancer name cap is enforced before AWS is called")
def test_validate_names_enforces_the_elb_cap(opts):
    from mojo.deploy.provision import spec as spec_module

    spec = spec_module.build("verylongprojectname", "staging", REGION)
    names = spec_module.names(spec)
    th.assert_true(len(names["certbot_target_group"]) > spec_module.ELB_NAME_MAX,
                   "this fixture is meant to exceed the cap")
    problems = spec_module.validate_names(spec)
    th.assert_true(any("certbot_target_group" in p for p in problems),
                   f"a target group name over {spec_module.ELB_NAME_MAX} "
                   f"characters must be reported, or CreateTargetGroup fails "
                   f"after the VPC and the database already exist: {problems}")
    th.assert_true(any(str(spec_module.ELB_NAME_MAX) in p for p in problems),
                   f"the problem must name the actual cap: {problems}")


@th.django_unit_test("the RDS DBName rules are enforced")
def test_validate_names_enforces_dbname_rules(opts):
    from mojo.deploy.provision import spec as spec_module

    names = spec_module.names(_spec())
    th.assert_eq(names["db_name"], "wmxprod",
                 "DBName is alphanumeric only — RDS rejects hyphens outright")
    th.assert_true(spec_module.DBNAME_RE.match(names["db_name"]),
                   "the derived DBName must satisfy the rule the validator "
                   "checks")
    th.assert_eq(bool(spec_module.DBNAME_RE.match("wmx-prod")), False,
                 "a hyphen must not be accepted in a DBName")
    th.assert_eq(bool(spec_module.DBNAME_RE.match("1wmx")), False,
                 "a DBName must start with a letter")


@th.django_unit_test("the RDS cluster identifier rules are enforced")
def test_validate_names_enforces_cluster_identifier_rules(opts):
    from mojo.deploy.provision import spec as spec_module

    th.assert_true(spec_module.IDENTIFIER_RE.match(
        spec_module.names(_spec())["db_cluster"]),
        "the derived cluster identifier must be legal")
    for bad in ("wmx_prod-aurora", "wmx--prod-aurora", "wmx-prod-aurora-",
                "1wmx-aurora", "WMX-aurora"):
        th.assert_eq(bool(spec_module.IDENTIFIER_RE.match(bad)), False,
                     f"{bad!r} must be rejected: RDS allows no underscores, no "
                     f"doubled or trailing hyphen, no uppercase, and requires "
                     f"a leading letter")

    problems = spec_module.validate_names(
        spec_module.build("a" * 60, "prod", REGION))
    th.assert_true(problems,
                   "a project slug this long produces names AWS rejects and "
                   "must not reach an API call")


@th.django_unit_test("the slug rules are enforced")
def test_validate_names_enforces_slug_rules(opts):
    from mojo.deploy.provision import spec as spec_module

    for bad in ("Prod", "pro_d", "-prod", "prod-", "pr--od", "1prod", ""):
        problems = spec_module.validate_names(
            spec_module.build(PROJECT, bad, REGION))
        th.assert_true(problems,
                       f"env slug {bad!r} must be rejected — it becomes a DNS "
                       f"label, a tag value and a resource-name prefix")

    problems = spec_module.validate_names(
        spec_module.build(PROJECT, ENV, REGION, admin_cidrs=["10.0.0.1"]))
    th.assert_true(any("CIDR" in p for p in problems),
                   f"an admin CIDR with no prefix length must be reported: "
                   f"{problems}")


@th.django_unit_test("the cost estimate lists the NLB only when one will be built")
def test_cost_table_nlb_line(opts):
    from mojo.deploy.provision import spec as spec_module

    with_nlb = spec_module.estimate_cost(_spec(preset="small"))
    items = [row["item"] for row in with_nlb["rows"]]
    th.assert_in("load balancer", items,
                 f"the small preset builds an NLB, so the estimate must "
                 f"include it: {items}")
    th.assert_in("balancer addresses", items,
                 f"the balancer's elastic IPs cost money too: {items}")

    without = spec_module.estimate_cost(_spec(preset="micro"))
    micro_items = [row["item"] for row in without["rows"]]
    th.assert_eq("load balancer" in micro_items, False,
                 f"micro builds no NLB, so budgeting for one is wrong: "
                 f"{micro_items}")
    th.assert_in("node addresses", micro_items,
                 f"without a balancer the nodes hold the addresses: "
                 f"{micro_items}")

    th.assert_true(with_nlb["total"] > 0, "a total of zero is not an estimate")
    th.assert_eq(
        with_nlb["total"],
        round(sum(row["monthly"] for row in with_nlb["rows"]), 2),
        "the total must be the sum of the rows shown, or the estimate is "
        "quietly hiding a line")


@th.django_unit_test("an unknown spec field raises instead of being ignored")
def test_unknown_spec_field_raises(opts):
    from mojo.deploy.provision import spec as spec_module

    raised = False
    try:
        spec_module.build(PROJECT, ENV, REGION, admin_cidr=["10.0.0.0/8"])
    except AttributeError:
        raised = True
    th.assert_true(raised,
                   "a misspelled override must raise — silently ignoring it "
                   "would leave SSH closed and look like it worked")


@th.django_unit_test("engine versions are pinned, not latest")
def test_engine_versions_are_pinned(opts):
    from mojo.deploy.provision import spec as spec_module

    th.assert_in(spec_module.DB_ENGINE, spec_module.ENGINE_VERSIONS,
                 "the database engine must have a pinned version")
    th.assert_in(spec_module.CACHE_ENGINE, spec_module.ENGINE_VERSIONS,
                 "the cache engine must have a pinned version")
    for engine, version in spec_module.ENGINE_VERSIONS.items():
        th.assert_true(version and version[0].isdigit(),
                       f"{engine} must pin a concrete version, got {version!r}")


@th.django_unit_test("stable_node_ips adds the node-address cost line even with an NLB")
def test_cost_table_stable_node_ips(opts):
    from mojo.deploy.provision import inputs
    from mojo.deploy.provision import spec as spec_module

    flagged = spec_module.estimate_cost(
        _spec(preset="small", stable_node_ips=True))
    items = [row["item"] for row in flagged["rows"]]
    th.assert_in("balancer addresses", items,
                 f"the NLB's own addresses must stay in the estimate: {items}")
    th.assert_in("node addresses", items,
                 f"stable_node_ips buys per-node addresses, so the estimate "
                 f"must show them: {items}")

    plain = spec_module.estimate_cost(_spec(preset="small"))
    plain_items = [row["item"] for row in plain["rows"]]
    th.assert_eq("node addresses" in plain_items, False,
                 f"without the flag a balancer fleet budgets no node "
                 f"addresses: {plain_items}")

    # The answers key and the CLI kwarg both reach the spec.
    answers = {"project": "mojo", "env": "prod", "region": "us-east-1",
               "preset": "small", "stable_node_ips": True}
    th.assert_true(inputs.to_spec(answers).stable_node_ips,
                   "the stable_node_ips answers key never reached the spec")
    th.assert_true(
        inputs.to_spec({"project": "mojo", "env": "prod",
                        "region": "us-east-1", "preset": "small"},
                       stable_node_ips=True).stable_node_ips,
        "the --stable-node-ips CLI kwarg never reached the spec")
    th.assert_eq(
        inputs.to_spec({"project": "mojo", "env": "prod",
                        "region": "us-east-1", "preset": "small"}).stable_node_ips,
        False, "stable_node_ips must default off")
