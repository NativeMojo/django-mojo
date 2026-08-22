from testit import helpers as th

from .brownfield_fixture import managed_topology


class _Clients:
    def __init__(self, client):
        self.client = client

    def get(self, name):
        return self.client


class _NoCalls:
    def __getattr__(self, name):
        raise AssertionError(f"IAM collision/idempotency must not call {name}")


@th.django_unit_test()
def test_managed_iam_name_collision_performs_no_mutation(opts):
    from mojo.deploy.provision import brownfield_identity, report

    observed = {"brownfield_profiles": {"api": {
        "role_collision": True, "profile_collision": False,
        "role_arn": None, "profile_arn": None}}}
    findings, actions, result = brownfield_identity.ensure_identity(
        _Clients(_NoCalls()), managed_topology(), observed, apply=True)
    th.assert_eq(actions, [],
                 "an unowned collision must not advertise or attempt a mutation")
    th.assert_true(any(row.status == report.BLIND for row in findings),
                   f"the collision must block downstream nodes: {findings}")
    th.assert_eq(result.as_dict()["brownfield_profiles"], {},
                 "no colliding profile may be exposed downstream")


@th.django_unit_test()
def test_failed_role_create_never_falls_through_to_policy_or_profile(opts):
    from botocore.exceptions import ClientError
    from mojo.deploy.provision import brownfield_identity

    class _IAM:
        def __init__(self):
            self.calls = []

        def create_role(self, **kwargs):
            self.calls.append("create_role")
            raise ClientError({"Error": {"Code": "EntityAlreadyExists",
                                         "Message": "collision"}}, "CreateRole")

        def __getattr__(self, name):
            def call(**kwargs):
                self.calls.append(name)
                return {}
            return call

    iam = _IAM()
    brownfield_identity.ensure_identity(
        _Clients(iam), managed_topology(), {"brownfield_profiles": {"api": {}}},
        apply=True)
    th.assert_eq(iam.calls, ["create_role"],
                 "a failed create must not mutate the colliding role/profile")


@th.django_unit_test()
def test_owned_managed_identity_is_idempotent(opts):
    from mojo.deploy.provision import brownfield_identity

    spec = managed_topology()
    policy = brownfield_identity.policy_document(spec)
    observed = {"brownfield_profiles": {"api": {
        "role_name": "maestro-shadow-api",
        "role_arn": "arn:aws:iam::123456789012:role/maestro-shadow-api",
        "profile_name": "maestro-shadow-api",
        "profile_arn": ("arn:aws:iam::123456789012:instance-profile/"
                        "maestro-shadow-api"),
        "policy_document": policy, "ssm_core_attached": True,
        "role_collision": False, "profile_collision": False,
    }}}
    findings, actions, result = brownfield_identity.ensure_identity(
        _Clients(_NoCalls()), spec, observed, apply=True)
    th.assert_eq(actions, [],
                 f"an owned converged role/profile must produce no actions: {actions}")
    th.assert_true(result.as_dict()["brownfield_profiles"]["api"]["profile_arn"],
                   "the verified profile must remain available to node launch")


@th.django_unit_test()
def test_runtime_policy_authorizes_only_exact_versioned_artifacts(opts):
    from mojo.deploy.provision import brownfield_identity

    policy = brownfield_identity.policy_document(managed_topology())
    statements = {row["Sid"]: row for row in policy["Statement"]}
    pinned = statements["ReadPinnedFleetArtifacts"]
    th.assert_eq(pinned["Action"], ["s3:GetObjectVersion"],
                 f"version-pinned downloads need GetObjectVersion: {pinned}")
    expected = {
        "arn:aws:s3:::maestro-prod-config/bootstrap/stage1.sh",
        "arn:aws:s3:::maestro-prod-config/config/live/django.conf",
        "arn:aws:s3:::maestro-prod-config/bootstrap/node-role.json",
        "arn:aws:s3:::maestro-prod-config/secrets/db.json",
    }
    th.assert_eq(set(pinned["Resource"]), expected,
                 f"only exact bootstrap/credential keys may be version-read: {pinned}")
    prefixes = statements["ReadDeclaredFleetPrefixes"]
    th.assert_eq(prefixes["Action"], ["s3:GetObject"],
                 f"unversioned GetObject must stay in its own prefix grant: {prefixes}")
    th.assert_eq(any(value.endswith("bootstrap/*")
                     for value in prefixes["Resource"]), False,
                 f"bootstrap must not receive a broad unversioned grant: {prefixes}")


@th.django_unit_test()
def test_identity_logical_actions_cover_profile_attachment_calls(opts):
    from mojo.deploy.provision import brownfield_identity, brownfield_plan

    spec = managed_topology()

    class _IAM:
        def __init__(self):
            self.calls = []

        def create_role(self, **kwargs):
            self.calls.append("create_role")
            return {"Role": {"Arn": "arn:aws:iam::123456789012:role/"
                                    "maestro-shadow-api"}}

        def create_instance_profile(self, **kwargs):
            self.calls.append("create_instance_profile")
            return {"InstanceProfile": {"Arn":
                    "arn:aws:iam::123456789012:instance-profile/"
                    "maestro-shadow-api"}}

        def __getattr__(self, name):
            def call(**kwargs):
                self.calls.append(name)
                return {}
            return call

    iam = _IAM()
    _findings, actions, _result = brownfield_identity.ensure_identity(
        _Clients(iam), spec, {"brownfield_profiles": {"api": {}}},
        apply=True)
    th.assert_in("add_role_to_instance_profile", iam.calls,
                 f"profile creation must include its subordinate attachment: {iam.calls}")
    allowed = brownfield_plan._allowed_action_targets(spec)
    for action in actions:
        th.assert_in(action.target,
                     allowed.get((action.step, action.verb), set()),
                     f"every logical IAM action must remain exactly gated: {action}")
