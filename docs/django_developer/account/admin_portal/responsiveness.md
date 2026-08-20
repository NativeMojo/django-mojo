# Admin portal responsiveness

Every Admin control must answer the click that started it. If the browser does
real work for a second and the screen does not change, the button reads as
broken; if a request is rejected and the previous screen stays put, the operator
believes it worked.

The shared affordances live in
`assets/components/actions.js`, are declared in the foundation `manifest.json`,
and are enforced by `tests/test_account/test_admin_portal_responsiveness.py`.

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
| `onError` | called with the error instead of rethrowing; use it to paint in-panel |
| `restoreOnSuccess` | default `true`; set `false` when success destroys or navigates away from the control |
| `signal` | the caller's `AbortController` signal. An `AbortError` is swallowed only when **this** signal aborted |

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

`test_no_raw_async_handlers_contract` walks the swept set, honours an exemption
comment within three lines above the handler, and caps the total number of
exemptions. There are three today, and the cap is three:

- `components/model.js` `lifecycleControl` — the await is a confirm dialog.
- `core.js` `FormView` — it already guards correctly. Its disable is
  **synchronous** (routing it through `runAction` would defer it 150 ms) and it
  deliberately stays disabled on success, because success normally closes the
  modal and re-enabling would re-open a double-submit window. It gains the
  announcement and nothing else.
- `features/advanced/page.js` `recordEditor` — its first await is the
  replace-the-record-set confirm. The provider write that follows runs behind
  the busy scrim.

The swept set holds the shared surfaces — `components/actions.js`,
`components/model.js`, `components/rows.js`, `components/views.js`, `core.js` —
plus the high-traffic feature files: `features/webapps/page.js`,
`features/webapps/api.js`, `features/webapps/wizard.js`,
`features/advanced/page.js`. The remaining feature files join it as they are
swept.

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
  `` `webapp-rollback:${app.id}` `` — is what makes the guard real.
- **A panel's skeleton goes into a body node, not the panel.** `loadInto(panel, …)`
  would replace the panel heading and the Refresh button sitting beside it. The
  Domains, Credentials, Upstreams and Vhosts panels each append a plain
  `<div>` and load into that.

## Styling

Pending styles live in `assets/admin.css` next to the busy scrim and reuse the
existing `busy-spin` keyframe — do not add a second one. `.pending-spinner` and
`.is-pending-icon` are both listed in the `prefers-reduced-motion` rule.
