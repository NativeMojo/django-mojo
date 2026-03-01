# content_guard

Deterministic content moderation for usernames and block text. No LLM calls, no heavy ML, no external APIs.

## Usage

```python
from mojo.helpers import content_guard

# check a username at registration — just works, default rules auto-loaded
result = content_guard.check_username("new_user")
if result.decision == "block":
    print(f"Rejected: {result.reasons}")

# check a comment
result = content_guard.check_text("Some user comment")
if result.decision == "block":
    print(f"Blocked: {result.reasons}")
elif result.decision == "warn":
    print(f"Flagged for review (score={result.score}): {result.reasons}")

# log match details
for match in result.matches:
    print(f"  {match.type}: '{match.value}' span={match.span}")
```

## API

All functions use default rules automatically. Pass custom `rules` only when needed.

### `check_username(username, rules=None, policy=None)`

Check a username. Returns `Result` with `decision` of `"allow"` or `"block"`.

Checks: format validation, reserved names, deny list matching across normalized variants (raw, collapsed, leet, dedup, squeezed, combined, phonetic, skeleton), consonant skeleton matching, reversed text matching, safelist overrides, edit-distance-1 for high-severity terms.

### `check_text(text, rules=None, surface="comment", policy=None)`

Check block text. Returns `Result` with `decision` of `"allow"`, `"warn"`, or `"block"`.

Scoring components: deny term hits (weighted by severity), decoded text matching (catches leet/phonetic evasions like `sh1t`, `phuck`), spam links, phone numbers, excessive repetition, excessive caps. Score is 0-100, mapped to decisions via thresholds.

### `suggest_username(username, rules=None, policy=None)`

Returns a cleaned username string or `None` if unsalvageable.

### `load_rules(...)`

Load custom rules. Only needed if you want to extend or override the defaults.

```python
rules = load_rules(extra_deny={"customword"}, extra_reserved={"myapp"})
result = content_guard.check_username("customword", rules)
```

### `DEFAULT_POLICY`

Dict of all configurable defaults. Override by passing a `policy` dict:

```python
result = content_guard.check_username("user", policy={"username_min_len": 2})
result = content_guard.check_text("text", policy={"text_block_threshold": 50})
```

## Policy Options

| Key | Default | Description |
|-----|---------|-------------|
| `username_min_len` | 3 | Minimum username length |
| `username_max_len` | 20 | Maximum username length |
| `allow_dot_in_username` | False | Allow dots in usernames |
| `forbid_leading_sep` | True | Block leading `_` or `.` |
| `forbid_trailing_sep` | True | Block trailing `_` or `.` |
| `forbid_double_sep` | True | Block `__` or `..` |
| `forbid_all_digits` | True | Block all-digit usernames |
| `deny_substring_min_len` | 3 | Min deny term length for substring matching |
| `enable_ed1_high_sev` | True | Edit-distance-1 matching for short high-severity terms |
| `ed1_max_len` | 6 | Max term length for ed1 matching |
| `enable_skeleton_match` | True | Consonant skeleton matching (catches DuckFick → fck) |
| `enable_reversed_match` | True | Reversed text matching for high-severity terms (catches reggin) |
| `enable_text_decoded_match` | True | Leet/phonetic decoded matching in text (catches sh1t, phuck) |
| `text_warn_threshold` | 35 | Score threshold for "warn" decision |
| `text_block_threshold` | 70 | Score threshold for "block" decision |
| `link_weight` | 25 | Score added per link detected |
| `phone_weight` | 20 | Score added per phone number |
| `repetition_weight` | 15 | Score added for excessive repetition |
| `caps_weight` | 10 | Score added for excessive caps |
| `deny_weight` | 30 | Score per normal deny hit |
| `high_sev_weight` | 50 | Score per high-severity deny hit |
| `repeat_deny_weight` | 15 | Extra score per additional deny hit |
| `include_debug_normalized` | False | Include normalized forms in result |

## Extending Word Lists

Add custom terms at load time:

```python
from mojo.helpers.content_guard import load_rules

rules = load_rules(extra_deny={"newterm"}, extra_safe={"scunthorpe"})
result = content_guard.check_username("newterm", rules)
```

Or provide your own files:

```python
rules = load_rules(deny_path="/app/data/my_deny.txt")
```

File format: one term per line, `#` for comments.

## Data Types

- **Result**: `decision`, `reasons` (list of stable codes), `matches` (list of Match), `score` (0-100), `normalized` (optional debug dict)
- **Match**: `type`, `value`, `span` (tuple or None), `variant` (which normalization hit)

## Reason Codes

Usernames: `too_short`, `too_long`, `invalid_chars`, `leading_separator`, `trailing_separator`, `double_separator`, `all_digits`, `reserved`, `deny_exact`, `deny_substring`, `deny_skeleton`, `deny_reversed`, `deny_ed1`

Text: `deny_hit`, `high_severity`, `repeated_profanity`, `spam_link`, `spam_phone`, `excessive_repetition`, `repeated_words`, `excessive_caps`

## Evasion Detection

The library catches a wide range of evasion techniques:

- **Leet speak**: `a55hole` → `asshole`, `sh1t` → `shit` (15 character substitutions)
- **Separator insertion**: `f_u_c_k` → `fuck` (underscores, dots, hyphens, dashes, tildes)
- **Character repetition**: `fucckkk` → `fuck` (collapsed to single chars)
- **Phonetic substitution**: `phuck` → `fuck` (`ph→f`, `kn→n`, `wr→r`)
- **Consonant skeleton**: `DuckFick` → skeleton `dckfck` contains `fck` (skeleton of `fuck`)
- **Reversed text**: `reggin` → reversed = `nigger` (high-severity terms only)
- **Homoglyphs**: Cyrillic/Greek lookalikes normalized to Latin (`а→a`, `о→o`, etc.)
- **Accent stripping**: `fùck` → `fuck` (diacritical marks removed)
- **Multi-language**: Spanish, French, German, Portuguese, Italian, Russian (transliterated)
- **Edit distance**: Near-misses of high-severity terms caught within 1 edit
