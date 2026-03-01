# content_guard — Django Developer Reference

## Overview

Deterministic content moderation for usernames and block text. No LLM calls, no ML models, no external APIs. Rules auto-load at import time — just call and go.

## Import

```python
from mojo.helpers import content_guard
```

## Quick Start

```python
from mojo.helpers import content_guard

# check a username
result = content_guard.check_username("new_user")
if result.decision == "block":
    print(f"Rejected: {result.reasons}")

# check text
result = content_guard.check_text("Some user comment")
if result.decision == "block":
    print(f"Blocked (score={result.score}): {result.reasons}")
elif result.decision == "warn":
    print(f"Flagged for review (score={result.score}): {result.reasons}")
```

## REST API Handler

Use `on_rest_request` to expose content checking via any REST endpoint. It reads `request.DATA` and returns a dict suitable for JSON response.

```python
import mojo.decorators as md
from mojo.helpers import content_guard

@md.POST('content/check')
@md.requires_auth()
def on_content_check(request):
    return content_guard.on_rest_request(request)
```

**Request fields:**

| Field | Description |
|-------|-------------|
| `username` | Username string to check |
| `text` | Text string to check |
| `surface` | Surface name for text checks (default `"comment"`) |
| Any policy key | Override that policy setting (see [Policy Options](#policy-options)) |

Send `username`, `text`, or both. Policy overrides are automatically coerced to the correct type (string `"false"` becomes `False`, string `"5"` becomes `5`).

**Request examples:**

```json
{"username": "DuckFick"}

{"text": "Check this comment", "text_block_threshold": 50}

{"username": "test_user", "text": "Hello world"}

{"username": "ab", "username_min_len": 2}
```

**Response format:**

```json
{
    "username": {
        "decision": "block",
        "reasons": ["deny_skeleton"],
        "score": 100,
        "matches": [
            {"type": "deny_skeleton", "value": "fuck", "span": null, "variant": "skeleton"}
        ]
    }
}
```

If both `username` and `text` are provided, both keys appear in the response. If neither is provided, returns `{"error": "...", "code": 400}`.

## API Reference

### `check_username(username, rules=None, policy=None)`

Check a username for validity and content violations.

Returns a result with `decision` of `"allow"` or `"block"`, `reasons` list, `matches` list, and `score` (0 or 100).

Checks performed:
- Format validation (length, allowed chars, separators, all-digits)
- Reserved name matching
- Deny list matching across 8 normalized variants
- Consonant skeleton matching (catches embedded profanity like "DuckFick")
- Reversed text matching (catches reversed slurs like "reggin")
- Edit-distance-1 fuzzy matching for high-severity terms
- Safelist override (prevents false positives like "assistant", "cocktail")

### `check_text(text, rules=None, surface="comment", policy=None)`

Check block text for moderation issues.

Returns a result with `decision` of `"allow"`, `"warn"`, or `"block"`, a `score` from 0-100, and detailed `matches`.

Scoring components:
- Deny term hits (weighted by severity)
- Decoded text matching (catches leet/phonetic evasions like `sh1t`, `phuck`)
- Spam detection: links, phone numbers
- Excessive repetition, caps, repeated words

### `suggest_username(username, rules=None, policy=None)`

Clean and validate a username. Returns a cleaned string or `None` if unsalvageable.

```python
suggestion = content_guard.suggest_username("Hello World!")
# Returns: "helloworld" (or similar cleaned form)
```

### `on_rest_request(request)`

Handle a Django REST request for content checking. See [REST API Handler](#rest-api-handler) above.

### `load_rules(...)`

Load custom rules. Only needed to extend or override defaults.

```python
from mojo.helpers.content_guard import load_rules

rules = load_rules(extra_deny={"customword"}, extra_safe={"scunthorpe"})
result = content_guard.check_username("customword", rules)
```

Parameters:
- `deny_path` — Custom deny list file path
- `high_severity_path` — Custom high-severity list file path
- `safe_path` — Custom safelist file path
- `reserved_path` — Custom reserved names file path
- `extra_deny` — Set of additional deny terms to merge
- `extra_safe` — Set of additional safe terms to merge
- `extra_reserved` — Set of additional reserved names to merge

File format: one term per line, `#` for comments.

## Policy Options

Override any of these by passing a `policy` dict:

```python
result = content_guard.check_username("ab", policy={"username_min_len": 2})
result = content_guard.check_text("text", policy={"text_block_threshold": 50})
```

### Username Format

| Key | Default | Description |
|-----|---------|-------------|
| `username_min_len` | 3 | Minimum username length |
| `username_max_len` | 20 | Maximum username length |
| `allow_dot_in_username` | False | Allow dots in usernames |
| `forbid_leading_sep` | True | Block leading `_` or `.` |
| `forbid_trailing_sep` | True | Block trailing `_` or `.` |
| `forbid_double_sep` | True | Block `__` or `..` |
| `forbid_all_digits` | True | Block all-digit usernames |

### Matching

| Key | Default | Description |
|-----|---------|-------------|
| `deny_substring_min_len` | 3 | Min deny term length for substring matching |
| `enable_ed1_high_sev` | True | Edit-distance-1 matching for high-severity terms |
| `ed1_max_len` | 6 | Max term length for ED1 matching |
| `enable_skeleton_match` | True | Consonant skeleton matching |
| `enable_reversed_match` | True | Reversed text matching (high-severity only) |
| `enable_text_decoded_match` | True | Leet/phonetic decoded matching in text |

### Text Scoring

| Key | Default | Description |
|-----|---------|-------------|
| `text_warn_threshold` | 35 | Score threshold for "warn" |
| `text_block_threshold` | 70 | Score threshold for "block" |
| `deny_weight` | 30 | Score per normal deny hit |
| `high_sev_weight` | 50 | Score per high-severity hit |
| `repeat_deny_weight` | 15 | Extra score per additional deny hit |
| `link_weight` | 25 | Score per link detected |
| `phone_weight` | 20 | Score per phone number |
| `repetition_weight` | 15 | Score for excessive repetition |
| `caps_weight` | 10 | Score for excessive caps |

### Debug

| Key | Default | Description |
|-----|---------|-------------|
| `include_debug_normalized` | False | Include normalized forms in result |

## Data Types

Results and matches are `objict` instances with attribute access:

- **Result**: `decision`, `reasons` (list), `matches` (list), `score` (0-100), `normalized` (dict or None)
- **Match**: `type`, `value`, `span` (tuple or None), `variant` (which normalization matched)

## Reason Codes

### Username

| Code | Meaning |
|------|---------|
| `too_short` | Below `username_min_len` |
| `too_long` | Above `username_max_len` |
| `invalid_chars` | Contains disallowed characters |
| `leading_separator` | Starts with `_` or `.` |
| `trailing_separator` | Ends with `_` or `.` |
| `double_separator` | Contains `__` or `..` |
| `all_digits` | Username is all numbers |
| `reserved` | Matches reserved name list |
| `deny_exact` | Exact match on deny list |
| `deny_substring` | Contains a deny term |
| `deny_skeleton` | Consonant skeleton contains a deny skeleton |
| `deny_reversed` | Reversed form matches a high-severity term |
| `deny_ed1` | Within edit distance 1 of a high-severity term |

### Text

| Code | Meaning |
|------|---------|
| `deny_hit` | Contains a deny term |
| `high_severity` | Contains a high-severity term |
| `repeated_profanity` | Multiple deny hits in same text |
| `spam_link` | Contains URL/link |
| `spam_phone` | Contains phone number |
| `excessive_repetition` | Character repeated 5+ times |
| `repeated_words` | Same word repeated 4+ times |
| `excessive_caps` | Over 70% uppercase |

## Evasion Detection

The library catches a wide range of evasion techniques:

- **Leet speak**: `a55hole` -> `asshole`, `sh1t` -> `shit` (15 character substitutions)
- **Separator insertion**: `f_u_c_k` -> `fuck` (underscores, dots, hyphens, dashes, tildes)
- **Character repetition**: `fucckkk` -> `fuck` (collapsed to single chars)
- **Phonetic substitution**: `phuck` -> `fuck` (`ph->f`, `kn->n`, `wr->r`)
- **Consonant skeleton**: `DuckFick` -> skeleton `dckfck` contains `fck` (skeleton of `fuck`)
- **Reversed text**: `reggin` -> reversed = `nigger` (high-severity terms only)
- **Homoglyphs**: Cyrillic/Greek lookalikes normalized to Latin
- **Accent stripping**: diacritical marks removed
- **Multi-language**: Spanish, French, German, Portuguese, Italian, Russian (transliterated)
- **Edit distance**: Near-misses of high-severity terms caught within 1 edit

## Word Lists

Bundled in `mojo/helpers/content_guard/data/`:

| File | Contents |
|------|----------|
| `deny.txt` | ~100 terms: English profanity, slurs, abbreviations, Spanish, French, German, Portuguese, Italian, Russian |
| `high_severity.txt` | ~17 terms: slurs and extreme language (get higher weight + ED1 matching) |
| `safe.txt` | ~60 terms: false positive prevention (assistant, cocktail, reputation, computer, etc.) |
| `reserved.txt` | ~29 names: admin, support, root, moderator, etc. |

## Integration Examples

### Username Registration

```python
from mojo.helpers import content_guard

def validate_username(username):
    result = content_guard.check_username(username)
    if result.decision == "block":
        return False, result.reasons
    return True, []
```

### Comment Moderation

```python
from mojo.helpers import content_guard

def moderate_comment(text):
    result = content_guard.check_text(text, surface="comment")
    if result.decision == "block":
        return "rejected"
    elif result.decision == "warn":
        # queue for human review
        return "pending_review"
    return "approved"
```

### REST Endpoint with Custom Policy

```python
import mojo.decorators as md
from mojo.helpers import content_guard

@md.POST('content/check')
@md.requires_auth()
def on_content_check(request):
    return content_guard.on_rest_request(request)
```

### Custom Rules Per Organization

```python
from mojo.helpers.content_guard import load_rules, check_username

# org-specific deny list
org_rules = load_rules(
    extra_deny={"competitor_name", "internal_codename"},
    extra_reserved={"orgadmin", "orgbot"},
)

result = check_username("competitor_name", org_rules)
```

## File Structure

```
mojo/helpers/content_guard/
    __init__.py          # public API exports
    core.py              # check_username, check_text, suggest_username, on_rest_request
    normalize.py         # normalization functions and variant generation
    rules.py             # rule loading and pattern compilation
    data/
        deny.txt         # deny terms
        high_severity.txt # high-severity terms
        safe.txt         # safelist (false positive prevention)
        reserved.txt     # reserved usernames
    README.md            # package-level docs
```
