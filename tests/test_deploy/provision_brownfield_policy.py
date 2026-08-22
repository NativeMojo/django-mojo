from testit import helpers as th


class _Raw:
    def describe_vpcs(self):
        return {"Vpcs": []}

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


@th.django_unit_test()
def test_positive_client_policy_blocks_every_unlisted_mutation_via_getattr(opts):
    from mojo.deploy.provision import brownfield_policy, discover

    client = discover.GuardedClient(
        _Raw(), "ec2", brownfield_policy.MutationPolicy())
    th.assert_eq(client.describe_vpcs(), {"Vpcs": []},
                 "explicit read operations must remain reachable")
    th.assert_eq(client.run_instances(), {"Instances": []},
                 "declared preparation mutations must remain reachable")
    for method in ("associate_address", "set_subnets", "create_db_cluster"):
        raised = None
        try:
            getattr(client, method)
        except brownfield_policy.BrownfieldCallBlocked as err:
            raised = err
        th.assert_true(raised is not None,
                       f"dynamic getattr must not bypass the block on {method}")


@th.django_unit_test()
def test_closed_policy_excludes_data_dns_certificates_handoff_and_teardown(opts):
    from mojo.deploy.provision import brownfield_policy

    forbidden = {
        "ec2": ("associate_address", "disassociate_address", "create_tags",
                "terminate_instances", "modify_instance_attribute"),
        "elbv2": ("set_subnets", "delete_load_balancer", "deregister_targets"),
        "route53": ("change_resource_record_sets",),
        "acm": ("request_certificate", "import_certificate"),
        "rds": ("create_db_cluster", "modify_db_cluster"),
        "elasticache": ("create_replication_group",),
        "s3": ("put_object", "put_bucket_policy", "put_bucket_tagging"),
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
