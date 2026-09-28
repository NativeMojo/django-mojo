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
