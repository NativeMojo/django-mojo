"""content_guard false positives (#5774): whole-word matching, stopwords, hashes.

Deny terms match only as whole words (or listed forms), so work words such as
"passed", "mass" and "assign" score nothing, while every listed profane word
keeps its django-mojo 1.30.1 score.
"""
from testit import helpers as th


# Every deny term and form with the score the 1.30.1 substring matcher gave it
# on its own, generated from that matcher and pinned. "dumbass" is the one
# listed word whose score changes: 1.30.1 hid it (0) through the whole-text
# safelist ("bass"); it now scores 30 like any listed word containing "ass".
SCORES_1_30_1 = {
    "arschloch": 30, "ass": 30, "asshat": 30, "asshole": 75, "asswipe": 30,
    "bastard": 30, "batard": 30, "beaner": 30, "bitch": 30, "bitched": 30,
    "bitches": 30, "bitchier": 30, "bitchiest": 30, "bitching": 30, "bitchy": 30,
    "blyad": 30, "blyat": 30, "buceta": 30, "bullshit": 50, "bullshitted": 50,
    "bullshitting": 50, "cabron": 30, "caralho": 30, "cazzo": 30, "chingar": 30,
    "chink": 50, "cock": 30, "cocksucker": 30, "cojon": 30, "cojones": 75,
    "connard": 30, "connasse": 75, "cracker": 30, "crap": 30, "crapped": 30,
    "crapping": 30, "culo": 30, "cunt": 50, "damn": 30, "damned": 30, "debil": 30,
    "diaf": 30, "dick": 30, "dickhead": 30, "douche": 30, "douchebag": 30, "dumbass": 0,
    "dyke": 50, "ebat": 30, "enculer": 30, "fag": 50, "faggot": 100, "ffs": 30,
    "filhodaputa": 75, "foad": 30, "fotze": 30, "fuck": 50, "fucked": 50, "fucker": 95,
    "fuckers": 95, "fuckface": 50, "fuckhead": 50, "fucking": 95, "gandon": 30,
    "goddamn": 30, "goddamned": 30, "gook": 50, "gringo": 30, "gtfo": 30,
    "hijueputa": 75, "hurensohn": 30, "jackass": 75, "jackasses": 75, "joder": 30,
    "kike": 50, "lmfao": 30, "marica": 30, "merde": 30, "mierda": 30, "minchia": 30,
    "missgeburt": 30, "motherfucker": 100, "motherfucking": 95, "mudak": 30,
    "nahui": 30, "nigga": 50, "niggaz": 50, "nigger": 50, "nique": 30, "omfg": 30,
    "pendejo": 30, "pidar": 30, "piss": 30, "pissed": 30, "pisses": 30, "pissing": 30,
    "pizda": 30, "porra": 30, "prick": 30, "pussy": 30, "puta": 30, "putain": 75,
    "puto": 30, "puttana": 75, "retard": 50, "retarded": 100, "salaud": 30,
    "salope": 30, "scheisse": 30, "schlampe": 30, "schwuchtel": 30, "shit": 50,
    "shithead": 50, "shittier": 50, "shittiest": 50, "shitting": 50, "shitty": 50,
    "slut": 50, "slutty": 50, "smartass": 30, "spic": 50, "stfu": 30, "stronzo": 30,
    "suka": 30, "tranny": 50, "twat": 30, "vaffanculo": 75, "verga": 30, "viado": 30,
    "wanker": 30, "wetback": 30, "whore": 50, "whorehouse": 50, "wichser": 30,
    "wtf": 30,
}
CHANGED = {"dumbass": 30}
ALLOW = {"link_allow_domains": ["maestromojo.com"]}

INNOCENT_WORDS = [
    "mass", "as", "class", "pass", "passed", "password", "assign", "assignment",
    "assert", "assume", "assets", "bypass", "embarrass", "assess", "diffs",
    "suspicious", "despicable", "debate", "ridiculous", "restful", "smartwatch",
    "flame retardant", "esophagus", "cockpit", "Hitchcock", "Scunthorpe office",
]
NUMBERS_AND_SHORT_TOKENS = [
    "45", "445", "a5", "4s", "F5", "iPhone 4S", "see PR #45", "meet at 4:45", "3847",
]


@th.django_unit_test("work words containing a deny term score 0")
def test_innocent_words_score_zero(opts):
    from mojo.helpers.content_guard import check_text

    for text in INNOCENT_WORDS + NUMBERS_AND_SHORT_TOKENS:
        result = check_text(text)
        assert result.score == 0, (
            f"{text!r} is not profane and must score 0, got {result.score} {result.reasons}")


