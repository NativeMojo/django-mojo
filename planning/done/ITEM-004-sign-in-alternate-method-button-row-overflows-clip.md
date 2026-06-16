---
id: ITEM-004
type: bug
title: Sign In — alternate-method button row overflows / clips when all methods are enabled
priority: P2
effort:                # XS | S | M | L | XL
owner:                 # team or person
opened: 2026-06-16
depends_on: []
related: []
links: []
---

# Sign In — alternate-method button row overflows / clips when all methods are enabled

## What & Why
On the Sign In page, the "or continue with" button row (`.mat-oauth-row`) lays the
alternate login methods out as a single non-wrapping flex row of equal-width
buttons. When all four are enabled — **SMS ("Sign in with a code") + Passkey +
Google + Apple** — they cannot fit the right-hand panel at desktop widths
(≥768px): the long "Sign in with a code" label wraps onto 3 lines while the
icon+word buttons stay one line, the row overflows its container, and the last
button (Apple) is clipped at the right edge (see attached screenshot).

This is the realistic worst case for any tenant that turns on the full set of
login methods, so the default layout must hold up when all buttons are present.

## Acceptance Criteria
- [ ] With `login_methods` = sms + passkey + google + apple (+ password), the
      Sign In oauth-method row fits inside `.mat-card` / `.mat-main` at all
      widths ≥768px — no horizontal overflow, no button clipped off the right edge.
- [ ] Buttons no longer wrap unevenly: the row reads as a tidy grid (wrap to a
      second line and/or shorten the long label) rather than one tall button next
      to three short ones.
- [ ] The existing `max-width:767px` column-stack behavior still works.
- [ ] Renders correctly under both light and dark themes.
- [ ] `register.html`'s `.mat-oauth-row` (Google + Apple) is unaffected/also fine,
      since it shares the same class.

## Repro — bugs only
1. Configure the account app so `login_methods` includes `sms`, `passkey`,
   `google`, and `apple` (and `password`).
2. Open the Sign In page at a desktop viewport width (≥768px; screenshot is ~1017px).
- Expected: the four alternate-method buttons fit the panel in a clean layout.
- Actual: "Sign in with a code" wraps to 3 lines, the row overflows the panel,
  and the Apple button is clipped at the right edge.

## Plan

### Goal
Make `.mat-oauth-row` reflow as a responsive auto-fit grid so the alternate-login
buttons never overflow or clip at any viewport width or visible-button count.

### Context — what exists
This is a **CSS-only** fix in one stylesheet; the markup is correct and unchanged.
`.mat-oauth-row` is used in **four** places, all styled by the same class, so a
single CSS change covers every case:

1. `mojo/apps/account/templates/account/login.html:34-42` — Sign In primary view.
   Contains the SMS button (`btn-go-sms`, label "Sign in with a code") + the
   `{% include "account/_login_method_buttons.html" %}`. **This is the worst case
   (up to 4 buttons) shown in the bug screenshot.**
2. `mojo/apps/account/templates/account/login.html:163-165` — SMS-code view; the
   include only (Passkey/Google/Apple, no SMS button).
3. `mojo/apps/account/templates/account/register.html:109` — Google + Apple buttons
   **inlined** (does NOT use the include; no Passkey, no SMS).
4. The buttons partial:
   `mojo/apps/account/templates/account/_login_method_buttons.html` — Passkey
   (`btn-passkey`, `style="display:none"`, revealed by JS when WebAuthn is
   supported), Google (`btn-google`), Apple (`btn-apple`); each is
   `<button class="mat-btn mat-btn-outline">` with an 18px SVG + a
   `<span class="mat-btn-text">` label.

Layout CSS — `mojo/apps/account/static/account/mojo-auth-theme.css` (verified
line numbers and exact rule bodies):

- `.mat-oauth-row` **(lines 502-505)**:
  ```css
  .mat-oauth-row {
      display: flex;
      gap: 0.75rem;
  }
  ```
