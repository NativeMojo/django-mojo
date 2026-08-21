"""KMS credential-resolution test moved out of tests/test_helpers/kms.py.

It replaces `settings.get` on the shared SettingsHelper singleton with a spy —
a process-global mutation every parallel module reads through — so it runs only
in the opt-in serial tier (maestro item #1839). `client_kwargs(region_name)`
offers no injectable settings seam to convert through.
"""
from testit import helpers as th


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
