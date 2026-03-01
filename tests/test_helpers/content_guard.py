from testit import helpers as th


# ── Username: valid accepts ──────────────────────────────────────────────────

@th.django_unit_test()
def test_username_accept_normal(opts):
    from mojo.helpers.content_guard import check_username
    for name in ["alice", "bob_smith", "user123", "a1b2c3"]:
        result = check_username(name)
        assert result.decision == "allow", f"Expected allow for '{name}', got {result.decision}: {result.reasons}"


@th.django_unit_test()
def test_username_accept_edge_lengths(opts):
    from mojo.helpers.content_guard import check_username
    result = check_username("abc")
    assert result.decision == "allow", f"Expected allow for 'abc', got {result.decision}: {result.reasons}"
    result = check_username("a" * 20)
    assert result.decision == "allow", f"Expected allow for 20-char name, got {result.decision}: {result.reasons}"


@th.django_unit_test()
def test_username_accept_with_underscore(opts):
    from mojo.helpers.content_guard import check_username
    result = check_username("cool_user_name")
    assert result.decision == "allow", f"Expected allow for 'cool_user_name', got {result.decision}: {result.reasons}"


# ── Username: format rejections ──────────────────────────────────────────────

@th.django_unit_test()
def test_username_reject_too_short(opts):
    from mojo.helpers.content_guard import check_username
    result = check_username("ab")
    assert result.decision == "block", f"Expected block for 'ab', got {result.decision}"
    assert "too_short" in result.reasons, f"Expected 'too_short' in reasons, got {result.reasons}"


@th.django_unit_test()
def test_username_reject_too_long(opts):
    from mojo.helpers.content_guard import check_username
    result = check_username("a" * 21)
    assert result.decision == "block", f"Expected block for 21-char name, got {result.decision}"
    assert "too_long" in result.reasons, f"Expected 'too_long' in reasons, got {result.reasons}"


@th.django_unit_test()
def test_username_reject_invalid_chars(opts):
    from mojo.helpers.content_guard import check_username
    for name in ["user!name", "hello world", "caf\u00e9", "user@home"]:
        result = check_username(name)
        assert result.decision == "block", f"Expected block for '{name}', got {result.decision}"
        assert "invalid_chars" in result.reasons, f"Expected 'invalid_chars' for '{name}', got {result.reasons}"


@th.django_unit_test()
def test_username_reject_leading_underscore(opts):
    from mojo.helpers.content_guard import check_username
    result = check_username("_badname")
    assert result.decision == "block", f"Expected block for '_badname', got {result.decision}"
    assert "leading_separator" in result.reasons, f"Expected 'leading_separator' in reasons, got {result.reasons}"


@th.django_unit_test()
def test_username_reject_trailing_underscore(opts):
    from mojo.helpers.content_guard import check_username
    result = check_username("badname_")
    assert result.decision == "block", f"Expected block for 'badname_', got {result.decision}"
    assert "trailing_separator" in result.reasons, f"Expected 'trailing_separator' in reasons, got {result.reasons}"


@th.django_unit_test()
def test_username_reject_double_underscore(opts):
    from mojo.helpers.content_guard import check_username
    result = check_username("bad__name")
    assert result.decision == "block", f"Expected block for 'bad__name', got {result.decision}"
    assert "double_separator" in result.reasons, f"Expected 'double_separator' in reasons, got {result.reasons}"


@th.django_unit_test()
def test_username_reject_all_digits(opts):
    from mojo.helpers.content_guard import check_username
    result = check_username("123456")
    assert result.decision == "block", f"Expected block for '123456', got {result.decision}"
    assert "all_digits" in result.reasons, f"Expected 'all_digits' in reasons, got {result.reasons}"


# ── Username: reserved ───────────────────────────────────────────────────────

@th.django_unit_test()
def test_username_reject_reserved(opts):
    from mojo.helpers.content_guard import check_username
    for name in ["admin", "support", "moderator", "root"]:
        result = check_username(name)
        assert result.decision == "block", f"Expected block for reserved '{name}', got {result.decision}"
        assert "reserved" in result.reasons, f"Expected 'reserved' for '{name}', got {result.reasons}"


# ── Username: deny matching and evasion ──────────────────────────────────────

