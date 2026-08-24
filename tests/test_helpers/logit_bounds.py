"""Regression coverage for bounded framework file logging (Maestro #1243)."""

from testit import helpers as th

TESTIT_TIER = "bug"  # #2792 tier curation


class _FakeElapsed:
    def total_seconds(self):
        return 0.001


class _FakeResponse:
    def __init__(self, data=None, text=None, json_error=None, status_code=200):
        self._data = {} if data is None else data
        self.text = text if text is not None else "{}"
        self.content = self.text.encode("utf-8")
        self._json_error = json_error
        self.status_code = status_code
        self.ok = 200 <= status_code < 400
        self.reason = "OK" if self.ok else "Error"
        self.headers = {}
        self.elapsed = _FakeElapsed()

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._data


def _open_temp_logger(log_path):
    from uuid import uuid4
    from mojo.helpers import logit

    name = f"test_logit_bounds_{uuid4().hex}"
    logger = logit.get_logger(name, str(log_path))
    logger.logger.propagate = False
    return name, logger


def _close_temp_logger(name, logger):
    from mojo.helpers import logit

    for handler in list(logger.logger.handlers):
        handler.flush()
        handler.close()
        logger.logger.removeHandler(handler)
    if logit.LOG_MANAGER is not None:
        logit.LOG_MANAGER.loggers.pop(name, None)


@th.unit_test("pretty_format preserves the existing small coloured representation")
def test_pretty_format_small_output_unchanged(opts):
    from mojo.helpers.logit import PrettyLogger

    rendered = PrettyLogger.pretty_format({"b": "two", "a": [1]})
    expected = (
        "{\n"
        "  \033[34m\"a\"\033[0m: [\n"
        "    \033[33m1\033[0m\n"
        "  ],\n"
        "  \033[34m\"b\"\033[0m: \033[31m\"two\"\033[0m\n"
        "}"
    )
    assert rendered == expected, (
        "Small values must retain the existing sorted, indented ANSI rendering; "
        f"got {rendered!r}"
    )


@th.unit_test("pretty_format clips one ASCII scalar and reports omitted bytes")
def test_pretty_format_ascii_value_cap(opts):
    from mojo.helpers.logit import PrettyLogger

    rendered = PrettyLogger.pretty_format(
        {"payload": "x" * 1000}, max_length=256, value_max_length=100)
    encoded = rendered.encode("utf-8")
    assert len(encoded) <= 256, (
        f"Pretty output must stay within the 256-byte total cap, got {len(encoded)} bytes"
    )
    assert "900 more bytes" in rendered, (
        f"Scalar marker must report the 900 omitted ASCII bytes, got {rendered!r}"
    )
    assert rendered.endswith("}"), f"Truncated dict should remain visibly closed: {rendered!r}"


@th.unit_test("pretty_format counts multibyte UTF-8 without splitting code points")
def test_pretty_format_unicode_value_cap(opts):
    from mojo.helpers.logit import PrettyLogger

    rendered = PrettyLogger.pretty_format(
        {"payload": "🙂" * 100}, max_length=256, value_max_length=32)
    encoded = rendered.encode("utf-8")
    assert len(encoded) <= 256, (
        f"Unicode pretty output must stay within 256 bytes, got {len(encoded)}"
    )
    assert "368 more bytes" in rendered, (
        f"Eight emojis fit 32 bytes, so the marker must report 368 omitted bytes: {rendered!r}"
    )
    assert "�" not in rendered, f"UTF-8 clipping must not create replacement characters: {rendered!r}"


@th.unit_test("pretty_format caps aggregate output from many small values")
def test_pretty_format_total_cap_many_values(opts):
    from mojo.helpers.logit import PrettyLogger

    payload = {f"key_{idx:05d}": "ok" for idx in range(10000)}
    rendered = PrettyLogger.pretty_format(
        payload, max_length=512, value_max_length=64)
    encoded = rendered.encode("utf-8")
    assert len(encoded) <= 512, (
        f"Aggregate pretty output must stay within 512 bytes, got {len(encoded)}"
    )
    assert "output truncated" in rendered, (
        f"Aggregate clipping must be explicit, got {rendered!r}"
    )


@th.unit_test("pretty_format stops rendering later values after the total cap")
def test_pretty_format_stops_after_total_cap(opts):
    from mojo.helpers.logit import PrettyLogger

    class ExplodesIfRendered:
        def __str__(self):
            raise AssertionError("formatter visited a value after reaching its output cap")

    payload = {
        **{f"a_{idx:02d}": "x" * 1024 for idx in range(10)},
        "z_explode": ExplodesIfRendered(),
    }
    rendered = PrettyLogger.pretty_format(
        payload, max_length=512, value_max_length=256)
    assert len(rendered.encode("utf-8")) <= 512
    assert "output truncated" in rendered, rendered


