"""Routing and ContextVar isolation for the optional database reader."""

import threading

from testit import helpers as th


class _Meta:
    app_label = "reader_test"


class _SessionMeta:
    app_label = "sessions"


class _Model:
    _meta = _Meta()


class _SessionModel:
    _meta = _SessionMeta()


class _State:
    def __init__(self, db):
        self.db = db


class _Object:
    def __init__(self, db):
        self._state = _State(db)


@th.django_unit_test("reader router: reads outside an HTTP scope use primary")
def test_unscoped_read_uses_primary(opts):
    from mojo.db.router import ReaderRouter

    alias = ReaderRouter().db_for_read(_Model)
    assert alias == "default", \
        f"an unscoped read must use primary, got {alias!r}"


@th.django_unit_test("reader router: active unpinned scopes use the reader")
def test_active_scope_uses_reader(opts):
    from mojo.db import pinning
    from mojo.db.router import ReaderRouter

    tokens = pinning.activate()
    try:
        alias = ReaderRouter().db_for_read(_Model)
        assert alias == "reader", \
            f"an active unpinned read must use the reader, got {alias!r}"
    finally:
        pinning.deactivate(tokens)


@th.django_unit_test("reader router: writes pin later reads to primary")
def test_write_pins_later_reads(opts):
    from mojo.db import pinning
    from mojo.db.router import ReaderRouter

    router = ReaderRouter()
    tokens = pinning.activate()
    try:
        alias = router.db_for_write(_Model)
        assert alias == "default", \
            f"writes must always use primary, got {alias!r}"
        assert pinning.is_pinned(), \
            "db_for_write must pin the current context"
        alias = router.db_for_read(_Model)
        assert alias == "default", \
            f"a read after a write must stay on primary, got {alias!r}"
    finally:
        pinning.deactivate(tokens)


@th.django_unit_test("reader router: a write outranks use_reader")
def test_write_inside_reader_scope_repins(opts):
    from mojo.db import pinning, use_reader
    from mojo.db.router import ReaderRouter

    router = ReaderRouter()
    tokens = pinning.activate()
    try:
        with use_reader():
            router.db_for_write(_Model)
            alias = router.db_for_read(_Model)
            assert alias == "default", \
                f"a write inside use_reader must re-pin later reads, got {alias!r}"
    finally:
        pinning.deactivate(tokens)


@th.django_unit_test("reader router: use_reader starts a fresh unpinned sub-scope")
def test_reader_scope_clears_stale_pin_on_entry(opts):
    from mojo.db import pinning, use_reader
    from mojo.db.router import ReaderRouter

    router = ReaderRouter()
    tokens = pinning.activate(pinned=True)
    try:
        with use_reader():
            alias = router.db_for_read(_Model)
            assert alias == "reader", \
                f"use_reader must clear a stale pin on entry, got {alias!r}"
        assert pinning.is_pinned(), \
            "use_reader must restore the caller's pin when it exits"
    finally:
        pinning.deactivate(tokens)


@th.django_unit_test("reader router: atomic blocks always read from primary")
def test_atomic_block_uses_primary(opts):
    from django.db import transaction
    from mojo.db import pinning, use_reader
    from mojo.db.router import ReaderRouter

    router = ReaderRouter()
    tokens = pinning.activate()
    try:
        with use_reader():
            with transaction.atomic():
                alias = router.db_for_read(_Model)
                assert alias == "default", \
                    f"an atomic read must use primary even under use_reader, got {alias!r}"
    finally:
        pinning.deactivate(tokens)


@th.django_unit_test("reader router: explicit primary routing wins")
def test_use_primary_forces_primary(opts):
    from mojo.db import pinning, use_primary
    from mojo.db.router import ReaderRouter

    tokens = pinning.activate()
    try:
        with use_primary():
            alias = ReaderRouter().db_for_read(_Model)
            assert alias == "default", \
                f"use_primary must force primary in an active scope, got {alias!r}"
    finally:
        pinning.deactivate(tokens)


@th.django_unit_test("reader router: use_reader works outside request scopes and resets")
def test_use_reader_outside_scope_is_bounded(opts):
    from mojo.db import use_reader
    from mojo.db.router import ReaderRouter

    router = ReaderRouter()
    before = router.db_for_read(_Model)
    with use_reader():
        inside = router.db_for_read(_Model)
    after = router.db_for_read(_Model)

    assert before == "default", \
        f"the unscoped baseline must use primary, got {before!r}"
    assert inside == "reader", \
        f"use_reader must opt an unscoped block onto the reader, got {inside!r}"
    assert after == "default", \
        f"use_reader must restore primary routing on exit, got {after!r}"


@th.django_unit_test("reader router: session reads always use primary")
def test_session_model_uses_primary(opts):
    from mojo.db import pinning, use_reader
    from mojo.db.router import ReaderRouter

    tokens = pinning.activate()
    try:
        with use_reader():
            alias = ReaderRouter().db_for_read(_SessionModel)
            assert alias == "default", \
                f"session rows must never route to a lagging reader, got {alias!r}"
    finally:
        pinning.deactivate(tokens)


@th.django_unit_test("reader router: migrations are allowed only on primary")
def test_migrations_use_primary_only(opts):
    from mojo.db.router import ReaderRouter

    router = ReaderRouter()
    assert router.allow_migrate("default", "reader_test") is True, \
        "migrations must be allowed on the default database"
    assert router.allow_migrate("reader", "reader_test") is False, \
        "migrations must be refused on the reader database"


@th.django_unit_test("reader router: default and reader objects may be related")
def test_default_reader_relation_is_allowed(opts):
    from mojo.db.router import ReaderRouter

    router = ReaderRouter()
    result = router.allow_relation(_Object("default"), _Object("reader"))
    assert result is True, \
        f"objects from aliases backed by the same data must be relatable, got {result!r}"
    result = router.allow_relation(_Object("analytics"), _Object("default"))
    assert result is None, \
        f"unrelated aliases must be left to other routers, got {result!r}"


@th.django_unit_test("reader router: a fresh thread does not inherit request scope")
def test_fresh_thread_uses_primary(opts):
    from mojo.db import pinning
    from mojo.db.router import ReaderRouter

    observed = []
    tokens = pinning.activate()
    try:
        thread = threading.Thread(
            target=lambda: observed.append(ReaderRouter().db_for_read(_Model)))
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive(), \
            "the routing-isolation worker thread did not finish"
        assert observed == ["default"], \
            f"a fresh thread must start outside the request scope, got {observed!r}"
    finally:
        pinning.deactivate(tokens)
