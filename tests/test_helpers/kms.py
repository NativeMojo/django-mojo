from testit import helpers as th


@th.django_unit_test()
def test_kms_client_kwargs_carries_configured_credentials(opts):
    """The regression: KMS built its boto3 client with region only, so a box
    holding perfectly good AWS_KEY/AWS_SECRET in var/django.conf still failed
    with NoCredentialsError — boto3 cannot read Django settings."""
    from mojo.helpers.aws import kms
    from mojo.helpers.settings import settings

    kwargs = kms.client_kwargs("us-west-2")

    assert kwargs["region_name"] == "us-west-2", \
        f"region_name should pass through unchanged, got {kwargs.get('region_name')}"

    configured_key = settings.get_static("AWS_KEY", None)
    if not configured_key:
        # No keys on this box — the fallback path is the one under test.
        assert kwargs["aws_access_key_id"] is None, \
            "with no configured AWS_KEY the kwarg must be None so boto3 falls " \
            f"back to its own chain, got {kwargs['aws_access_key_id']!r}"
        return

    assert kwargs["aws_access_key_id"] == configured_key, \
        "the configured AWS_KEY must reach the KMS client — this is exactly " \
        "what was missing when MojoSecrets raised NoCredentialsError"
    assert kwargs["aws_secret_access_key"] == settings.get_static("AWS_SECRET", None), \
        "the configured AWS_SECRET must reach the KMS client alongside the key"


@th.django_unit_test()
def test_kms_client_kwargs_reads_static_settings_only(opts):
    """Credentials for the encryption client must never resolve through the
    DB-backed settings store: MojoSecrets needs this client to decrypt, so a
    DB lookup here can recurse into a secret row it cannot read yet."""
    from mojo.helpers.aws import kms
    from mojo.helpers.settings import settings

    seen = []
    original_get = settings.get

    def spy(name, *args, **kwargs):
        seen.append(name)
        return original_get(name, *args, **kwargs)

    settings.get = spy
    try:
        kms.client_kwargs("us-west-2")
    finally:
        settings.get = original_get

    leaked = [n for n in seen if n in ("AWS_KEY", "AWS_SECRET")]
    assert not leaked, \
        f"credentials must be read with get_static, but settings.get was used for {leaked}"


@th.django_unit_test()
def test_kms_helper_uses_client_kwargs(opts):
    """The helper must actually build its client through client_kwargs —
    a correct helper function nothing calls would not have fixed anything."""
    from mojo.helpers.aws import kms

    captured = {}
    original = kms.boto3.client

    def spy(service, **kwargs):
        captured["service"] = service
        captured["kwargs"] = kwargs
        return original(service, **kwargs)

    kms.boto3.client = spy
    try:
        # ensure_key=False keeps this off the network — no AWS call is made.
        kms.KMSHelper(kms_key_id="alias/mojo-unit-test",
                      region_name="us-west-2",
                      ensure_key=False)
    finally:
        kms.boto3.client = original

    assert captured.get("service") == "kms", \
        f"expected a kms client to be built, got {captured.get('service')!r}"
    assert "aws_access_key_id" in captured["kwargs"], \
        "KMSHelper built its client without ever passing credentials — the " \
        f"exact bug this fixes. kwargs were {sorted(captured['kwargs'])}"
    assert captured["kwargs"]["region_name"] == "us-west-2", \
        f"region must reach the client, got {captured['kwargs'].get('region_name')}"