@th.django_unit_test()
def test_username_reject_exact_deny(opts):
    from mojo.helpers.content_guard import check_username
    result = check_username("fuck")
    assert result.decision == "block", f"Expected block for 'fuck', got {result.decision}"
    assert "deny_exact" in result.reasons, f"Expected 'deny_exact' in reasons, got {result.reasons}"


@th.django_unit_test()
def test_username_reject_deny_substring(opts):
    from mojo.helpers.content_guard import check_username
    result = check_username("xfuckx")
    assert result.decision == "block", f"Expected block for 'xfuckx', got {result.decision}"
    assert "deny_substring" in result.reasons, f"Expected 'deny_substring' in reasons, got {result.reasons}"


@th.django_unit_test()
def test_username_reject_deny_short_substring(opts):
    """myassSucks should be caught via 'ass' substring."""
    from mojo.helpers.content_guard import check_username
    result = check_username("myassSucks")
    assert result.decision == "block", f"Expected block for 'myassSucks', got {result.decision}"
    assert "deny_substring" in result.reasons, f"Expected 'deny_substring' for 'myassSucks', got {result.reasons}"


@th.django_unit_test()
def test_username_reject_separator_evasion(opts):
    """f_u_c_k should be caught via collapsed variant."""
    from mojo.helpers.content_guard import check_username
    result = check_username("f_u_c_k")
    assert result.decision == "block", f"Expected block for 'f_u_c_k', got {result.decision}: {result.reasons}"


@th.django_unit_test()
def test_username_reject_leet_evasion(opts):
    """a55hole should be caught via leet variant (5->s)."""
    from mojo.helpers.content_guard import check_username
    result = check_username("a55hole")
    assert result.decision == "block", f"Expected block for 'a55hole', got {result.decision}: {result.reasons}"


@th.django_unit_test()
def test_username_reject_repeat_evasion(opts):
    """fucckkk should be caught via squeezed variant."""
    from mojo.helpers.content_guard import check_username
    result = check_username("fucckkk")
    assert result.decision == "block", f"Expected block for 'fucckkk', got {result.decision}: {result.reasons}"


@th.django_unit_test()
def test_username_reject_combined_evasion(opts):
    """fu_cc_kk should be caught via combined variant (collapsed + squeezed)."""
    from mojo.helpers.content_guard import check_username
    result = check_username("fu_cc_kk")
    assert result.decision == "block", f"Expected block for 'fu_cc_kk', got {result.decision}: {result.reasons}"


# ── Username: safelist ───────────────────────────────────────────────────────

@th.django_unit_test()
def test_username_safelist_prevents_false_positive(opts):
    """'assistant' contains 'ass' but should be safelisted."""
    from mojo.helpers.content_guard import check_username
    result = check_username("assistant")
    assert result.decision == "allow", f"Expected allow for 'assistant', got {result.decision}: {result.reasons}"


@th.django_unit_test()
def test_username_safelist_cocktail(opts):
    """'cocktail' contains 'cock' but should be safelisted."""
    from mojo.helpers.content_guard import check_username
    result = check_username("cocktail")
    assert result.decision == "allow", f"Expected allow for 'cocktail', got {result.decision}: {result.reasons}"


@th.django_unit_test()
def test_username_safelist_classic(opts):
    from mojo.helpers.content_guard import check_username
    result = check_username("classic")
    assert result.decision == "allow", f"Expected allow for 'classic', got {result.decision}: {result.reasons}"


# ── Username: suggest ────────────────────────────────────────────────────────

@th.django_unit_test()
def test_suggest_username_cleans_invalid_chars(opts):
    from mojo.helpers.content_guard import suggest_username
    suggestion = suggest_username("Hello World!")
    assert suggestion is not None, f"Expected a suggestion for 'Hello World!', got None"
    assert " " not in suggestion, f"Expected no spaces in suggestion, got '{suggestion}'"


@th.django_unit_test()
def test_suggest_username_returns_none_for_unsalvageable(opts):
    from mojo.helpers.content_guard import suggest_username
    suggestion = suggest_username("!@#")
    assert suggestion is None, f"Expected None for '!@#', got '{suggestion}'"


# ── Text: innocuous content ──────────────────────────────────────────────────