@th.unit_test("pretty_format escapes user-controlled terminal sequences")
def test_pretty_format_escapes_terminal_controls(opts):
    from mojo.helpers.logit import PrettyLogger

    malicious = "\033]8;;https://evil.test\007click\033]8;;\007" + ("x" * 1000)
    rendered = PrettyLogger.pretty_format(
        {"payload": malicious}, max_length=256, value_max_length=96)
    assert "\033]8" not in rendered, (
        f"User-controlled OSC sequences must not remain executable: {rendered!r}"
    )
    assert "\007" not in rendered, f"Terminal bell must be escaped: {rendered!r}"
    assert "\\x1b]8" in rendered, (
        f"Escaped terminal control should remain visible for diagnosis: {rendered!r}"
    )
    assert "more bytes" in rendered, rendered


@th.unit_test("Logger._build_log owns one cap across strings and dictionaries")
def test_build_log_caps_complete_record(opts):
    from mojo.helpers.logit import Logger, MAX_LOG_RECORD_BYTES

    logger = Logger("bounded", None, None)
    rendered = logger._build_log(
        "prefix",
        "x" * (MAX_LOG_RECORD_BYTES * 2),
        {"payload": "y" * (MAX_LOG_RECORD_BYTES * 2)},
    )
    encoded = rendered.encode("utf-8")
    assert len(encoded) <= MAX_LOG_RECORD_BYTES, (
        f"Complete record must stay within {MAX_LOG_RECORD_BYTES} bytes, got {len(encoded)}"
    )
    assert rendered.startswith("prefix\n"), (
        f"Arguments that fit before truncation must preserve order, got {rendered[:80]!r}"
    )
    assert "more bytes" in rendered or "output truncated" in rendered, (
        f"Record clipping must be visible, got tail {rendered[-120:]!r}"
    )


@th.unit_test("RestClient oversized JSON request produces a bounded disk record")
def test_rest_client_request_log_is_bounded(opts):
    from pathlib import Path
    import tempfile
    from unittest import mock
    from mojo.helpers.logit import MAX_LOG_RECORD_BYTES
    from testit.client import RestClient

    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "request.log"
        name, logger = _open_temp_logger(log_path)
        try:
            client = RestClient("http://example.test", logger=logger)
            response = _FakeResponse(data={"ok": True}, text='{"ok": true}')
            payload = {"payload": "x" * (MAX_LOG_RECORD_BYTES * 4)}
            with mock.patch.object(client.session, "request", return_value=response):
                result = client.post("/oversized", payload)
            for handler in logger.logger.handlers:
                handler.flush()

            raw = log_path.read_bytes()
            text = raw.decode("utf-8")
            assert result.status_code == 200, (
                f"Fake request should complete normally, got {result.status_code}"
            )
            assert len(raw) <= MAX_LOG_RECORD_BYTES + 4096, (
                "One capped payload record plus the client's small request/response records "
                f"should stay below {MAX_LOG_RECORD_BYTES + 4096} bytes, got {len(raw)}"
            )
            assert "more bytes" in text or "output truncated" in text, (
                f"On-disk request log must disclose clipping, got tail {text[-160:]!r}"
            )
        finally:
            _close_temp_logger(name, logger)


@th.unit_test("RestClient exception path bounds a huge plain response body")
def test_rest_client_error_body_log_is_bounded(opts):
    from pathlib import Path
    import tempfile
    from unittest import mock
    from mojo.helpers.logit import MAX_LOG_RECORD_BYTES
    from testit.client import RestClient

    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "response_error.log"
        name, logger = _open_temp_logger(log_path)
        try:
            client = RestClient("http://example.test", logger=logger)
            response = _FakeResponse(
                text="z" * (MAX_LOG_RECORD_BYTES * 4),
                json_error=RuntimeError("synthetic JSON failure"),
                status_code=500,
            )
            with mock.patch.object(client.session, "request", return_value=response):
                result = client.post("/broken", {"small": True})
            for handler in logger.logger.handlers:
                handler.flush()

            raw = log_path.read_bytes()
            text = raw.decode("utf-8")
            assert result.error == "synthetic JSON failure", (
                f"Client should preserve the synthetic failure, got {result!r}"
            )
            assert len(raw) <= MAX_LOG_RECORD_BYTES + 4096, (
                "One capped exception record plus small request records should stay below "
                f"{MAX_LOG_RECORD_BYTES + 4096} bytes, got {len(raw)}"
            )
            assert "more bytes" in text or "output truncated" in text, (
                f"On-disk exception log must disclose clipping, got tail {text[-160:]!r}"
            )
        finally:
            _close_temp_logger(name, logger)