@th.django_unit_test("a standalone deny word and its evasions still score")
def test_standalone_and_evasions_score(opts):
    from mojo.helpers.content_guard import check_text

    for text in ["ass", "you-ass", "a.s.s", "a55", "asss", "class ass"]:
        result = check_text(text)
        assert result.score == 30, f"{text!r} must score 30 (ass), got {result.score} {result.reasons}"
        assert result.reasons == ["deny_hit"], f"{text!r} reasons, got {result.reasons}"
    for text in ["sh1t", "phuck", "fuuuck"]:
        result = check_text(text)
        assert result.score == 50, f"{text!r} must keep its 50, got {result.score}"
        assert "strong_profanity" in result.reasons, f"{text!r} reasons, got {result.reasons}"
        assert "high_severity" not in result.reasons, f"{text!r} is not a slur, got {result.reasons}"


@th.django_unit_test("every listed term and form keeps its 1.30.1 score")
def test_listed_words_keep_scores(opts):
    from mojo.helpers.content_guard import check_text
    from mojo.helpers.content_guard.core import _DEFAULT_RULES

    assert set(SCORES_1_30_1) == _DEFAULT_RULES.listed, (
        "the pinned table must cover exactly the deny terms and forms; regenerate it "
        "from the 1.30.1 matcher when a list changes")
    for word, expected in SCORES_1_30_1.items():
        expected = CHANGED.get(word, expected)
        result = check_text(word)
        assert result.score == expected, (
            f"{word!r} must score {expected} as in 1.30.1, got {result.score} {result.reasons}")
    for text, expected in [("fuck", 50), ("shit", 50), ("bullshit", 50), ("asshole", 75),
                           ("fucking", 95), ("motherfucker", 100), ("mierda", 30)]:
        result = check_text(text)
        assert result.score == expected, f"{text!r} must score {expected}, got {result.score}"


@th.django_unit_test("slurs give high_severity, other strong words strong_profanity")
def test_reason_codes(opts):
    from mojo.helpers.content_guard import check_text

    for text in ["You are a faggot", "n1gger", "kike"]:
        result = check_text(text)
        assert "high_severity" in result.reasons, f"{text!r} is a slur, got {result.reasons}"
        assert result.score >= 50, f"{text!r} must score at least 50, got {result.score}"
    for text in ["fuck", "holy shit it works", "this is bullshit"]:
        result = check_text(text)
        assert result.score == 50, f"{text!r} must score 50, got {result.score}"
        assert result.reasons == ["strong_profanity"], f"{text!r} reasons, got {result.reasons}"
    result = check_text("bastard bitch")
    assert result.score == 75, f"'bastard bitch' must score 75, got {result.score}"
    matches = [m for m in result.matches if m.type == "deny_word"]
    assert len(matches) == 2, f"two deny_word matches expected, got {result.matches}"
    assert matches[0].span == (0, 7), f"span must be the matched word, got {matches[0].span}"


@th.django_unit_test("names: whole words only")
def test_names(opts):
    from mojo.helpers.content_guard import check_text

    for name in ["Matsushita", "Harshita"]:
        result = check_text(name, surface="name", policy={"text_block_threshold": 50})
        assert result.decision == "allow", f"{name!r} must allow, got {result.decision} {result.reasons}"
    result = check_text("Shithead", surface="name", policy={"text_block_threshold": 50})
    assert result.decision == "block", f"'Shithead' must still flag, got {result.decision}"


@th.django_unit_test("repeated_words ignores stopwords")
def test_repeated_words_stopwords(opts):
    from mojo.helpers.content_guard import check_text

    for text in ["the the the the", "you you you you"]:
        result = check_text(text)
        assert result.score == 0, f"{text!r} repeats a stopword and must score 0, got {result.score}"
    result = check_text("buy buy buy buy buy now")
    assert result.score == 15, f"real word repetition must still score 15, got {result.score}"
    assert "repeated_words" in result.reasons, f"reasons, got {result.reasons}"


