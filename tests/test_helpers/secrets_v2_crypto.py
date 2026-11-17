from testit import helpers as th

KEY = "k" * 40
OTHER = "o" * 40


@th.django_unit_test()
def test_v2_round_trip_and_prefix(opts):
    from mojo.helpers.crypto import aes
    blob = aes.encrypt_v2({"token": "abc"}, KEY, "account.user.1.mojo_secrets")
    assert blob.startswith("v2:"), f"v2 blob must start with v2:, got {blob[:8]!r}"
    assert aes.is_v2(blob), "is_v2 must recognise its own blob"
    data, index = aes.decrypt_v2(blob, [KEY], "account.user.1.mojo_secrets")
    assert data == {"token": "abc"}, f"round trip lost data: {data!r}"
    assert index == 0, f"primary key should report index 0, got {index}"


@th.django_unit_test()
def test_v2_wrong_context_refused(opts):
    from mojo.helpers.crypto import aes
    blob = aes.encrypt_v2({"token": "abc"}, KEY, "account.user.1.mojo_secrets")
    for context in ("account.user.2.mojo_secrets", "account.group.1.mojo_secrets"):
        try:
            aes.decrypt_v2(blob, [KEY], context)
        except ValueError:
            continue
        raise AssertionError(f"blob bound to user 1 must not open under {context}")


@th.django_unit_test()
def test_v2_fallback_key_reports_index(opts):
    from mojo.helpers.crypto import aes
    blob = aes.encrypt_v2({"a": 1}, OTHER, "ctx")
    data, index = aes.decrypt_v2(blob, ["", KEY, OTHER], "ctx")
    assert data == {"a": 1}, f"fallback decrypt lost data: {data!r}"
    assert index == 2, f"fallback key should report its list index 2, got {index}"


@th.django_unit_test()
def test_v2_unknown_key_refused(opts):
    from mojo.helpers.crypto import aes
    blob = aes.encrypt_v2({"a": 1}, OTHER, "ctx")
    for keys in ([KEY], [], [""]):
        try:
            aes.decrypt_v2(blob, keys, "ctx")
        except ValueError:
            continue
        raise AssertionError(f"keys {len(keys)} without the right key must not decrypt")


@th.django_unit_test()
def test_v2_encrypt_requires_key(opts):
    from mojo.helpers.crypto import aes
    try:
        aes.encrypt_v2({"a": 1}, "", "ctx")
    except ValueError:
        return
    raise AssertionError("encrypt_v2 with an empty key must raise")


@th.django_unit_test()
def test_legacy_blob_is_never_v2(opts):
    from mojo.helpers.crypto import aes
    for _ in range(20):
        legacy = aes.encrypt({"a": 1}, "row-password")
        assert not aes.is_v2(legacy), f"a legacy blob must never look like v2: {legacy[:8]!r}"
    try:
        aes.decrypt_v2(legacy, [KEY], "ctx")
    except ValueError:
        return
    raise AssertionError("decrypt_v2 must refuse a legacy blob")
