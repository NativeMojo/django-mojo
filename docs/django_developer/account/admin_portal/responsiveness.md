# Admin portal responsiveness

Every Admin control must answer the click that started it. If the browser does
real work for a second and the screen does not change, the button reads as
broken; if a request is rejected and the previous screen stays put, the operator
believes it worked.

The shared affordances live in
`assets/components/actions.js`, are declared in the foundation `manifest.json`,
and are enforced by `tests/test_account/test_admin_portal_responsiveness.py`.

Every portal surface has been swept, and the guard now walks the whole asset
tree rather than a list of files somebody has to extend — so this page describes
what the code does today, not a target. A new feature file is covered the moment
it is written.

## The placement rule — read this before choosing a target

**Before putting a pending state on element `E` for handler `H`, ask: does `H`
(or the `reload()` it calls) `replaceChildren` on an ancestor of `E`?**

If it does, `E` is destroyed mid-flight. The pending state paints into a
detached node — invisible from the first frame — and the `finally` restore
writes to something no longer in the document. The affordance moves to the
nearest surviving ancestor, never onto the doomed node.

Three shared components already sit on that trap, and each solves it
differently:

| Component | Why the obvious target is doomed | Where the affordance goes |
|---|---|---|
| `components/model.js` `actionMenu` | the handler sets `menu.hidden = true` **before** awaiting `action.run` | the `•••` trigger |
| `components/rows.js` `statusHeadline` | `onRefresh` repaints the whole block the `↻` button lives in | the document-level live region (`announce()`); the button itself is restored only if it survived |
| `components/views.js` `sectionTabs` | callers rebuild the nav inside their own `paint()` | the clicked tab, deliberately **not** held across a repaint — the new nav re-derives it |

## The policy

| Case | Affordance |
|---|---|
| Scoped action, result lands near the button | Inline pending: `aria-disabled="true"`, pending label, spinner, announced through `role="status"` |
| Action invalidating the whole view, or one that must not be interrupted | Busy scrim (`openBusy`) |
| First paint of a page, panel, tab or section | Skeleton painted into the target before the first await — **only if the branch actually awaits** |
| Any failure of a load or a panel-scoped action | In-panel `errorState(error, retry)` — never stale content |
| Settles under 150 ms | Nothing paints — the skeleton included |
| A pending state that did paint | Held ≥250 ms so it cannot strobe |
| Re-entrant click while in flight | Ignored; the in-flight promise is returned. The guard is registered **synchronously, before the first await** |
| Trigger inside the subtree the action replaces | See the placement rule above |
| The await is for **human input** (confirm dialog, form modal) | **No affordance at all** until the human answers |
| `fresh_auth_required` (HTTP 440) | **Restore the control, render nothing** |
| `AbortError` from the wrapper's own signal | Swallowed — by signal identity, never by error name |

### Why `aria-disabled`, not `disabled`

A focused element that becomes `disabled` is blurred by the browser: focus falls
to `<body>` and Tab restarts from the top of the document — a WCAG 2.4.3
regression, made worse by the 150 ms delay, because it would happen
asynchronously in the middle of an interaction. The re-entry guard already
prevents the second submit, so `aria-disabled` costs nothing.

### Why `role="status"`, not `aria-busy`

`aria-busy="true"` marks a region unstable and *suppresses* announcements while
it is set; it is not itself announced on a non-live element. The announcement
channel is a polite `role="status"` region. `actions.js` keeps exactly one, on
`<body>`, deliberately outside any subtree a handler might replace.

### Why a 440 restores and says nothing

`core.js` already handles HTTP 440: it calls `clearBusy()`, and if `freshRetry`
is set it awaits `requestFreshAuth` then retries internally. By the time
`FreshAuthRequired` reaches a caller the prompt has already happened and the
retry has already failed. Holding the pending state would disable the control
forever — a fresh instance of the bug this policy exists to kill.

Two consequences for anything wrapping a request:

- `clearBusy()` clears **all** scrims, so a wrapper must tolerate its own scrim
  having been destroyed underneath it, and must only ever call `busy.close()`.
- The error is reported by the shared client, not by the caller. Restore and
  render nothing. (`features/settings/panels.js` `saveHandler` is the older
  hand-rolled version of the same rule.)

## The API

```js
import {runAction, copyButton, loadInto} from '../../components/actions.js';
```

### `runAction(target, task, options)`