@th.django_unit_test("hex hashes and digit runs inside words are opaque")
def test_hashes_opaque(opts):
    import hashlib
    import random
    from mojo.helpers.content_guard import check_text

    rng = random.Random(5774)
    for _ in range(1000):
        digest = hashlib.sha1(str(rng.random()).encode()).hexdigest()
        for text in (digest, f"merged {digest} into main"):
            result = check_text(text)
            assert result.score == 0, f"hash text {text!r} must score 0, got {result.score} {result.reasons}"
    result = check_text("5551234567")
    assert "spam_phone" in result.reasons, f"a bare phone number still scores, got {result.reasons}"
    result = check_text("call5551234567")
    assert result.score == 0, f"digits glued to a word are not a phone, got {result.reasons}"


@th.django_unit_test("message 16909 reconstruction scores under 35")
def test_message_16909(opts):
    from mojo.helpers.content_guard import check_text

    # Reconstruction of Maestro message 16909's scoring shape: "passed", one
    # maestromojo.com link and "the" four times. 1.30.1 scored it 70.
    text = ("Release passed the checks. Notes: https://maestromojo.com/app/#/releases "
            "and the rollout starts at the top of the hour, the usual window.")
    result = check_text(text)
    assert result.score < 35, f"16909 must score under 35, got {result.score} {result.reasons}"
    result = check_text(text, policy=ALLOW)
    assert result.score == 0, f"16909 with maestromojo.com allowed must score 0, got {result.reasons}"


@th.django_unit_test("Rules and load_rules stay backward compatible")
def test_rules_backward_compatible(opts):
    from mojo.helpers.content_guard import Rules, check_text, load_rules

    rules = Rules({"darn"}, set(), set(), set())
    result = check_text("oh darn it", rules=rules)
    assert result.score == 30, f"positional Rules without new sets must still score, got {result.score}"
    rules = Rules(deny={"darn"}, high_severity={"darn"})
    result = check_text("darn darn", rules=rules)
    assert result.reasons == ["strong_profanity"], f"no slurs set means no high_severity, got {result.reasons}"
    rules = load_rules(extra_forms=["darnit"], extra_slurs=["darn"], extra_deny=["darn"])
    result = check_text("darnit", rules=rules)
    assert "high_severity" in result.reasons, f"extra_forms and extra_slurs must load, got {result.reasons}"


# ── Links and the domain allowlist ───────────────────────────────────────────

# (text, score without an allowlist or None, score with maestromojo.com allowed)
LINK_CASES = [
    ("https://evil.xyz/fuck", 75, 75),
    ("https://evil.xyz/5551234567", 25, 25),
    ("https://app.maestromojo.com/fuck-assert/5551234567", 75, 0),
    ("maestromojo.com/app/fuck", 75, 0),
    ("maestromojo.com/app/fuck-assert", 75, 0),
    ("https://MaestroMojo.COM./x", 25, 0),
    ("(maestromojo.com)", 25, 0),
    ("**maestromojo.com**", 25, 0),
    ("[maestromojo.com](https://evil.xyz)", 25, 25),
    ("[a.com](https://b.xyz)", 25, 25),
    ("[evil.xyz](https://maestromojo.com)", 25, 0),
    ("[see](https://maestromojo.com https://evil.xyz)", 50, 25),
    ("https://maestromojo.com@evil.xyz", 25, 25),
    ("https://evil.xyz\\@maestromojo.com", 25, 25),
    ("https://maestromojo.com.evil.xyz", 25, 25),
    ("https://evilmaestromojo.com", 25, 25),
    ("https://ma\u0435stromojo.com", 25, 25),
    ("evil.com/?u=https://maestromojo.com", 25, 25),
    ("https://[evil", 25, 25),
]


@th.django_unit_test("links: allowed hosts score nothing, the host comes from the address")
def test_link_allowlist(opts):
    from mojo.helpers.content_guard import check_text

    for text, plain, allowed in LINK_CASES:
        if plain is not None:
            result = check_text(text)
            assert result.score == plain, (
                f"{text!r} without an allowlist must score {plain}, got {result.score} {result.reasons}")
        result = check_text(text, policy=ALLOW)
        assert result.score == allowed, (
            f"{text!r} with maestromojo.com allowed must score {allowed}, got {result.score} {result.reasons}")
        if allowed == 0:
            assert not [m for m in result.matches if m.type == "spam_link"], (
                f"an allowed link adds no match: {result.matches}")


