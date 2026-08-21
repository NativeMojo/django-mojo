"""Request lifecycle coverage for ``ReaderPinMiddleware``."""

from testit import helpers as th


class _Request:
    def __init__(self, method):
        self.method = method


class _Meta:
    app_label = "reader_test"


class _Model:
    _meta = _Meta()


@th.unit_test("reader middleware: scope exists only while handling a request")
def test_scope_is_bounded_to_request(opts):
    from mojo.db import pinning
    from mojo.middleware.db_reader import ReaderPinMiddleware

    observed = []

    def response(request):
        observed.append((pinning.is_active(), pinning.is_pinned()))
        return "response"

    result = ReaderPinMiddleware(response)(_Request("GET"))
    assert result == "response", \
        f"middleware must return the wrapped response, got {result!r}"
    assert observed == [(True, False)], \
        f"GET must be active and initially unpinned during handling, got {observed!r}"
    assert not pinning.is_active(), \
        "request scope must be cleared after the response"
    assert not pinning.is_pinned(), \
        "request pin must be cleared after the response"


@th.unit_test("reader middleware: mutating HTTP methods start pinned")
def test_mutating_methods_start_pinned(opts):
    from mojo.db import pinning
    from mojo.middleware.db_reader import ReaderPinMiddleware

    observed = {}

    def response(request):
        observed[request.method] = pinning.is_pinned()
        return None

    middleware = ReaderPinMiddleware(response)
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        middleware(_Request(method))

    assert observed == {
        "POST": True, "PUT": True, "PATCH": True, "DELETE": True,
    }, f"every mutating method must start pinned, got {observed!r}"


@th.unit_test("reader middleware: safe HTTP methods start unpinned")
def test_safe_methods_start_unpinned(opts):
    from mojo.db import pinning
    from mojo.middleware.db_reader import ReaderPinMiddleware

    observed = {}

    def response(request):
        observed[request.method] = pinning.is_pinned()
        return None

    middleware = ReaderPinMiddleware(response)
    for method in ("GET", "HEAD", "OPTIONS"):
        middleware(_Request(method))

    assert observed == {
        "GET": False, "HEAD": False, "OPTIONS": False,
    }, f"safe methods must start unpinned, got {observed!r}"


@th.unit_test("reader middleware: a write pin does not leak to the next request")
def test_pin_does_not_survive_next_request(opts):
    from mojo.db import pinning
    from mojo.middleware.db_reader import ReaderPinMiddleware

    observed = []

    def response(request):
        observed.append(pinning.is_pinned())
        if len(observed) == 1:
            pinning.pin()
        return None

    middleware = ReaderPinMiddleware(response)
    middleware(_Request("GET"))
    middleware(_Request("GET"))

    assert observed == [False, False], \
        f"each GET must receive a fresh unpinned scope, got {observed!r}"
    assert not pinning.is_pinned(), \
        "the pin from the first request must not survive both responses"


@th.unit_test("reader middleware: exception paths restore prior context")
def test_exception_restores_prior_context(opts):
    from mojo.db import pinning
    from mojo.middleware.db_reader import ReaderPinMiddleware

    outer_tokens = pinning.activate(pinned=True)
    try:
        def response(request):
            assert pinning.is_active(), \
                "the nested request must have an active routing scope"
            assert not pinning.is_pinned(), \
                "the nested GET must start unpinned"
            raise RuntimeError("test response failure")

        try:
            ReaderPinMiddleware(response)(_Request("GET"))
            assert False, "the wrapped response exception must propagate"
        except RuntimeError as error:
            assert str(error) == "test response failure", \
                f"the original exception must propagate, got {error!r}"

        assert pinning.is_active(), \
            "middleware must restore the caller's active scope on exception"
        assert pinning.is_pinned(), \
            "middleware must restore the caller's pin on exception"
    finally:
        pinning.deactivate(outer_tokens)


@th.django_unit_test("reader auth: credential resolution always uses primary")
def test_authentication_resolution_uses_primary(opts):
    from mojo.db import pinning
    from mojo.db.router import ReaderRouter
    from mojo.middleware import auth

    prefix = "dbreadertest"
    observed = []

    def handler(token, request):
        observed.append(ReaderRouter().db_for_read(_Model))
        return object(), None

    prior = auth.AUTH_BEARER_HANDLERS_CACHE.get(prefix)
    auth.AUTH_BEARER_HANDLERS_CACHE[prefix] = handler
    tokens = pinning.activate()
    try:
        request = type("Request", (), {
            "META": {"HTTP_AUTHORIZATION": f"{prefix} token"},
        })()
        result = auth.AuthenticationMiddleware(
            lambda request: None).process_request(request)
        assert result is None, \
            f"successful credential resolution must continue the request, got {result!r}"
        assert observed == ["default"], \
            f"credential lookup must be forced to primary, got {observed!r}"
    finally:
        pinning.deactivate(tokens)
        if prior is None:
            auth.AUTH_BEARER_HANDLERS_CACHE.pop(prefix, None)
        else:
            auth.AUTH_BEARER_HANDLERS_CACHE[prefix] = prior
