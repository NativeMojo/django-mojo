---
name: sites-verify
description: >-
  Actually look at a maestro site you just deployed — real pixels from local
  headless Chrome, a real narrow viewport from the browser pane — and know
  which cheap substitutes lie (DOM state, headless "mobile", a stale pane,
  a blank cross-origin tile).
user-invocable: true
argument-hint: <site url or slug (omit to use the site you just deployed)>
maestro-skill-version: 2
---

# Sites Verify — Look At It Before You Say It Works

`deploy_site` returns byte counts, not pixels. A gallery of blank tiles once
shipped reported as verified off a deploy response; the owner opened it and
said *site looks horrible.*

> **loaded ≠ painted ≠ looks right.**

## The DOM Is Not Evidence

`img.complete === true` and `img.naturalWidth === 1280` prove the bytes
decoded, not that anything painted — a page can report every asset loaded and
render as a column of empty rectangles.

So: **never report a visual feature verified from DOM state.** Query the DOM to
*diagnose* (the cross-origin test below), never to *conclude*. Only an image of
the page in your context closes a visual claim.

## Desktop Truth — Local Headless Chrome

Real pixels, and it composites cross-origin media correctly:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless=new --disable-gpu --hide-scrollbars \
  --virtual-time-budget=10000 --window-size=1440,1000 \
  --screenshot=/tmp/site.png https://<slug>.sitesmojo.com/
```

(On Linux the binary is `google-chrome` or `chromium`.)

Then **`Read` the PNG** — running the command is not verification until the
image is in your context. Read it and say what you see.

- `--virtual-time-budget=10000` lets fonts, lazy images and entry animations
  settle; without it you screenshot a half-built page.
- One file per page — screenshot every page you changed, not just the home one.
- `--hide-scrollbars` keeps a scrollbar gutter from reading as a layout seam.

## Narrow Viewport — NOT Headless

**Headless Chrome cannot emulate mobile.** Both `--headless` and
`--headless=new` lay the page out at desktop width and then **crop** the PNG to
`--window-size`: a 375px screenshot is the left 375 pixels of a 1440px render.
Nothing reflows, no media query fires, and every wide element looks like it
overflows — inventing bugs that do not exist. One "mobile overflow" was chased
through a stylesheet before a real browser reported
`scrollWidth === clientWidth === 375` and not one offending element.

For narrow viewports use the browser pane:

1. `resize_window {preset: "mobile"}` (375×812) — a real reflow.
2. Confirm there is no horizontal overflow:

   ```js
   [document.documentElement.scrollWidth, document.documentElement.clientWidth]
   ```

   Equal means no overflow. That is the whole test.
3. If they differ, find the culprits rather than guessing:

   ```js
   [...document.querySelectorAll('*')]
     .filter(e => e.getBoundingClientRect().right > window.innerWidth + 1)
     .map(e => e.tagName + '.' + e.className).slice(0, 20)
   ```

Then look at the pane: text and layout render faithfully there, media does not.

## A Blank Tile — Cross-Origin, Or Actually Broken?

Site media serves from the platform's asset host (an S3 URL), **outside the
deployed bundle by design**, which is what makes a media URL survive redeploys
and rollbacks. It is also why a screenshot taken *inside* a browser pane can
show a fine image as an empty rectangle: the pane cannot composite cross-origin
pixels into its capture.

Do not guess. Settle it in the page:

```js
const img = document.querySelector('img');           // the blank one
const c = document.createElement('canvas');
c.width = c.height = 8;
const ctx = c.getContext('2d');
ctx.drawImage(img, 0, 0, 8, 8);
try { ctx.getImageData(0, 0, 1, 1); return 'readable (same-origin or CORS)'; }
catch (e) { return 'TAINTED: ' + e.name; }            // SecurityError
```

A `SecurityError` taint means real cross-origin pixels decoded into the canvas:
**the image is there and the screenshot is what is broken.** Confirm with
headless Chrome, which composites it correctly.

Readable canvas and still blank, or `naturalWidth === 0` — the media is
genuinely missing. Check the URL, the deploy, and that the asset was uploaded
to the right workspace.

## The Pane Goes Stale

After JS-driven scrolls the browser pane returns blank frames, or the
*previous* frame. The tell is the error string:

> The Browser pane is currently hidden.

A dead surface, not a finding about the site. Call `preview_start` again for a
fresh tab and redo the check. Never read a stale-pane screenshot as evidence —
least of all "the section below the fold is empty".

## Build It So It Can Be Seen

Verification failures are often authored in. The big one:

**Reveal-on-scroll defaults to invisible.** An IntersectionObserver that sets
`opacity: 0` on everything up front means one missed callback renders a blank
page — and it will miss: in a screenshot tool, on a slow connection, with
reduced motion, in any browser that scrolled past before the observer attached.

- Only hide what is genuinely below the fold; above-the-fold content is never
  opacity-zero.
- Always ship the failsafe:

  ```js
  setTimeout(() => document.querySelectorAll('.reveal')
    .forEach(el => el.classList.add('visible')), 2000);
  ```

- Never make an image's visibility depend on a JS callback that can be skipped.

## What To Actually Check

Once you can see the page:

- **The fold** — headline legible, nav present, no half-loaded hero.
- **Media** — every image present, correctly cropped, not stretched or squashed
  by a missing `object-fit`.
- **Text over imagery** — contrast survives a busy photo; where "looks fine in
  the design" fails in the render.
- **The narrow viewport** — reflow, no horizontal scroll, tap targets not
  overlapping.
- **Each changed page**, not just the one you remember.

## Reporting

Say what you verified and how: "Screenshotted the home and gallery pages at
1440×1000 in headless Chrome and read both; all six gallery tiles render;
checked 375px in the browser pane, `scrollWidth === clientWidth`, no overflow."

Name what you could not see — "the video poster did not load in the capture, I
could not confirm it" beats silence — and name any skipped check as skipped.

Never write "should look good", "the site is live and looking great", or any
sentence whose evidence is a deploy response.

## Forbidden

- Reporting a visual feature verified from DOM state (`complete`,
  `naturalWidth`, computed styles) — those prove loading, never painting.
- Trusting a headless-Chrome screenshot as a mobile rendering. It is a desktop
  layout cropped to width, and the "bugs" it shows are fabrications.
- Claiming a site looks right without having put an image of it in your context.
- Reading a stale pane ("The Browser pane is currently hidden") as a finding
  about the page.
- Calling a blank tile a broken site before running the canvas-taint test —
  cross-origin media is the expected design, not a defect.