# (text, score without an allowlist, score with maestromojo.com and .ai allowed):
# the host is the whole address, whatever its suffix, port or query
ALLOW_TWO = {"link_allow_domains": ["maestromojo.com", "maestromojo.ai"]}
BARE_ADDRESS_CASES = [
    ("maestromojo.com.evil.dev", 25, 25),
    ("maestromojo.com.au", 25, 25),
    ("maestromojo.ai/app/fuck", 50, 0),
    ("maestromojo.ai/5551234567", 20, 0),
    ("app.maestromojo.ai#fuck", 50, 0),
    ("maestromojo.com:8000/fuck", 75, 0),
    ("maestromojo.com?x=fuck", 75, 0),
    ("maestromojo.com, fuck", 75, 50),
    ("maestromojo.ai.evil.dev/fuck", 50, 50),
    ("notmaestromojo.ai/fuck", 50, 50),
]


@th.django_unit_test("links: a bare address is read whole before its host is compared")
def test_bare_address_host(opts):
    from mojo.helpers.content_guard import check_text

    for text, plain, allowed in BARE_ADDRESS_CASES:
        result = check_text(text)
        assert result.score == plain, (
            f"{text!r} without an allowlist must score {plain}, got {result.score} {result.reasons}")
        result = check_text(text, policy=ALLOW_TWO)
        assert result.score == allowed, (
            f"{text!r} with maestromojo.com and .ai allowed must score {allowed}, "
            f"got {result.score} {result.reasons}")


# Rick's whole-address table (#5774 comment 53028): (text, score with
# maestromojo.com and .ai allowed, score without an allowlist, as on main)
HOST_CASES = [
    ("maestromojo.com@evil.dev", 25, 25),
    ("https://maestromojo.com@evil.dev", 25, 25),
    ("evil.dev/path/maestromojo.ai/fuck", 50, 50),
    ("https://evil.dev/path/maestromojo.ai/fuck", 75, 75),
    ("evil.com/path/maestromojo.com/x", 25, 25),
    ("see maestromojo.com and evil.com", 25, 50),
    ("maestromojo.com:8000/fuck", 0, 75),
    ("maestromojo.ai/app/fuck", 0, 50),
    ("[a.com](https://b.xyz)", 25, 25),
    ("[maestromojo.com](https://evil.xyz)", 25, 25),
    ("[evil.xyz](https://maestromojo.com)", 0, 25),
    ("maestromojo.com.evil.dev", 25, 25),
    ("maestromojo.com.au", 25, 25),
]


@th.django_unit_test("links: the host is parsed from the whole address; only a fully allowed host is exempt")
def test_whole_address_host(opts):
    from mojo.helpers.content_guard import check_text
    from mojo.helpers.content_guard.core import _DEFAULT_RULES, _allow_domains, _find_links

    domains = _allow_domains(ALLOW_TWO["link_allow_domains"])
    for text, allowed, plain in HOST_CASES:
        result = check_text(text, policy=ALLOW_TWO)
        assert result.score == allowed, (
            f"{text!r} with maestromojo.com and .ai allowed must score {allowed}, "
            f"got {result.score} {result.reasons}")
        result = check_text(text)
        assert result.score == plain, (
            f"{text!r} without an allowlist must score {plain}, got {result.score} {result.reasons}")
        links = _find_links(text, _DEFAULT_RULES, domains)
        if not any(link.allowed for link in links):
            assert allowed == plain, (
                f"{text!r} holds no allowed host {[link.host for link in links]}, so the "
                f"allowlist must not change its score ({allowed} vs {plain})")
    links = _find_links("maestromojo.com@evil.dev evil.dev/p/maestromojo.ai/x",
                        _DEFAULT_RULES, domains)
    assert [link.host for link in links] == ["evil.dev"], (
        f"hosts come from whole addresses, never a user name or path segment: {links}")


# Brenda's round-3 finding (#5774 comment 53085) and Rick's decision: a scheme
# inside one run starts the address only after an unclosed markdown opener or
# punctuation. (text, score with maestromojo.com and .ai allowed, without)
SCHEME_CASES = [
    ("evil.dev/path/https://maestromojo.com/fuck", 75, 75),
    ("[see](https://maestromojo.com https://evil.xyz)", 25, 50),
    ("(https://maestromojo.com/x)", 0, 25),
    ("**https://maestromojo.com**", 0, 25),
    ("evil.com/?u=https://maestromojo.com", 25, 25),
    ("x=https://maestromojo.com/fuck", 75, 75),
    # Rick's nested-address rule (comment 53104): a second address fails closed
    ("evil.dev/?next=https://maestromojo.com/fuck", 75, 75),
    ("maestromojo.ai/path/https://evil.dev/fuck", 75, 75),
    ("https://maestromojo.com/login?next=https://maestromojo.com/app", 25, 25),
    ("maestromojo.com/go/evil.xyz/fuck", 75, 75),
    ("maestromojo.com/docs/maestromojo.ai/x", 0, 25),
]
EMBED_JOINS = ["/", "/path/", "?u=", "=", "#", "@", ":"]