- `.mat-oauth-row .mat-btn` **(lines 506-508)**:
  ```css
  .mat-oauth-row .mat-btn {
      flex: 1;
  }
  ```
- `.mat-btn` base **(lines 414-438)** already sets `width: 100%;`,
  `padding: 0.8rem 1.25rem;`, `font-size: 0.875rem;`, `display: inline-flex;`.
- `.mat-btn-text` has **no dedicated CSS rule** (label inherits from `.mat-btn`).
- The only responsive rule is `@media (max-width: 767px)` (block starts line 686);
  inside it **(lines 702-704)**:
  ```css
  .mat-oauth-row {
      flex-direction: column;
  }
  ```
  This is what produces the mobile single-column stack. **Note:** `flex-direction`
  has no effect on a grid container, so once the base rule becomes a grid this
  mobile override must be rewritten to a grid equivalent (see Changes) or the
  mobile stack silently breaks.
- `.mat-card` `max-width: 960px` (lines 60-71); `.mat-main` is the right-hand panel
  with `padding: 3rem 3rem 2.5rem` at ≥768px. No container imposes a width that
  prevents the grid solution.

### Root cause (confidence: high)
`.mat-oauth-row` is a non-wrapping flex row whose children are forced to `flex: 1`
(equal widths). With 4 buttons — one carrying the long "Sign in with a code" label
— the equal share is narrower than that label needs, so the label wraps to ~3
lines, the row's total intrinsic width exceeds the panel, and because there is no
`flex-wrap` and no desktop fallback above 767px, the row overflows and the last
button (Apple) is clipped at the right edge.

### Changes — what to do
All three edits are in `mojo/apps/account/static/account/mojo-auth-theme.css`.
No template, JS, or Python changes.

1. **Base `.mat-oauth-row` (lines 502-505)** — switch flex → responsive auto-fit grid:
   ```css
   .mat-oauth-row {
       display: grid;
       grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
       gap: 0.75rem;
   }
   ```
   `auto-fit` packs as many equal columns as fit; `minmax(150px, 1fr)` guarantees
   each cell is wide enough for an icon + label and never narrower than 150px, so
   nothing overflows or clips at any width. For 4 buttons in the right panel this
   yields a tidy 2×2 (or 3+1 / 4-across on very wide panels — all non-clipping).

2. **`.mat-oauth-row .mat-btn` (lines 506-508)** — drop the flex-only `flex: 1`
   (inert and misleading on grid items) and add `min-width: 0` so a long label
   wraps inside its cell instead of forcing the column wider:
   ```css
   .mat-oauth-row .mat-btn {
       min-width: 0;
   }
   ```
   (`.mat-btn` base already provides `width: 100%`, which fills the grid cell.)