Runs `task` with the pending affordance attached to `target`, and returns the
task's promise. A second call for the same `target` while the first is in flight
returns the first promise and starts nothing.

`target` may be `null` for a headless action: the guard, the busy scrim and the
error handling all still apply, and no DOM write is attempted.

| Option | Meaning |
|---|---|
| `busy` | a title string, or the `openBusy` options object, for an action that must not be interrupted |
| `pendingLabel` | swaps the control's own label while pending (`'Saving…'`) |
| `announceLabel` | what a screen reader hears when the control keeps its label — a tab, an icon-only button |
| `onError` | called with the error; use it to paint the failure in-panel. Takes precedence over `failure` |
| `success` | the confirmation toast — a string, or a function of the task's result. Raised after the pending state clears |
| `failure` | default `true`; toasts the error when no `onError` is given. Set `false` to rethrow to the caller |
| `restoreOnSuccess` | default `true`; set `false` when success destroys or navigates away from the control |
| `signal` | the caller's `AbortController` signal, used to skip the pending paint on a superseded render |

`runAction` does **not** rethrow by default. An error goes to `onError` if one
is given, otherwise to a danger toast; an `AbortError` is always swallowed,
whoever owns the signal, because a superseded request is not a failure. Pass
`failure: false` when the caller genuinely wants the rejection.

A success message is raised from the `finally` block, after the pending state
has cleared, and a formatter that throws degrades to a plain "Done." — a broken
message must never report a completed action as broken.


## Saying that it finished

A pending state can only ever say *working*. Until `toast()` existed the portal
had no way to say *done*, so an action that succeeded left the screen silent —
which reads exactly like a button that did nothing, and was the most common
complaint about this admin.

```js
import {toast} from '../../components/actions.js';

toast('Password reset link sent.');                    // success (default)
toast('That address is already attached.', {tone: 'danger'});
toast('Re-checking the fleet…', {tone: 'info'});
```

Toasts stack bottom-right, dismiss after ~4 seconds, pause while the pointer is
over them, and carry a close button. They are announced through the same live
region as the pending state, so the confirmation is not purely visual.

Prefer `runAction`'s `success` option over calling `toast` by hand — it fires
only when the task actually resolved, and only after the control has settled:

```js
runAction(button, () => post(`/api/user/${user.id}`, {send_invite: {}}),
  {success: 'Invite sent.'});
```

An action-menu entry says it declaratively — `actionMenu` passes `done` through
as `success`:

```js
{label: 'Resend invite', capability: manage, done: 'Invite sent.', run: …}
```

```js
// Scoped action, result lands next to the button.
save.addEventListener('click', () => runAction(save, async () => {
  await api('/api/thing', {method: 'POST', body: JSON.stringify(payload)});
  await reload();
}, {pendingLabel: 'Saving…', onError: (error) => { message.textContent = error.message; }}));

// Action that invalidates the whole view.
runAction(button, () => applyEverything(), {busy: 'Applying changes…'});

// Icon-only control: spinner and announcement, no label swap.
runAction(refresh, () => load(true), {announceLabel: 'Refreshing…'});
```

### `copyButton(text, options)`

The one clipboard affordance. `text` is a string or a function returning one —
pass a function for a value that is scrubbed when a modal closes. A non-secure
context rejects `navigator.clipboard.writeText`; every bare call in the tree
today swallows that silently, and this one says so on the button.

```js
copyButton(() => secret, {label: 'Copy secret', copiedLabel: 'Copied'});
```

### `loadInto(target, loader, options)`

First paint of a panel, tab or section. Paints `loadingState(message)` into
`target` after the 150 ms delay, awaits `loader(current)`, and on rejection
paints `errorState(error, retry)` in place — never stale content. The loader
receives a `current()` predicate: two fast tab clicks are two different buttons,
so the re-entry guard does not cover them and the loser must not paint over the
winner.

```js
async function section(id) {
  // A synchronous branch is NOT wrapped — there is no await to cover, and the
  // wrapper would only ever be a flash.
  if (id === 'setup') { body.replaceChildren(setupPanel(...)); return; }
  await loadInto(body, (current) => manageSection(ctx, app, summary, id, body, reload, current),
    {message: 'Loading…', retry: () => section(id)});
}
```

## The banned-pattern rule

A raw async handler hands a promise to a listener nobody awaits: the click
returns, the work runs on, and nothing on screen moves. These spellings are
banned on swept surfaces:

```
onclick: async        onchange: async        onsubmit: async
addEventListener('click'|'change'|'submit'|'keydown'|'input'|'paste'|'pointerdown'|'dblclick', async
```

Route the work through `runAction` instead. Where an await genuinely must not
carry an affordance, mark the line:

```js
// responsiveness-exempt: the await here is confirmAction — a human answering a
// dialog. No affordance paints until they have answered.
button.addEventListener('click', async () => {
```

`test_no_raw_async_handlers_contract` honours an exemption comment within three
lines above the handler, and caps the total number of exemptions. There are
three, and the cap is three:

- `components/model.js` `lifecycleControl` — the await is a confirm dialog.
- `core.js` `FormView` — it already guards correctly. Its disable is
  **synchronous** (routing it through `runAction` would defer it 150 ms) and it
  deliberately stays disabled on success, because success normally closes the
  modal and re-enabling would re-open a double-submit window. It gains the
  announcement and nothing else.
- `features/advanced/page.js` `recordEditor` — its first await is the
  replace-the-record-set confirm. The provider write that follows runs behind
  the busy scrim.

### The swept set is the tree

The whole portal is swept, so there is no list to keep in step: the guard walks
`assets/**/*.js` and applies to every module it finds — including one added
tomorrow. The walk is **filtered to `.js`** on purpose. The same tree carries
`mojo-logo.png`, several `styles.css` files and eight feature `manifest.json`
files, and `read_text()` on the PNG raises `UnicodeDecodeError`: an unfiltered
walk does not fail the test, it crashes it.

Four assertions run over that walk:

| Test | What it holds |
|---|---|
| `test_no_raw_async_handlers_contract` | the banned spellings, the exemption comment, the cap. Also asserts the walk found the tree at all — a glob matching nothing would make every other assertion vacuous |
| `test_pending_states_are_never_pinned_to_a_closed_container_contract` | the placement rule: no `runAction` paints on `event.currentTarget`, or on the very container, within three lines of a `.close()` / `.hidden = true` |
| `test_local_action_wrappers_are_retired_contract` | one action wrapper in the portal, no local `actionError`, and every module calling `runAction` / `loadInto` / `copyButton` actually imports it |
| `test_guard_keys_are_unique_contract` | the namespace rule: a bare literal `key:` is used by exactly one `runAction` call site tree-wide, and no tab nav guards its tabs on one shared literal |

There is exactly one `runAction`, in `components/actions.js`. The two local
wrappers the sweep started from are gone: `features/platform/page.js` had its
own `runAction(title, detail, task)` and `features/advanced/page.js` had an
`actionError(panel, error, retry)` that appended a bare `.error-state` div.
Platform's Setup page still owns its **busy scrim** — `drive()` reports progress
through `busy.update()`, so the handle has to stay where the task can reach it —
but the guard, the error capture and the 440 rule all come from the shared
helper now.

## Choosing between inline and the scrim, in practice

The swept feature files settled into three recognisable shapes. Reach for the
matching one rather than re-deriving it:

| Shape | Example | What it gets |
|---|---|---|
| Click → request → result lands beside the button | `addAddressDialog`'s Check, the wizard's Create app | `runAction(button, task, {pendingLabel})`, plus `restoreOnSuccess: false` when success closes the modal |
| Confirm dialog → destructive or fleet-wide work | delete app, roll back, retire a certificate, delete a DNS record set | **no affordance while the dialog is open**, then `runAction(null, task, {key, busy})` — the trigger is inside the table `reload()` rebuilds, so there is nothing to pin to |
| Trigger closes its own container, then awaits | the framework drill-in's Update / Hold / Resume | `runAction(null, …)` with a scrim, never `event.currentTarget` — the button is detached from the first frame |

Two details worth copying:

- **Pass an explicit `key` for a headless action.** `runAction(null, …)` with no
  `key` mints a fresh symbol, so two clicks are two runs. A stable string —
  `` `webapps:rollback:${app.id}:${release.id}` `` — is what makes the guard
  real. A DOM node works as a key too, and is the right one for a per-row
  trigger.
- **A panel's skeleton goes into a body node, not the panel.** `loadInto(panel, …)`
  would replace the panel heading and the Refresh button sitting beside it. The
  Domains, Credentials, Upstreams and Vhosts panels each append a plain
  `<div>` and load into that; so do the People list and the Group inspector's
  Members and API Keys tabs.

### The guard key is a portal-wide namespace

