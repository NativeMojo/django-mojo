"""
BlocklistEntry — validation, map mechanics, and the seed.

The rendering contract under test: rows are DATA that land inside
double-quoted `map` patterns and `geo` entries, allow rows render FIRST
(nginx map regexes match in order of appearance), `off` rows render
nowhere, and a duplicate network can never reach a `geo` block (that is an
nginx [emerg] that would freeze fleet convergence).
"""

from testit import helpers as th

from tests.test_edge._helpers import (
    declare_pools,
    cleanup, declare_reserved_names, ensure_blocklist_seed, make_certificate,
    make_domain, make_group, make_upstream, make_vhost, raises,
)


def _clean_rows():
    """Drop rows this module creates. The SEED rows are deliberately left
    alone — tests assert on them."""
    from mojo.apps.edge.models import BlocklistEntry

    BlocklistEntry.objects.filter(note__startswith="bltest").delete()


def _row(kind, value, mode="log", note="bltest"):
    from mojo.apps.edge.models import BlocklistEntry

    return BlocklistEntry.objects.create(
        kind=kind, value=value, mode=mode, note=note)


@th.django_unit_setup()
def setup_blocklist(opts):
    cleanup()
    _clean_rows()
    # The runner flushes tables but keeps migration records, so the 0004
    # seed is re-applied here — through the migration's own function.
    ensure_blocklist_seed()
    declare_reserved_names()
    declare_pools()
    opts.group = make_group("edgebl")
    opts.domain = make_domain(group=opts.group)
    opts.certificate = make_certificate(opts.domain)
    opts.upstream = make_upstream(host="127.0.0.1", port=8600)
    opts.generation = "c" * 64


@th.django_unit_test("hostile ua patterns are rejected, not escaped")
def test_ua_pattern_rejects_injection(opts):
    from mojo.apps.edge import validators

    class Fake:
        def __init__(self, kind, value, mode="log"):
            self.kind, self.value, self.mode = kind, value, mode

    hostile = [
        'x" 1; } server {',            # the canonical quoted-string breakout
        'x"',
        "x`whoami`",
        "x{2}",                        # braces are nginx block chars
        "x;",
        "bad bot",                     # whitespace
        "bad\tbot",
        "bad\nbot",
        "bad$host",                    # $ is nginx interpolation
        "bad'",
        "x\\",                         # trailing odd backslash escapes the quote
        "x\\\\\\",
        "a" * 257,                     # over the cap
        "(unbalanced",                 # does not compile
        "",
        None,
    ]
    for candidate in hostile:
        err = raises(validators.validate_blocklist_entry,
                     Fake("ua", candidate))
        assert err is not None, \
            f"validate_blocklist_entry accepted {candidate!r} — that value " \
            f"reaches a rendered map"

    legitimate = ["^Lynx", "libwww-perl", "80legs", "Chrome/[1-9][0-9]\\..*",
                  "x\\\\", "bot(one|two)", "^Head.*less"]
    for candidate in legitimate:
        err = raises(validators.validate_blocklist_entry,
                     Fake("ua", candidate))
        assert err is None, \
            f"validate_blocklist_entry rejected the legitimate {candidate!r}: {err}"


@th.django_unit_test("ip rows must parse and are STORED normalized")
def test_ip_normalization(opts):
    from mojo.apps.edge import validators
    from mojo.apps.edge.models import BlocklistEntry

    class Fake:
        def __init__(self, value):
            self.kind, self.value, self.mode = "ip", value, "log"

    for candidate in ["not-an-ip", "1.2.3.4;", "1.2.3.4/999", "1.2.3",
                      "", "1.2.3.4 5.6.7.8"]:
        err = raises(validators.validate_blocklist_entry, Fake(candidate))
        assert err is not None, \
            f"validate_blocklist_entry accepted the non-network {candidate!r}"

    row = _row("ip", "10.1.2.3/8")
    assert row.value == "10.0.0.0/8", (
        f"a CIDR was not stored normalized: {row.value!r} — two spellings "
        "of one network is a geo [emerg]")

    row = _row("ip", "192.168.7.9")
    assert row.value == "192.168.7.9/32", \
        f"a bare address was not stored as its /32: {row.value!r}"

    err = raises(BlocklistEntry.objects.create, kind="ip",
                 value="10.99.0.0/8", mode="log", note="bltest")
    assert err is not None, (
        "a second spelling of an already-stored network was accepted — "
        "normalization plus unique(kind, value) should refuse it")