3. **Mobile override inside `@media (max-width: 767px)` (lines 702-704)** — replace
   the now-inert `flex-direction: column` with the grid equivalent so the mobile
   single-column stack (AC #3) is preserved:
   ```css
   .mat-oauth-row {
       grid-template-columns: 1fr;
   }
   ```

### Design decisions
- **Auto-fit grid over flex-wrap** — grid adapts the column count to available
  width and keeps every button in a row equal height/width with one declaration,
  and (unlike `flex: 1` on flex) never sizes a cell below its `minmax` floor, so it
  cannot clip. Flex-wrap with a fixed `flex-basis: calc(50% - gap)` would also
  work but locks the count at 2 and is a noisier change. *(Recommended default;
  user did not select an alternative when asked.)*
- **Keep the full "Sign in with a code" label** — the 150px `minmax` floor gives it
  a wide-enough cell; if it wraps to 2 lines, grid keeps the buttons in that row at
  equal height, so it still reads tidily. No user-facing copy change → lowest risk.
  *(Recommended default; the alternative — shortening to e.g. "Use a code" in
  `login.html` — was offered and not selected.)*
- **`150px` minmax floor** is tunable; it comfortably fits "Google"/"Apple"/
  "Passkey" on one line and gives the SMS label room. If during build the 4-button
  panel reads better as a forced 2-column grid, bumping the floor (e.g. ~170-200px)
  achieves that without structural change — verify visually.

### Edge cases & risks
- **Variable button count (2/3/4):** Passkey is hidden until JS reveals it, and
  register/SMS-code views show fewer buttons. auto-fit reflows gracefully for every
  count — verify 2, 3, and 4 visible buttons all read well.
- **`register.html` (Google + Apple only):** shares the class; with 2 buttons it
  renders as a clean 2-up (or stacked on mobile). Verify unaffected.
- **Mobile (<768px):** change #3 preserves the single-column stack; without it the
  stack would silently revert to multi-column. This is the highest-risk omission —
  do not skip change #3.
- **Dark theme:** layout-only change using existing variables; no theming impact,
  but confirm both themes visually.

### Tests
- **No automated test.** This is a CSS layout change with no JS/Python behavior; it
  cannot be meaningfully asserted in testit (testit covers backend behavior, not
  rendered CSS layout). The normal "bug ⇒ regression test" expectation does not
  apply here — record this explicitly so `/build` does not fabricate a meaningless
  test.
- **Build-baseline still applies:** run `bin/run_tests --agent` before and after;
  since no Python changes, the suite must remain green (no new failures).
- **Manual/visual verification (the real coverage)** — render the Sign In page with
  `login_methods` = sms + passkey + google + apple (+ password) and confirm:
  - At ≥768px (≈1017px as in the screenshot): all 4 buttons fit inside
    `.mat-card`/`.mat-main`, no horizontal overflow, Apple not clipped (AC #1, #2).
  - At <768px: single-column stack still works (AC #3).
  - Both light and dark themes render correctly (AC #4).
  - `register.html` (Google + Apple) still looks right (AC #5).
  - Spot-check 2- and 3-button counts (Passkey hidden vs shown).

### Docs
- **None.** No doc file references `.mat-oauth-row` or the auth-page button layout
  (confirmed by search of `docs/` and `mojo/apps/account/docs/`). No
  developer-facing behavior/API/appearance contract changes.
- Update `CHANGELOG.md` only if the project records UI fixes there (builder's call).

### Open questions
- None. (Layout mechanism and label handling resolved to the recommended defaults
  above; both are visually tunable during build without changing the structure.)

## Notes
- Repo scoping: this is a **django-mojo** bug. web-mojo only provides the
  `MojoAuth` JS client these buttons call (`startGoogleLogin`, `startAppleLogin`,
  `loginWithPasskeyDiscoverable`, etc.) — no web-mojo change is involved.
- **Build baseline (2026-06-16, `bin/run_tests --agent`):** total 2248, passed 2189,
  failed 3, skipped 56. The 3 failures are all in
  `tests/test_assistant/5_test_web_tools.py` (`fetch_public_url`,
  `fetch_returns_title`, `fetch_json_content`) — external-network flakiness, HTTP
  503 from httpbin.org. Unrelated to this CSS-only change. Accepted as the
  pre-existing set; end state must add no NEW failures beyond these 3.

## Resolution
- closed: 2026-06-16
- branch: main
- files changed: CHANGELOG.md,docs/django_developer/account/api_keys.md,docs/web_developer/account/api_keys.md,mojo/apps/account/models/api_key.py,mojo/apps/account/rest/api_key.py,mojo/apps/account/static/account/mojo-auth-theme.css,tests/test_assistant/5_test_web_tools.py,tests/test_user_mgmt/api_keys.py
- tests added: None — CSS layout fix, not assertable in testit. Coverage is visual
  verification via a static harness linking the real stylesheet: confirmed a tidy
  2×2 grid at ~1017px (no overflow, no clip, "Sign in with a code" on one line) and
  the single-column stack at 375px. Full default suite green afterward (2576 total,
  2195 passed, 0 failed). Note: a separate commit fixed pre-existing flaky
  httpbin.org tests in tests/test_assistant/5_test_web_tools.py.