`INFLIGHT` is **one process-global `Map`**. A key is not scoped to the module
that wrote it, to the page, or to the record — two modules that both pass
`'framework-update'` are guarding each other, and one of them silently returns
the other's promise and never runs. Spell every string key:

```
<feature>:<action>[:<id>…]
```

`<feature>` is the feature directory (`webapps`, `settings`, `platform`,
`advanced`, `people`, `activity`), `<action>` is the verb, and every `<id>`
needed to make **one key mean exactly one action** follows. Two rules:

- **Include every discriminator the action varies on.** A rollback varies on
  the app *and the release*; a pin write varies on the value being written; an
  owner-settings save varies on the payload, not on its field names. If two
  clicks would legitimately do different work, they must not share a key —
  the second one appears to succeed and never runs.
- **A bare literal key must be unique across the whole asset tree**, and
  `test_guard_keys_are_unique_contract` enforces it. Interpolated keys are
  exempt from uniqueness: their whole point is one prefix over many ids.

Keys predating this convention are spelled `<noun>-<action>:<id>`
(`certificate-retire:${row.id}`). They are unique and were left alone; the
convention above governs new and changed keys, and the uniqueness test covers
every key regardless of spelling.

### When NOT to reach for the guard

`runAction` always guards, and there is one shape where guarding is the wrong
answer: a control whose work is already **superseded** rather than queued.
`features/platform/metrics.js` is the case — every dropdown and checkbox calls
`loadSeries()`, which aborts the previous request through its own
`AbortController` and drops a stale answer. Wrapping those in `runAction` would
make the second change return the first's promise, so the chart would keep the
old selection while the dropdown showed the new one: the control and the screen
disagreeing, which is the bug this policy exists to remove. They stay unwrapped,
and the affordance is the skeleton `loadSeries` paints into `chartSlot` — a node
no repaint there destroys, carrying `role="status"` so it is announced.

The rule of thumb: **guard what queues, do not guard what supersedes.**

**A tab nav supersedes.** `features/people/page.js`'s Users/Groups nav is the
second case, and it is the one that shows what the mistake costs. Both tabs
called `runAction(null, () => render(), {key: 'people-view'})` — one key for two
different actions. `active` and the URL are written synchronously outside the
guard, so clicking Users and then Groups before the first list landed set both
to *groups*, then handed back the *Users* render's promise: `render()` never ran
for Groups, and the operator was left on a users table under a URL that said
groups, with nothing repainting. Keying per tab would only push the same failure
out to the third click. The nav calls `render()` directly now. Nothing is lost:
`render()` takes `mine = ++generation` and drops a superseded paint, and each
pass builds a fresh `listBody`, so `loadInto`'s generation token cannot
cross-write either.

`components/views.js` `sectionTabs` — every other tab nav in the portal — was
never exposed: it guards on `event.currentTarget`, so each tab button is already
its own key.

### A single-use credential outlives the restore

`runAction` restores the control on the error path — correct almost everywhere,
and wrong for a control holding something the failure consumed. The Domains
purchase button (`features/advanced/page.js` `renderConfirm`) spends a
single-use quote token on one non-retried attempt. A refusal used to leave
"Register domain" live directly beneath the message saying the quote was spent;
the server refuses the retry (a `select_for_update` CAS on the quote's status
plus a confirm-token match), so the only thing a second click buys is a second,
more confusing refusal.

Such a control **latches off inside the task**, synchronously, before the first
await — so the guard and the latch agree — and stays latched:

```js
let spent = false;
const buy = h('button', {class: 'button danger', disabled: true, onclick: () => runAction(buy, async () => {
  spent = true; buy.disabled = true;   // synchronous: no await above this line
  …
}, {busy: {…}, restoreOnSuccess: false, onError: …})}, 'Register domain');
// validate() re-derives `disabled` on every keystroke, so it must honour the latch.
const validate = () => { buy.disabled = spent || …; };
```

`disabled`, not `aria-disabled`, is deliberate here and is the one exception to
the rule above it: the control is meant to leave the tab order, the busy scrim
is managing focus anyway, and the way back is a new quote, not this button.
Taking a fresh quote rebuilds the confirm step, and with it an unlatched button.

## Styling

Pending styles live in `assets/admin.css` next to the busy scrim and reuse the
existing `busy-spin` keyframe — do not add a second one. `.pending-spinner` and
`.is-pending-icon` are both listed in the `prefers-reduced-motion` rule.
