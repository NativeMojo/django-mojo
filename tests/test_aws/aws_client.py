from unittest import mock

from testit import helpers as th


@th.django_unit_test()
def test_aws_session_uses_default_chain_and_rejects_partial_settings(opts):
    from botocore.exceptions import PartialCredentialsError
    from mojo.helpers.aws import client

    with mock.patch.object(client.boto3, "Session") as session_cls:
        client.get_session(region="us-west-2")
        session_cls.assert_called_once_with(region_name="us-west-2")

    try:
        client.get_session(access_key="key-only", region="us-west-2")
    except PartialCredentialsError:
        pass
    else:
        assert False, "A half-configured static credential pair must fail before AWS access"


@th.django_unit_test()
def test_aws_client_applies_bounded_config(opts):
    from mojo.helpers.aws.client import get_client

    session = mock.Mock()
    get_client("sts", session=session, region="us-east-2", timeout=7, max_attempts=2)
    kwargs = session.client.call_args.kwargs
    config = kwargs["config"]
    assert kwargs["region_name"] == "us-east-2", "The selected region must reach the client"
    assert config.connect_timeout == 7, "The connect timeout must be bounded by the caller"
    assert config.read_timeout == 7, "The read timeout must be bounded by the caller"
    assert config.retries["max_attempts"] == 2, "The retry budget must be bounded"


@th.django_unit_test()
def test_cloudwatch_helper_uses_injected_client_factory(opts):
    from mojo.helpers.aws.cloudwatch import CloudWatchHelper

    clients = {name: mock.Mock(name=name) for name in ("cloudwatch", "ec2", "rds", "elasticache")}
    factory = mock.Mock(side_effect=lambda service, **kwargs: clients[service])
    helper = CloudWatchHelper(
        access_key="key", secret_key="secret", region="us-east-1",
        session=mock.Mock(), client_factory=factory, timeout=4,
    )
    assert helper.cw is clients["cloudwatch"], "CloudWatch must use the injected factory"
    assert helper.ec2 is clients["ec2"], "EC2 must use the injected factory"
    assert factory.call_count == 2, "Each lazy client should be constructed once"