@th.django_unit_test()
def test_text_allow_clean_content(opts):
    from mojo.helpers.content_guard import check_text
    result = check_text("This is a perfectly normal comment about the weather.")
    assert result.decision == "allow", f"Expected allow for clean text, got {result.decision}: {result.reasons}"
    assert result.score == 0, f"Expected score 0 for clean text, got {result.score}"


@th.django_unit_test()
def test_text_allow_empty(opts):
    from mojo.helpers.content_guard import check_text
    result = check_text("")
    assert result.decision == "allow", f"Expected allow for empty text, got {result.decision}"
    assert result.score == 0, f"Expected score 0 for empty text, got {result.score}"


# ── Text: profanity ──────────────────────────────────────────────────────────

@th.django_unit_test()
def test_text_deny_hit(opts):
    from mojo.helpers.content_guard import check_text
    result = check_text("What the fuck is going on")
    assert result.decision in ("warn", "block"), f"Expected warn/block for profanity, got {result.decision}"
    assert "deny_hit" in result.reasons or "high_severity" in result.reasons, f"Expected deny reason, got {result.reasons}"
    assert result.score > 0, f"Expected score > 0 for profanity, got {result.score}"


@th.django_unit_test()
def test_text_high_severity(opts):
    from mojo.helpers.content_guard import check_text
    result = check_text("You are a faggot")
    assert result.decision in ("warn", "block"), f"Expected warn/block for high severity, got {result.decision}"
    assert "high_severity" in result.reasons, f"Expected 'high_severity' in reasons, got {result.reasons}"
    assert result.score >= 50, f"Expected score >= 50 for high severity, got {result.score}"


@th.django_unit_test()
def test_text_repeated_profanity_scores_higher(opts):
    from mojo.helpers.content_guard import check_text
    single = check_text("That is shit")
    double = check_text("That is shit and also fuck you")
    assert double.score > single.score, f"Expected double profanity score ({double.score}) > single ({single.score})"


# ── Text: spam detection ─────────────────────────────────────────────────────

@th.django_unit_test()
def test_text_spam_link(opts):
    from mojo.helpers.content_guard import check_text
    result = check_text("Check out https://spam-site.com for deals!")
    assert "spam_link" in result.reasons, f"Expected 'spam_link' in reasons, got {result.reasons}"
    assert result.score > 0, f"Expected score > 0 for spam link, got {result.score}"
    link_matches = [m for m in result.matches if m.type == "spam_link"]
    assert len(link_matches) > 0, f"Expected at least one spam_link match, got {len(link_matches)}"
    assert link_matches[0].span is not None, f"Expected span on link match, got None"


@th.django_unit_test()
def test_text_spam_phone(opts):
    from mojo.helpers.content_guard import check_text
    result = check_text("Call me at 555-123-4567 for a good time")
    assert "spam_phone" in result.reasons, f"Expected 'spam_phone' in reasons, got {result.reasons}"
    phone_matches = [m for m in result.matches if m.type == "spam_phone"]
    assert len(phone_matches) > 0, f"Expected at least one spam_phone match, got {len(phone_matches)}"
    assert phone_matches[0].span is not None, f"Expected span on phone match, got None"


@th.django_unit_test()
def test_text_excessive_repetition(opts):
    from mojo.helpers.content_guard import check_text
    result = check_text("AAAAAAAAA this is sooooooo annoying")
    assert "excessive_repetition" in result.reasons, f"Expected 'excessive_repetition' in reasons, got {result.reasons}"


@th.django_unit_test()
def test_text_excessive_caps(opts):
    from mojo.helpers.content_guard import check_text
    result = check_text("THIS IS ALL CAPS AND I AM YELLING AT YOU RIGHT NOW")
    assert "excessive_caps" in result.reasons, f"Expected 'excessive_caps' in reasons, got {result.reasons}"


@th.django_unit_test()
def test_text_repeated_words(opts):
    from mojo.helpers.content_guard import check_text
    result = check_text("buy buy buy buy buy now")
    assert "repeated_words" in result.reasons, f"Expected 'repeated_words' in reasons, got {result.reasons}"


# ── Text: safelist prevents false positives ──────────────────────────────────