@th.django_unit_test("links: a scheme inside an address never replaces its host")
def test_embedded_scheme_host(opts):
    from mojo.helpers.content_guard import check_text

    for text, allowed, plain in SCHEME_CASES:
        result = check_text(text, policy=ALLOW_TWO)
        assert result.score == allowed, (
            f"{text!r} with maestromojo.com and .ai allowed must score {allowed}, "
            f"got {result.score} {result.reasons}")
        result = check_text(text)
        assert result.score == plain, (
            f"{text!r} without an allowlist must score {plain}, got {result.score} {result.reasons}")
    # when it is unclear which site an address points to, it scores as with no
    # allowlist -- in both directions, the allowed site first or last
    for join in EMBED_JOINS:
        for tail in ("https://maestromojo.com/fuck", "http://maestromojo.ai/fuck",
                     "maestromojo.com/fuck", "maestromojo.ai/fuck"):
            for text in (f"evil.dev{join}{tail}",
                         f"{tail.replace('/fuck', '')}{join}https://evil.dev/fuck"):
                allowed = check_text(text, policy=ALLOW_TWO)
                plain = check_text(text)
                assert allowed.score == plain.score, (
                    f"{text!r} joins an allowed site to one that is not, so it must score as "
                    f"with no allowlist: {allowed.score} {allowed.reasons} vs "
                    f"{plain.score} {plain.reasons}")


@th.django_unit_test("decoded terms count only where decoding is enabled, never in links")
def test_decoded_terms_policy(opts):
    from mojo.helpers.content_guard import check_text

    off = {"enable_text_decoded_match": False}
    for text, default, disabled in [("puttana", 75, 30), ("sh1t", 50, 0), ("fuck", 50, 50)]:
        result = check_text(text)
        assert result.score == default, f"{text!r} must score {default}, got {result.score}"
        result = check_text(text, policy=off)
        assert result.score == disabled, (
            f"{text!r} with decoding disabled must score {disabled}, got {result.score} {result.matches}")
    for policy in (None, off):
        result = check_text("https://evil.xyz/puttana", policy=policy)
        assert result.score == 55, (
            f"a link word is matched as written (link 25 + puttana 30), got {result.score} {result.matches}")


@th.django_unit_test("markdown: only the address is a link, the rest is read")
def test_markdown_links(opts):
    from mojo.helpers.content_guard import check_text

    result = check_text("[maestromojo.com](https://evil.xyz)", policy=ALLOW)
    links = [m.value for m in result.matches if m.type == "spam_link"]
    assert links == ["https://evil.xyz"], f"the markdown address is the link, got {links}"
    result = check_text("[x](a you are a faggot)")
    assert "high_severity" in result.reasons, (
        f"a tail that is not an address is read as words, got {result.reasons}")
    result = check_text("[see](https://maestromojo.com) and fuck", policy=ALLOW)
    assert result.score == 50, f"text around an allowed link is still read, got {result.score}"
    result = check_text("[a.com](https://b.xyz) and c.com")
    links = [m.value for m in result.matches if m.type == "spam_link"]
    assert links == ["https://b.xyz", "c.com"], (
        f"a markdown link counts once; text outside it is searched for links, got {links}")


@th.django_unit_test("link counts are unchanged without an allowlist")
def test_link_counts_unchanged(opts):
    from mojo.helpers.content_guard import check_text

    result = check_text("http://one.example http://two.example http://three.example")
    assert result.score == 75, f"three links score 75, got {result.score}"
    result = check_text("Check out https://spam-site.com for deals!")
    assert result.score == 25, f"one link scores 25, got {result.score}"


@th.django_unit_test("the REST check tool accepts link_allow_domains as a list or a string")
def test_rest_allow_domains(opts):
    from objict import objict
    from mojo.helpers.content_guard import on_rest_request

    for value in (["maestromojo.com", "github.com"], "maestromojo.com, github.com"):
        request = objict(DATA={"text": "see https://maestromojo.com/x and https://github.com/y",
                               "link_allow_domains": value})
        response = on_rest_request(request)
        assert response["text"]["score"] == 0, f"{value!r} must allow both hosts, got {response}"
