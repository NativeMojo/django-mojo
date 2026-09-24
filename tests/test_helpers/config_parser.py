"""Quoted config literals must decode the escapes emitted by repr()."""
from pathlib import Path

from testit import helpers as th


@th.django_unit_test()
def test_quoted_config_strings_round_trip_repr(opts):
    from mojo.helpers.settings.parser import DjangoConfigLoader

    loader = DjangoConfigLoader(Path("unused"))
    for value in ("line 1\nline 2\n", "\r\n", "a\tb", "quotes: ' and \"", "C:\\new\\file", "雪", "", "@literal"):
        assert loader._parse_value(repr(value)) == value, "Quoted strings must round-trip escapes and Unicode"


@th.django_unit_test()
def test_legacy_quoted_config_falls_back_without_executing(opts):
    from mojo.helpers.settings.parser import DjangoConfigLoader

    loader = DjangoConfigLoader(Path("unused"))
    assert loader._parse_value("'can't'") == "can't", "Legacy non-literal quoted strings retain quote stripping"
    assert loader._parse_value("'one','two'") == "one','two", "Quoted legacy text must not turn into a tuple"
    assert loader._parse_value("'__import__(\"os\").getcwd()'") == '__import__("os").getcwd()', "Quoted text is never executed"