@th.django_unit_test()
def test_text_safelist_assistant(opts):
    """The word 'assistant' should not trigger 'ass' deny."""
    from mojo.helpers.content_guard import check_text
    result = check_text("My assistant helped me today")
    assert result.decision == "allow", f"Expected allow for 'assistant' text, got {result.decision}: {result.reasons}"


@th.django_unit_test()
def test_text_safelist_classic(opts):
    from mojo.helpers.content_guard import check_text
    result = check_text("That was a classic performance")
    assert result.decision == "allow", f"Expected allow for 'classic' text, got {result.decision}: {result.reasons}"


# ── Text: threshold configurability ──────────────────────────────────────────

@th.django_unit_test()
def test_text_custom_thresholds(opts):
    from mojo.helpers.content_guard import check_text
    strict_policy = {"text_warn_threshold": 5, "text_block_threshold": 10}
    result = check_text("This has a link https://example.com", policy=strict_policy)
    assert result.decision == "block", f"Expected block with strict thresholds, got {result.decision}"

    lenient_policy = {"text_warn_threshold": 90, "text_block_threshold": 99}
    result = check_text("This has a link https://example.com", policy=lenient_policy)
    assert result.decision == "allow", f"Expected allow with lenient thresholds, got {result.decision}"


# ── Text: score capping ──────────────────────────────────────────────────────

@th.django_unit_test()
def test_text_score_capped_at_100(opts):
    from mojo.helpers.content_guard import check_text
    toxic = "FUCK SHIT DAMN https://spam.com https://more.com 555-123-4567 AAAAAAA BUY BUY BUY BUY"
    result = check_text(toxic)
    assert result.score <= 100, f"Expected score <= 100, got {result.score}"


# ── Policy: custom username policy ───────────────────────────────────────────

@th.django_unit_test()
def test_username_custom_min_max(opts):
    from mojo.helpers.content_guard import check_username
    result = check_username("ab", policy={"username_min_len": 2})
    assert result.decision == "allow", f"Expected allow for 'ab' with min_len=2, got {result.decision}"
    result = check_username("abcdefghijk", policy={"username_max_len": 10})
    assert result.decision == "block", f"Expected block for 11-char name with max_len=10, got {result.decision}"


@th.django_unit_test()
def test_username_allow_dots(opts):
    from mojo.helpers.content_guard import check_username
    result = check_username("first.last")
    assert result.decision == "block", f"Expected block for 'first.last' without dot policy, got {result.decision}"
    result = check_username("first.last", policy={"allow_dot_in_username": True})
    assert result.decision == "allow", f"Expected allow for 'first.last' with dot policy, got {result.decision}"


# ── Debug: normalized output ─────────────────────────────────────────────────

@th.django_unit_test()
def test_debug_normalized_output(opts):
    from mojo.helpers.content_guard import check_username, check_text
    policy = {"include_debug_normalized": True}
    result = check_username("test_user", policy=policy)
    assert result.normalized is not None, f"Expected normalized dict for username, got None"
    assert "raw" in result.normalized, f"Expected 'raw' in normalized, got {list(result.normalized.keys())}"

    result = check_text("Hello world", policy=policy)
    assert result.normalized is not None, f"Expected normalized dict for text, got None"
    assert "display" in result.normalized, f"Expected 'display' in normalized, got {list(result.normalized.keys())}"


# ── Rules: extra lists ───────────────────────────────────────────────────────

@th.django_unit_test()
def test_load_rules_extra_deny(opts):
    from mojo.helpers.content_guard import load_rules, check_username
    rules = load_rules(extra_deny={"badword"})
    result = check_username("badword", rules)
    assert result.decision == "block", f"Expected block for extra deny 'badword', got {result.decision}"


@th.django_unit_test()
def test_load_rules_extra_reserved(opts):
    from mojo.helpers.content_guard import load_rules, check_username
    rules = load_rules(extra_reserved={"myapp"})
    result = check_username("myapp", rules)
    assert result.decision == "block", f"Expected block for extra reserved 'myapp', got {result.decision}"
    assert "reserved" in result.reasons, f"Expected 'reserved' in reasons, got {result.reasons}"


# ── Username: skeleton matching (consonant skeleton catches embedded profanity)

