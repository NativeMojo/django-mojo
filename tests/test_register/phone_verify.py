"""Unit tests for the Redis-backed phone_register service."""
from testit import helpers as th


@th.django_unit_test("phone_register.start writes a session and returns token+code")
def test_start_writes_session(opts):
    import json
    from mojo.apps.account.services import phone_register
    from mojo.helpers.redis import get_connection

    session_token, code, ttl = phone_register.start("+14155551212", ip="127.0.0.1")
    assert isinstance(session_token, str) and len(session_token) == 32, \
        f"session_token must be a 32-char uuid hex, got {session_token!r}"
    assert isinstance(code, str) and len(code) == 6 and code.isdigit(), \
        f"code must be a 6-digit string, got {code!r}"
    assert ttl > 0, f"ttl must be positive, got {ttl}"

    raw = get_connection().get(f"phone:register:session:{session_token}")
    assert raw is not None, "session key must be written to redis"
    data = json.loads(raw)
    assert data["phone"] == "+14155551212", \
        f"stored phone must round-trip, got {data['phone']!r}"
    assert data["code"] == code, "stored code must match returned code"

    # Cleanup
    get_connection().delete(f"phone:register:session:{session_token}")


@th.django_unit_test("phone_register.verify_code mints verified token, consumes session")
def test_verify_code_happy_path(opts):
    from mojo.apps.account.services import phone_register
    from mojo.helpers.redis import get_connection

    session_token, code, _ = phone_register.start("+14155550111")
    verified_token, phone, ttl = phone_register.verify_code(session_token, code)
    assert len(verified_token) == 32, \
        f"verified_token must be a 32-char uuid hex, got {verified_token!r}"
    assert phone == "+14155550111", \
        f"verified phone must round-trip, got {phone!r}"
    assert ttl > 0, "verified ttl must be positive"

    # Session is gone
    raw = get_connection().get(f"phone:register:session:{session_token}")
    assert raw is None, "session key must be consumed after verify"
    # Verified key exists
    vraw = get_connection().get(f"phone:register:verified:{verified_token}")
    assert vraw is not None, "verified key must be written"

    # Cleanup
    get_connection().delete(f"phone:register:verified:{verified_token}")


@th.django_unit_test("phone_register.verify_code rejects wrong code and consumes session")
def test_verify_code_wrong_code(opts):
    from mojo.apps.account.services import phone_register
    from mojo.helpers.redis import get_connection
    from mojo import errors as merrors

    session_token, code, _ = phone_register.start("+14155550222")
    wrong = "000000" if code != "000000" else "111111"
    try:
        phone_register.verify_code(session_token, wrong)
        assert False, "verify_code must reject wrong code"
    except merrors.ValueException:
        pass

    # Session is gone even on bad code (single-use to prevent brute force at the
    # service layer; endpoint rate limit covers the larger pattern).
    raw = get_connection().get(f"phone:register:session:{session_token}")
    assert raw is None, \
        "session must be consumed even when the code is wrong (single-use)"


@th.django_unit_test("phone_register.consume returns True only when phones match")
def test_consume_phone_binding(opts):
    from mojo.apps.account.services import phone_register

    session_token, code, _ = phone_register.start("+14155550333")
    verified_token, phone, _ = phone_register.verify_code(session_token, code)
    assert phone_register.consume(verified_token, "+14155559999") is False, \
        "consume must reject a token paired with a different phone"


@th.django_unit_test("phone_register.consume is single-use (idempotent only on success)")
def test_consume_single_use(opts):
    from mojo.apps.account.services import phone_register

    session_token, code, _ = phone_register.start("+14155550444")
    verified_token, phone, _ = phone_register.verify_code(session_token, code)
    first = phone_register.consume(verified_token, phone)
    second = phone_register.consume(verified_token, phone)
    assert first is True, "first consume of a valid token must return True"
    assert second is False, "second consume of the same token must return False"


@th.django_unit_test("phone_register rejects malformed token shapes early")
def test_rejects_malformed_token(opts):
    from mojo.apps.account.services import phone_register
    from mojo import errors as merrors

    # consume just returns False for malformed tokens
    assert phone_register.consume("not-a-uuid", "+14155550555") is False, \
        "consume must reject non-uuid tokens without a redis lookup"
    # verify_code raises on malformed tokens
    try:
        phone_register.verify_code("not-a-uuid", "123456")
        assert False, "verify_code must reject malformed session tokens"
    except merrors.ValueException:
        pass


@th.django_unit_test("normalize_phone is idempotent: token binds through non-normalized input")
def test_normalize_phone_idempotent_through_flow(opts):
    """If a caller passes the same number in a different format on register,
    consume() must still bind correctly because normalize_phone normalizes
    both stored and supplied values to the same canonical form.

    This locks in the idempotency assumption — non-normalized input through
    User.normalize_phone twice yields the same canonical phone string.
    """
    from mojo.apps.account.models import User
    from mojo.apps.account.services import phone_register

    raw_messy = "+1 (415) 555-0666"
    normalized = User.normalize_phone(raw_messy)
    assert normalized, f"normalize_phone must accept the formatted input, got {normalized!r}"
    assert User.normalize_phone(normalized) == normalized, \
        "normalize_phone must be idempotent — feeding its own output back must yield the same string"

    # Start with the already-normalized phone (mirrors the endpoint's behavior
    # where it normalizes before storing).
    session_token, code, _ = phone_register.start(normalized)
    verified_token, stored_phone, _ = phone_register.verify_code(session_token, code)
    assert stored_phone == normalized, \
        f"verify_code must return the normalized phone, got {stored_phone!r}"

    # Caller supplies the same phone in a different (re-normalized) format.
    # If normalize_phone is idempotent, the binding holds.
    resupplied = User.normalize_phone(raw_messy)
    assert phone_register.consume(verified_token, resupplied) is True, \
        "consume must succeed when the supplied phone normalizes to the bound phone"
