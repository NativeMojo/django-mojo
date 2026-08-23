from testit import helpers as th


class _Raw:
    def describe_vpcs(self):
        return {"Vpcs": []}

    def describe_instance_attribute(self, **kwargs):
        return {"UserData": {"Value": ""}}

    def describe_cache_clusters(self, **kwargs):
        return {"CacheClusters": []}

    def run_instances(self):
        return {"Instances": []}

    def associate_address(self):
        return {}

    def set_subnets(self):
        return {}

    def create_db_cluster(self):
        return {}

    def get_object(self):
        return {"Body": b"secret"}

    def get_secret_value(self):
        return {"SecretString": "secret"}

    def disassociate_address(self):
        return {}

    def release_address(self):
        return {}


@th.django_unit_test()
def test_positive_client_policy_blocks_every_unlisted_mutation_via_getattr(opts):
    from mojo.deploy.provision import brownfield_policy, discover

    client = discover.GuardedClient(
        _Raw(), "ec2", brownfield_policy.MutationPolicy())
    th.assert_eq(client.describe_vpcs(), {"Vpcs": []},
                 "explicit read operations must remain reachable")
    th.assert_eq(client.run_instances(), {"Instances": []},
                 "declared preparation mutations must remain reachable")
    th.assert_eq(client.describe_instance_attribute(
        InstanceId="i-1", Attribute="userData"),
        {"UserData": {"Value": ""}},
        "exact launch user-data evidence must remain readable")
    cache_client = discover.GuardedClient(
        _Raw(), "elasticache", brownfield_policy.MutationPolicy())
    th.assert_eq(cache_client.describe_cache_clusters(
        CacheClusterId="cache-member"), {"CacheClusters": []},
        "member-cluster metadata must remain reachable for network proof")
    for method in ("associate_address", "create_db_cluster"):
        raised = None
        try:
            getattr(client, method)
        except brownfield_policy.BrownfieldCallBlocked as err:
            raised = err
        th.assert_true(raised is not None,
                       f"dynamic getattr must not bypass the block on {method}")
    # FORBIDDEN_METHODS keys on (service, method) and holds
    # ("elbv2", "set_subnets"), so the live-ingress cutover seam only fires on
    # an elbv2 client. On the ec2 client above the same name is simply an
    # unlisted mutation, which the positive policy blocks one layer later —
    # both are refusals, but only the elbv2 one exercises the stronger seam
    # this assertion is about. No client in the package is exempt from it:
    # moving a live public address is external operator work.
    elbv2 = discover.GuardedClient(
        _Raw(), "elbv2", brownfield_policy.MutationPolicy())
    raised = None
    try:
        elbv2.set_subnets
    except discover.DestructiveCallBlocked as err:
        raised = err
    th.assert_true(raised is not None,
                   "SetSubnets must stop at the stronger global cutover seam")

    raised = None
    try:
        client.set_subnets
    except brownfield_policy.BrownfieldCallBlocked as err:
        raised = err
    th.assert_true(raised is not None,
                   "an elbv2-only verb is still refused on an ec2 client, as "
                   "an unlisted preparation mutation")


@th.django_unit_test()
def test_closed_policy_excludes_data_dns_certificates_cutover_and_teardown(opts):
    from mojo.deploy.provision import brownfield_policy

    forbidden = {
        "ec2": ("associate_address", "disassociate_address", "create_tags",
                "terminate_instances", "modify_instance_attribute"),
        "elbv2": ("set_subnets", "delete_load_balancer", "deregister_targets"),
        "route53": ("change_resource_record_sets",),
        "acm": ("request_certificate", "import_certificate"),
        "rds": ("create_db_cluster", "modify_db_cluster"),
        "elasticache": ("create_replication_group",),
        "s3": ("list_objects_v2", "put_object", "put_bucket_policy",
               "put_bucket_tagging"),
        "kms": ("create_key", "put_key_policy"),
    }
    policy = brownfield_policy.MutationPolicy()
    for service, methods in forbidden.items():
        for method in methods:
            raised = None
            try:
                policy.authorize(service, method)
            except brownfield_policy.BrownfieldCallBlocked as err:
                raised = err
            th.assert_true(raised is not None,
                           f"{service}.{method} must be unreachable")


@th.django_unit_test()
def test_target_group_attribute_boundary_is_narrow_and_explicit(opts):
    from mojo.deploy.provision import brownfield_policy

    policy = brownfield_policy.MutationPolicy()
    for method in ("describe_target_group_attributes",
                   "modify_target_group_attributes"):
        raised = None
        try:
            policy.authorize("elbv2", method)
        except Exception as err:
            raised = err
        th.assert_eq(raised, None,
                     f"the exact ELBv2 attribute operation must be allowed: {raised}")
    for method in ("delete_target_group", "modify_listener_attributes"):
        raised = None
        try:
            policy.authorize("elbv2", method)
        except brownfield_policy.BrownfieldCallBlocked as err:
            raised = err
        th.assert_true(raised is not None,
                       f"adjacent ELBv2 mutation {method} must remain blocked")


@th.django_unit_test()
def test_metadata_boundary_blocks_object_and_secret_bodies_via_getattr(opts):
    from mojo.deploy.provision import brownfield_policy, discover

    for service, method in (("s3", "get_object"),
                            ("secretsmanager", "get_secret_value")):
        guarded = discover.GuardedClient(
            _Raw(), service, brownfield_policy.MutationPolicy())
        raised = None
        try:
            getattr(guarded, method)
        except brownfield_policy.BrownfieldCallBlocked as err:
            raised = err
        th.assert_true(raised is not None,
                       f"{service}.{method} must never expose value bytes")


@th.django_unit_test()
def test_ordinary_clients_cannot_cross_the_preserved_address_boundary(opts):
    from mojo.deploy.provision import discover

    for service, method in (("ec2", "disassociate_address"),
                            ("ec2", "release_address"),
                            ("elbv2", "set_subnets")):
        guarded = discover.GuardedClient(_Raw(), service)
        raised = None
        try:
            getattr(guarded, method)
        except discover.DestructiveCallBlocked as err:
            raised = err
        th.assert_true(raised is not None,
                       f"ordinary {service}.{method} must fail at the client seam")

    # Managed stable-node EIPs still use this non-destructive association path.
    managed = discover.GuardedClient(_Raw(), "ec2")
    th.assert_eq(managed.associate_address(), {},
                 "the global guard must not break managed stable-node EIPs")