@th.django_unit_test()
def test_username_skeleton_duckfick(opts):
    """DuckFick -> skeleton 'dckfck' contains 'fck' (skeleton of 'fuck')."""
    from mojo.helpers.content_guard import check_username
    result = check_username("DuckFick")
    assert result.decision == "block", f"Expected block for 'DuckFick', got {result.decision}: {result.reasons}"
    assert "deny_skeleton" in result.reasons, f"Expected 'deny_skeleton' in reasons, got {result.reasons}"


@th.django_unit_test()
def test_username_skeleton_duck_safe(opts):
    """'duck' alone should NOT be blocked (safelisted)."""
    from mojo.helpers.content_guard import check_username
    result = check_username("duck")
    assert result.decision == "allow", f"Expected allow for 'duck', got {result.decision}: {result.reasons}"


@th.django_unit_test()
def test_username_skeleton_dock_safe(opts):
    """'dock' alone should NOT be blocked (safelisted)."""
    from mojo.helpers.content_guard import check_username
    result = check_username("dock")
    assert result.decision == "allow", f"Expected allow for 'dock', got {result.decision}: {result.reasons}"


@th.django_unit_test()
def test_username_skeleton_disabled(opts):
    """Skeleton matching should be disableable via policy."""
    from mojo.helpers.content_guard import check_username
    result = check_username("DuckFick", policy={"enable_skeleton_match": False})
    assert "deny_skeleton" not in result.reasons, f"Expected no 'deny_skeleton' with skeleton disabled, got {result.reasons}"


# ── Username: reversed matching (catches reversed slurs)

@th.django_unit_test()
def test_username_reversed_reggin(opts):
    """'reggin' reversed = 'nigger', should be caught."""
    from mojo.helpers.content_guard import check_username
    result = check_username("reggin")
    assert result.decision == "block", f"Expected block for 'reggin', got {result.decision}: {result.reasons}"
    assert "deny_reversed" in result.reasons, f"Expected 'deny_reversed' in reasons, got {result.reasons}"


@th.django_unit_test()
def test_username_reversed_kcuf(opts):
    """'kcuf' reversed = 'fuck', should be caught."""
    from mojo.helpers.content_guard import check_username
    result = check_username("kcuf")
    assert result.decision == "block", f"Expected block for 'kcuf', got {result.decision}: {result.reasons}"
    assert "deny_reversed" in result.reasons, f"Expected 'deny_reversed' in reasons, got {result.reasons}"


@th.django_unit_test()
def test_username_reversed_disabled(opts):
    """Reversed matching should be disableable via policy."""
    from mojo.helpers.content_guard import check_username
    result = check_username("reggin", policy={"enable_reversed_match": False})
    assert "deny_reversed" not in result.reasons, f"Expected no 'deny_reversed' with reversed disabled, got {result.reasons}"


# ── Username: phonetic evasion

@th.django_unit_test()
def test_username_phonetic_phuck(opts):
    """'phuck' -> phonetic 'fuck', should be blocked."""
    from mojo.helpers.content_guard import check_username
    result = check_username("phuck")
    assert result.decision == "block", f"Expected block for 'phuck', got {result.decision}: {result.reasons}"


# ── Username: foreign profanity

@th.django_unit_test()
def test_username_foreign_spanish(opts):
    """Spanish profanity should be blocked."""
    from mojo.helpers.content_guard import check_username
    for name in ["puta", "mierda", "pendejo"]:
        result = check_username(name)
        assert result.decision == "block", f"Expected block for '{name}', got {result.decision}: {result.reasons}"


@th.django_unit_test()
def test_username_foreign_italian(opts):
    """Italian profanity should be blocked."""
    from mojo.helpers.content_guard import check_username
    result = check_username("cazzo")
    assert result.decision == "block", f"Expected block for 'cazzo', got {result.decision}: {result.reasons}"


@th.django_unit_test()
def test_username_foreign_russian(opts):
    """Transliterated Russian profanity should be blocked."""
    from mojo.helpers.content_guard import check_username
    for name in ["blyat", "suka"]:
        result = check_username(name)
        assert result.decision == "block", f"Expected block for '{name}', got {result.decision}: {result.reasons}"


@th.django_unit_test()
def test_username_foreign_french(opts):
    """French profanity should be blocked."""
    from mojo.helpers.content_guard import check_username
    result = check_username("putain")
    assert result.decision == "block", f"Expected block for 'putain', got {result.decision}: {result.reasons}"


