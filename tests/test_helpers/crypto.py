from testit import helpers as th
from testit import faker
import datetime

@th.django_unit_test()
def test_crypto_encrypt_decrypt_text(opts):
    from mojo.helpers import crypto
    raw_text = "https://www.google.com/search?q=secrets"
    pword = "MYPASSWORD"
    enc_text = crypto.encrypt(raw_text, pword)
    dec_text = crypto.decrypt(enc_text, pword)
    assert dec_text == raw_text, f"Expected {raw_text}, got {dec_text}"


@th.django_unit_test()
def test_crypto_encrypt_decrypt_dict(opts):
    from mojo.helpers import crypto
    raw_dict = dict(name="John", age=30, email="john@example.com")
    pword = "213121311221321"
    enc_dict = crypto.encrypt(raw_dict, pword)
    dec_dict = crypto.decrypt(enc_dict, pword)
    assert dec_dict == raw_dict, f"Expected {raw_dict}, got {dec_dict}"

@th.django_unit_test()
def test_crypto_hashing(opts):
    from mojo.helpers import crypto

    rval = "bob jones was here"
    hval1 = crypto.hash(rval)
    hval2 = crypto.hash(rval)
    assert hval1 == hval2, f"Expected {hval1} == {hval2}"
    hval2 = crypto.hash("random value")
    assert hval1 != hval2, f"Expected {hval1} != {hval2}"

@th.django_unit_test()
def test_crypto_hashing_with_salt(opts):
    from mojo.helpers import crypto

    rval = "bob jones was here"
    hval1 = crypto.hash(rval, salt="salt")
    hval2 = crypto.hash(rval, salt="salt")
    assert hval1 == hval2, f"Expected {hval1} == {hval2}"
    hval2 = crypto.hash(rval)
    assert hval1 != hval2, f"Expected {hval1} != {hval2}"

    rval = "bob jones was here"
    hval1 = crypto.hash(rval, salt="salt1")
    hval2 = crypto.hash(rval, salt="salt2")
    assert hval1 != hval2, f"Expected {hval1} != {hval2}"


@th.django_unit_test()
def test_crypto_hashing_dicts(opts):
    from mojo.helpers import crypto

    rval = dict(name="John", age=30, email="john@example.com")
    hval1 = crypto.hash(rval)
    hval2 = crypto.hash(rval)
    assert hval1 == hval2, f"Expected {hval1} == {hval2}"

    rval2 = dict(name="John", age=30, email="john@example.com", gender="male")
    hval2 = crypto.hash(rval2)
    assert hval1 != hval2, f"Expected {hval1} != {hval2}"


@th.django_unit_test()
def test_crypto_signing(opts):
    from mojo.helpers import crypto

    rval = dict(name="John", age=30, email="john@example.com")
    sig = crypto.sign(rval)
    assert crypto.verify(rval, sig), f"Expected signature to verify"
    assert not crypto.verify(dict(name="John", age=30), sig), f"Expected signature to not verify"