@th.django_unit_test("mode mechanics: allow first, log watches, enforce blocks, off vanishes")
def test_mode_rendering(opts):
    from mojo.apps.edge.services import render

    rows = [
        dict(id=901, kind="ua", value="^Lynx", mode="allow"),
        dict(id=902, kind="ua", value="badbot", mode="enforce"),
        dict(id=903, kind="ua", value="watchbot", mode="log"),
        dict(id=904, kind="ip", value="203.0.113.0/24", mode="enforce"),
        dict(id=905, kind="ip", value="198.51.100.7/32", mode="log"),
        dict(id=906, kind="ip", value="192.0.2.1/32", mode="allow"),
    ]
    text = render.render_http_base(security=rows)

    block_ua = text[text.index("$edge_block_ua"):]
    block_ua = block_ua[:block_ua.index("}")]
    assert '"~*badbot" 902;' in block_ua, \
        "an enforce ua row is missing from the block map"
    assert '"~*watchbot"' not in block_ua, \
        "a log ua row leaked into the BLOCK map"
    assert block_ua.index('"~*^Lynx" 0;') < block_ua.index('"~*badbot" 902;'), \
        "the allow row does not render FIRST — order is what makes it win"

    watch_ua = text[text.index("$edge_watch_ua"):]
    watch_ua = watch_ua[:watch_ua.index("}")]
    assert '"~*watchbot" 903;' in watch_ua, \
        "a log ua row is missing from the watch map"
    assert '"~*badbot"' not in watch_ua, \
        "an enforce ua row leaked into the WATCH map"
    assert '"~*^Lynx" 0;' in watch_ua, \
        "the allow row must exempt from the watch map too"

    block_ip = text[text.index("$edge_block_ip"):]
    block_ip = block_ip[:block_ip.index("}")]
    assert "203.0.113.0/24 904;" in block_ip, \
        "an enforce ip row is missing from the block geo"
    assert "192.0.2.1/32 0;" in block_ip, \
        "the allow ip row is missing from the block geo"
    assert "198.51.100.7" not in block_ip, \
        "a log ip row leaked into the BLOCK geo"

    watch_ip = text[text.index("$edge_watch_ip"):]
    watch_ip = watch_ip[:watch_ip.index("}")]
    assert "198.51.100.7/32 905;" in watch_ip, \
        "a log ip row is missing from the watch geo"

    assert 'map "$edge_watch_ip:$edge_watch_ua" $edge_watch {' in text, \
        "the watch combiner map is missing"
    assert "log_format edge_watch" in text, "the watch log_format is missing"
    assert "edge_watch.log edge_watch if=$edge_watch;" in text, \
        "the watch access_log is missing"


@th.django_unit_test("off rows render nowhere and drop out of the payload")
def test_off_renders_nowhere(opts):
    from mojo.apps.edge.services import render

    row = _row("ua", "offbot9x", mode="off")
    text = render.render_http_base()
    assert "offbot9x" not in text, "an off row was rendered"

    values = {r["value"] for r in render.blocklist_payload()}
    assert "offbot9x" not in values, \
        "an off row joined the security payload — flipping modes would not " \
        "move the hash correctly"

    row.mode = "log"
    row.save()
    text = render.render_http_base()
    assert '"~*offbot9x"' in text, \
        "flipping off->log did not bring the row back"


@th.django_unit_test("render-time dedupe keeps a duplicate network out of the geo block")
def test_render_dedupe(opts):
    from mojo.apps.edge.services import render

    rows = [
        dict(id=911, kind="ip", value="203.0.113.0/24", mode="enforce"),
        dict(id=912, kind="ip", value="203.0.113.0/24", mode="enforce"),
        dict(id=913, kind="ua", value="dupbot", mode="log"),
        dict(id=914, kind="ua", value="dupbot", mode="log"),
    ]
    text = render.render_http_base(security=rows)
    assert text.count("203.0.113.0/24") == 1, (
        "a duplicate network rendered twice in a geo block — that is an "
        "nginx [emerg] on every node in the fleet")
    assert text.count('"~*dupbot"') == 1, "a duplicate ua pattern rendered twice"


@th.django_unit_test("a hostile row smuggled past save() is refused at render")
def test_render_reasserts(opts):
    from mojo.apps.edge.services import render

    for rows in (
            [dict(id=921, kind="ua", value='x" 1; } server {', mode="log")],
            [dict(id=922, kind="ua", value="x\\", mode="enforce")],
            [dict(id=923, kind="ip", value="1.2.3.4; root /etc", mode="enforce")],
    ):
        err = raises(render.render_http_base, security=rows)
        assert err is not None, (
            f"the renderer accepted the bypass-written blocklist row "
            f"{rows[0]['value']!r}")