@th.django_unit_test()
def test_username_foreign_german(opts):
    """German profanity should be blocked."""
    from mojo.helpers.content_guard import check_username
    result = check_username("scheisse")
    assert result.decision == "block", f"Expected block for 'scheisse', got {result.decision}: {result.reasons}"


# ── Username: compound abbreviation

@th.django_unit_test()
def test_username_compound_abbreviation(opts):
    """Compound abbreviations like lmfao, omfg should be blocked."""
    from mojo.helpers.content_guard import check_username
    for name in ["lmfao", "omfg"]:
        result = check_username(name)
        assert result.decision == "block", f"Expected block for '{name}', got {result.decision}: {result.reasons}"


# ── Username: safelist foreign false positives

@th.django_unit_test()
def test_username_safelist_reputation(opts):
    """Words containing foreign deny substrings should be safe."""
    from mojo.helpers.content_guard import check_username
    # 'reputation' contains 'puta' but is safelisted
    result = check_username("reputation")
    assert result.decision == "allow", f"Expected allow for 'reputation', got {result.decision}: {result.reasons}"


# ── Text: leet evasion in text

@th.django_unit_test()
def test_text_leet_sh1t(opts):
    """'sh1t' in text should be caught via decoded matching (1->i)."""
    from mojo.helpers.content_guard import check_text
    result = check_text("That is total sh1t")
    assert result.decision in ("warn", "block"), f"Expected warn/block for 'sh1t' text, got {result.decision}"
    assert any(r in result.reasons for r in ("deny_hit", "high_severity")), f"Expected deny reason for 'sh1t', got {result.reasons}"


@th.django_unit_test()
def test_text_phonetic_phuck(opts):
    """'phuck' in text should be caught via decoded matching (ph->f)."""
    from mojo.helpers.content_guard import check_text
    result = check_text("Oh phuck that is bad")
    assert result.decision in ("warn", "block"), f"Expected warn/block for 'phuck' text, got {result.decision}"


@th.django_unit_test()
def test_text_decoded_disabled(opts):
    """Decoded text matching should be disableable."""
    from mojo.helpers.content_guard import check_text
    # with decoded matching off, 'sh1t' should not be caught
    result = check_text("That is total sh1t", policy={"enable_text_decoded_match": False})
    # 'sh1t' won't match 'shit' in searchable form since 1 is stripped as non-alnum
    deny_matches = [m for m in result.matches if m.type in ("deny_substring", "deny_high_sev")]
    assert len(deny_matches) == 0, f"Expected no deny matches with decoded off, got {len(deny_matches)} matches"


# ── Text: foreign profanity in text

@th.django_unit_test()
def test_text_foreign_profanity(opts):
    """Foreign profanity in text should be detected (scored)."""
    from mojo.helpers.content_guard import check_text
    result = check_text("That was total mierda")
    assert result.score > 0, f"Expected score > 0 for 'mierda' text, got {result.score}"
    assert "deny_hit" in result.reasons, f"Expected 'deny_hit' in reasons for 'mierda', got {result.reasons}"


# ── Text: safelist foreign false positives in text

@th.django_unit_test()
def test_text_safelist_computer(opts):
    """'computer' contains 'puta' substring but should be safe."""
    from mojo.helpers.content_guard import check_text
    result = check_text("I bought a new computer today")
    assert result.decision == "allow", f"Expected allow for 'computer' text, got {result.decision}: {result.reasons}"


@th.django_unit_test()
def test_text_safelist_unique_technique(opts):
    """'unique' and 'technique' contain 'nique' but should be safe."""
    from mojo.helpers.content_guard import check_text
    result = check_text("That unique technique was impressive")
    assert result.decision == "allow", f"Expected allow for 'unique technique' text, got {result.decision}: {result.reasons}"


@th.django_unit_test()
def test_text_safelist_mississippi(opts):
    """'mississippi' contains 'piss' but should be safe."""
    from mojo.helpers.content_guard import check_text
    result = check_text("We drove through mississippi last summer")
    assert result.decision == "allow", f"Expected allow for 'mississippi' text, got {result.decision}: {result.reasons}"