@th.django_unit_test("every kind's server block carries both guards")
def test_guards_in_every_kind(opts):
    from mojo.apps.edge.services import render

    cases = [
        ("blg1", dict(kind="site")),
        ("blg2", dict(kind="api", upstream=opts.upstream)),
        ("blg3", dict(kind="site_api")),
        ("blg4", dict(kind="redirect", redirect_to="www.example.com")),
    ]
    for label, kwargs in cases:
        vhost = make_vhost(opts.domain, opts.certificate, label=label, **kwargs)
        text = render.render_vhost(vhost, opts.generation)
        assert "if ($edge_block_ip) { return 444; }" in text, \
            f"{kwargs['kind']}: the ip guard is missing"
        assert "if ($edge_block_ua) { return 444; }" in text, \
            f"{kwargs['kind']}: the ua guard is missing"


@th.django_unit_test("a quiet path is quiet for the MAIN log, never for the watch")
def test_quiet_path_keeps_watch(opts):
    from mojo.apps.edge.services import render

    vhost = make_vhost(opts.domain, opts.certificate, label="blq", kind="api",
                       upstream=opts.upstream, quiet_paths=["/healthz"])
    text = render.render_vhost(vhost, opts.generation)

    quiet_at = text.index("location = /healthz {")
    quiet_block = text[quiet_at:text.index("proxy_pass", quiet_at)]
    assert "edge_watch.log edge_watch if=$edge_watch;" in quiet_block, (
        "the quiet location does not re-emit the watch access_log — a "
        "watched client's health probes would be a blind spot")
    assert "access_log off" not in quiet_block, (
        "the quiet location uses `access_log off`, which silences the "
        "security watch too")


@th.django_unit_test("the seed imported the skeleton content, log-first")
def test_seed_content(opts):
    from mojo.apps.edge.models import BlocklistEntry

    seed = BlocklistEntry.objects.filter(
        note="seed: django-mojo-skeleton sec.d")

    lynx = seed.filter(kind="ua", value="^Lynx").first()
    assert lynx is not None, "the Lynx row is missing from the seed"
    assert lynx.mode == "allow", (
        f"the Lynx exception must import as ALLOW, not {lynx.mode} — its UA "
        "contains libwww-FM, which the libwww token matches")

    assert seed.filter(kind="ua", value="libwww", mode="log").exists(), \
        "the libwww token (the reason Lynx needs an allow) is missing"
    assert seed.filter(kind="ua", value="libwww-perl", mode="log").exists(), \
        "the libwww-perl exact row is missing"
    assert seed.filter(kind="ua", value="HeadlessChrome", mode="log").exists(), \
        "the HeadlessChrome row is missing"

    # The 2,635-char alternation split per token: spot-check both ends.
    for token in ("80legs", "^WallpapersHD", "SemrushBot", "^BackDoorBot"):
        assert seed.filter(kind="ua", value=token, mode="log").exists(), \
            f"the alternation token {token!r} is missing from the seed"

    count = seed.filter(kind="ua").count()
    assert count >= 260, (
        f"expected the per-token split to produce 260+ ua rows, found "
        f"{count} — the alternation was not split")
    for row in seed:
        assert len(row.value) <= 256, \
            f"seed row {row.value[:40]!r} exceeds the 256-char cap"

    assert not seed.filter(kind="ip").exists(), \
        "blocked_ips.conf is empty upstream — the seed must add no ip rows"
    assert not seed.filter(mode="enforce").exists(), \
        "the seed must be LOG-FIRST — nothing imports as enforce"


@th.django_unit_test("unique(kind, value) holds")
def test_unique_conflict(opts):
    from mojo.apps.edge.models import BlocklistEntry

    _row("ua", "uniqbot")
    err = raises(BlocklistEntry.objects.create, kind="ua", value="uniqbot",
                 mode="enforce", note="bltest")
    assert err is not None, "two rows carried the same (kind, value)"


@th.django_unit_test("a blocklist change moves the generation id")
def test_blocklist_converges(opts):
    from mojo.apps.edge.services import render

    vhost = make_vhost(opts.domain, opts.certificate, label="blgen")
    before = render.desired_state([vhost])["generation"]
    row = _row("ua", "convergebot", mode="enforce")
    after = render.desired_state([vhost])["generation"]
    assert after != before, (
        "adding a blocklist row did not move the generation id — the fleet "
        "would never converge onto it")

    row.mode = "off"
    row.save()
    off = render.desired_state([vhost])["generation"]
    assert off == before, (
        "an off row still influences the hash — off must render nowhere and "
        "hash as nothing")
