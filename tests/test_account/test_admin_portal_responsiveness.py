"""Every Admin control answers the click that started it.

A click that does real work must change the screen before the work finishes,
and a rejected request must never leave the previous screen sitting there as
though nothing happened. The shared affordances live in
``assets/components/actions.js``; this module is the contract that keeps them
attached to a node that actually survives the handler that paints them.
"""

import re
from pathlib import Path

from testit import helpers as th


ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "mojo/apps/account/admin_portal/assets"


@th.django_unit_test("the Deploys tab paints its own loading and failure states")
def test_deploys_tab_renders_its_own_states_contract(opts):
    page = (ASSETS / "features/webapps/page.js").read_text()

    # #2232: clicking Deploys fired two un-awaited reads and painted nothing
    # until they landed. The awaiting branch now owns a loading state and an
    # in-panel failure, both through the shared helper.
    assert "'../../components/actions.js'" in page, \
        "the app page does not import the shared responsiveness helpers"
    assert "loadInto(body," in page, \
        "the awaiting branch of section(id) does not route through loadInto — " \
        "a Deploys click still paints nothing while its reads are in flight"
    assert "retry: () => section(id)" in page, \
        "a failed section render has no in-panel retry"

    # The setup branch is synchronous: wrapping it would flash a skeleton on
    # every click of that tab for no await at all.
    assert "if (id === 'setup') { body.replaceChildren(setupPanel(ctx, app, summary, reload)); return; }" in page, \
        "the synchronous setup branch was wrapped in loadInto, or moved — it " \
        "must return before any loading state is scheduled"

    # paint() awaits its section, and every caller awaits paint(): action
    # handlers rely on `await reload()` meaning the view is current.
    assert "async function paint()" in page, \
        "paint() is still synchronous, so its section render is never awaited"
    assert "await section(active);" in page, \
        "paint() fires section() un-awaited — a rejection there is unhandled"
    assert "const reload = async () => { await fetchSummary(); await paint(); };" in page, \
        "reload() does not await paint(); `await reload()` no longer means the view is current"
    assert "await fetchSummary(); await paint();" in page, \
        "the initial load does not await paint()"

    # The key tab has the same shape and rides the same fix.
    assert "if (section === 'key') {" in page, \
        "the deploy-key section moved — confirm it still routes through section(id)"

    # Both empty states stay reachable.
    for copy in ("No versions have been deployed yet.", "No deploy activity yet."):
        assert copy in page, f"the Deploys tab lost its {copy!r} empty state"


def swept():
    """Every JavaScript module in the packaged portal.

    The sweep is finished, so the swept set is no longer a list somebody has to
    remember to extend — it is the tree. A new feature file is covered the day
    it is added, which is the whole point of closing the door.

    Filtered to ``*.js`` deliberately. The same tree carries ``mojo-logo.png``,
    several ``styles.css`` files and eight feature ``manifest.json`` files;
    ``read_text()`` on the PNG raises ``UnicodeDecodeError``, so an unfiltered
    walk does not fail this test, it crashes it.
    """
    return sorted(ASSETS.rglob("*.js"))


def _name(path):
    return path.relative_to(ASSETS).as_posix()


# Handler spellings that hand a promise to an event listener nobody awaits:
# the click returns, the work runs on, and the screen does not move.
BANNED = (
    "onclick: async",
    "onchange: async",
    "onsubmit: async",
) + tuple(
    f"addEventListener('{event}', async"
    for event in ("click", "change", "submit", "keydown", "input", "paste",
                  "pointerdown", "dblclick")
)

EXEMPT_MARKER = "responsiveness-exempt:"
# A deliberate exemption is rare and reasoned. If this needs raising, the
# reason belongs in the review, not in a quietly larger number. The four that
# exist are named in
# docs/django_developer/account/admin_portal/responsiveness.md.
EXEMPT_CAP = 4


def _code(text):
    """The source with whole-line // comments dropped.

    Negative assertions must be about what the module *does*; the comments
    explaining why it does not do a thing name that thing.
    """
    return "\n".join(line for line in text.splitlines()
                     if not line.strip().startswith("//"))


def _unexempted(text):
    """Banned handler lines with no exemption comment in the 3 lines above."""
    lines = text.splitlines()
    hits = []
    for index, line in enumerate(lines):
        if not any(pattern in line for pattern in BANNED):
            continue
        window = lines[max(0, index - 3):index]
        if any(EXEMPT_MARKER in above for above in window):
            continue
        hits.append((index + 1, line.strip()))
    return hits


@th.django_unit_test("the shared action helper carries the whole responsiveness policy")
def test_action_helper_contract(opts):
    from mojo.apps.account.services import admin_assets

    actions = (ASSETS / "components/actions.js").read_text()
    code = _code(actions)
    styles = (ASSETS / "admin.css").read_text()
    assets = admin_assets.load_manifest()

    # An undeclared asset is served as a 404 and nothing in Python complains,
    # so every swept surface would break silently.
    assert "assets/components/actions.js" in assets, \
        "actions.js is not a declared package asset — the browser will 404 it"

    for export in ("export function runAction", "export function copyButton",
                   "export function loadInto"):
        assert export in actions, f"actions.js does not expose {export!r}"

    assert "PENDING_DELAY = 150" in actions, \
        "the 150ms threshold below which nothing paints is gone"
    assert "PENDING_HOLD = 250" in actions, \
        "the 250ms minimum hold is gone — a pending state can strobe again"

    # The guard must be in place before control ever leaves the click handler.
    assert "if (running) return running;" in actions and "INFLIGHT.set(key, promise);" in actions, \
        "the re-entry guard does not return the in-flight promise"
    assert actions.index("INFLIGHT.set(key, promise);") > actions.index("const promise = execute("), \
        "the guard is registered by something other than the synchronous call path"

    # aria-disabled, never disabled: a disabled element is blurred by the
    # browser and Tab restarts from the top of the document.
    assert "setAttribute('aria-disabled', 'true')" in actions, \
        "the pending state does not mark the control aria-disabled"
    assert ".disabled = true" not in code, \
        "actions.js disables a control — that blurs it and breaks tab order"

    # role="status", not aria-busy: aria-busy suppresses announcements.
    assert "role: 'status'" in actions and "'aria-live': 'polite'" in actions, \
        "there is no live region for the pending announcement"
    assert "aria-busy" not in code, \
        "aria-busy suppresses announcements — it is not an announcement channel"

    # 440 restores the control and renders nothing: the shared client already
    # prompted and already retried.
    assert "error?.code === 'fresh_auth_required'" in actions, \
        "a 440 is not recognised — the control would stay disabled forever"
    assert "clearBusy" not in code, \
        "actions.js calls clearBusy(), which destroys every other scrim on the page"
    assert "scrim?.close()" in actions, \
        "the busy scrim is not closed through its own token"

    # Abort is decided by signal identity, never by the error's name alone.
    assert "signal?.aborted && error?.name === 'AbortError'" in actions, \
        "an AbortError is swallowed by name — somebody else's abort would vanish"

    # A target removed mid-flight must not be written to; focus comes back.
    assert "target.isConnected" in actions, \
        "the restore does not null-check a target removed mid-flight"
    assert "focus?.({preventScroll: true})" in actions, \
        "focus is not restored to a control that still holds the interaction"

    # A headless action takes the same path without touching the DOM.
    assert "target instanceof Element" in actions, \
        "a null target is not handled — a headless action would throw"
    assert "Symbol('headless action')" in actions, \
        "a headless action has no guard key of its own"

    # loadInto: generation token plus the same 150ms rule.
    assert "GENERATION" in actions and "GENERATION.get(target) === mine" in actions, \
        "loadInto has no generation token, so a superseded render still paints"
    assert "errorState(error, again)" in actions, \
        "a failed load does not paint an in-panel error with a retry"
    assert "loadingState(message)" in actions, \
        "loadInto never paints a loading state"
    assert "setTimeout(() => {\n    if (current() && target.isConnected)" in actions, \
        "the loading state is painted immediately instead of after the delay"

    # The pending styles reuse the existing spinner keyframe.
    assert ".pending-spinner{" in styles and "animation:busy-spin" in styles, \
        "the pending spinner does not reuse the shared busy-spin keyframe"
    assert styles.count("@keyframes busy-spin") == 1, \
        "a second spin keyframe was invented instead of reusing busy-spin"
    assert 'aria-disabled="true"' in styles, \
        "aria-disabled has no visual contract, so a pending control looks live"
    assert "@media(prefers-reduced-motion:reduce){.skeleton,.busy-spinner,.pending-spinner,.is-pending-icon{animation:none}}" in styles, \
        "the new pending animations ignore prefers-reduced-motion"


@th.django_unit_test("no portal asset hands a promise to an un-awaited listener")
def test_no_raw_async_handlers_contract(opts):
    modules = swept()
    # A glob that matched nothing would make every assertion below vacuous.
    names = {_name(path) for path in modules}
    assert len(modules) >= 25 and "core.js" in names and "components/actions.js" in names, \
        f"the asset walk found {len(modules)} JavaScript modules under {ASSETS} — " \
        f"it is not reaching the portal tree, so this guard proves nothing"

    exemptions = 0
    for path in modules:
        text = path.read_text()
        exemptions += text.count(EXEMPT_MARKER)
        hits = _unexempted(text)
        assert not hits, (
            f"{_name(path)} still attaches a raw async handler at "
            f"{', '.join(f'line {line}' for line, _ in hits)} — route the work "
            f"through runAction() from components/actions.js, or mark the line "
            f"'// {EXEMPT_MARKER} <reason>' when the await is for human input "
            f"or the control already guards itself")
    assert exemptions <= EXEMPT_CAP, \
        f"{exemptions} responsiveness exemptions across the portal assets " \
        f"(cap {EXEMPT_CAP}) — exemptions are supposed to be rare and reasoned"


@th.django_unit_test("the portal has one action wrapper, and it is the shared one")
def test_local_action_wrappers_are_retired_contract(opts):
    shared = ASSETS / "components/actions.js"
    for path in swept():
        text = path.read_text()
        code = _code(text)
        if path != shared:
            assert "function runAction" not in code, \
                f"{_name(path)} defines its own runAction — the timing, the guard " \
                f"and the 440 handling are the shared helper's job, and a second " \
                f"copy is a second set of rules nobody will keep in step"
        # advanced/page.js used to append a bare .error-state div with no
        # heading, no role and no retry, so repeated failures stacked under
        # stale content. errorState(error, retry) replaced it.
        assert "function actionError" not in code, \
            f"{_name(path)} re-introduces a local error renderer — use " \
            f"errorState(error, retry) from components/views.js"

    # Every module that reaches for a shared affordance must import it; a
    # copy-pasted call with no import is a ReferenceError on first click and
    # nothing in Python notices.
    for path in swept():
        text = path.read_text()
        if path == shared:
            continue
        for symbol in ("runAction(", "loadInto(", "copyButton("):
            if symbol not in text:
                continue
            assert re.search(rf"import \{{[^}}]*\b{symbol[:-1]}\b[^}}]*\}} from '[^']*actions\.js'", text), \
                f"{_name(path)} calls {symbol[:-1]}() without importing it from " \
                f"components/actions.js"


# A handler that hides or closes a container before it awaits has destroyed
# every node inside it. `menu.hidden = true` and `inspector.close()` are the two
# spellings in this tree.
CLOSERS = (".hidden = true", ".close()")
TARGET = re.compile(r"runAction\(\s*([A-Za-z_$][\w.$]*)")


def _doomed_pending_states(text):
    """runAction calls painting on a node the same handler just removed.

    Two ways that happens: the target IS the container that was closed, or the
    target is `event.currentTarget` — the clicked control, which lived inside
    whatever the line above closed. A named target that is not the closed
    container is fine, and is the fix the placement rule asks for: actionMenu
    hides `menu` and paints on `button`.
    """
    lines = text.splitlines()
    hits = []
    for index, line in enumerate(lines):
        closed = set()
        for closer in CLOSERS:
            if closer not in line:
                continue
            words = line.split(closer)[0].strip().split()
            closed.add(words[-1] if words else "")
        if not closed:
            continue
        for ahead in lines[index + 1:index + 4]:
            match = TARGET.search(ahead)
            if not match:
                continue
            target = match.group(1)
            if target == "event.currentTarget" or target in closed:
                hits.append((index + 2, target))
    return hits


@th.django_unit_test("no pending state is pinned to a node its own handler just removed")
def test_pending_states_are_never_pinned_to_a_closed_container_contract(opts):
    # The placement rule in its tree-wide form, and the failure mode the phase-1
    # review called the most likely one: the guard goes green, the docs say it
    # is enforced, and the portal is no more responsive than before because the
    # affordance paints into a subtree that is already gone.
    for path in swept():
        hits = _doomed_pending_states(path.read_text())
        assert not hits, (
            f"{_name(path)} pins a pending state to a node the same handler "
            f"already removed: "
            + ", ".join(f"line {line} paints on {target}" for line, target in hits)
            + " — move the affordance to a surviving ancestor, or make the call "
              "headless (runAction(null, …, {key, busy})) when nothing survives")


@th.django_unit_test("a pending state outlives the render its own action triggers")
def test_pending_state_survives_its_own_action_contract(opts):
    model = (ASSETS / "components/model.js").read_text()
    rows = (ASSETS / "components/rows.js").read_text()
    views = (ASSETS / "components/views.js").read_text()
    actions = (ASSETS / "components/actions.js").read_text()

    # --- model.js: actionMenu hides the menu BEFORE running the action, so a
    # pending state on the menu item paints inside a hidden container. It must
    # be on the ••• trigger, which survives.
    menu_item = model.split("const menu = h('div', {class: 'action-menu-list'", 1)[1] \
        .split("const button = h('button', {class: 'icon-button'", 1)[0]
    assert "setOpen(false)" in menu_item, \
        "actionMenu no longer closes its menu before running — re-check the placement, " \
        "because a pending state on a hidden menu item is invisible"
    assert "document.removeEventListener('click', closeOnOutside)" in model, \
        "the action menu's outside-click listener is no longer removed on close — " \
        "a re-rendering table would accumulate one dead document listener per row"
    assert "runAction(button," in menu_item, \
        "the action-menu pending state is not attached to the ••• trigger"
    assert "runAction(menu" not in model and "runAction(event.currentTarget" not in menu_item, \
        "the action-menu pending state is attached to the menu item the handler hides"

    # Painting on the shared trigger must not also GUARD on it. Every item in
    # one record's menu paints on the same ••• button, so a guard keyed on the
    # paint target makes the second item clicked return the first item's
    # in-flight promise and never run — the operator sees a pending state for
    # an action that was silently dropped. On the People inspector that menu
    # holds "Revoke sessions" and "Send password-reset link", so the dropped
    # action is one an admin will believe happened.
    assert "key: event.currentTarget" in menu_item, \
        "actionMenu guards on the shared ••• trigger, so one menu item's click " \
        "returns another item's in-flight promise and never runs its own action; " \
        "pass the clicked item as runAction's `key` and keep the trigger as the target"
    assert "options.key || target" in actions, \
        "runAction no longer lets a caller separate the guard key from the paint " \
        "target — actionMenu needs that to guard per menu item"

    # --- rows.js: the refresh button lives inside the block its own onRefresh
    # replaces, so nothing rendered by statusHeadline can carry the
    # announcement. It has to come from the document-level live region.
    assert "runAction(event.currentTarget, () => onRefresh()" in rows, \
        "statusHeadline does not await onRefresh through runAction"
    headline = rows.split("export function statusHeadline", 1)[1]
    for local in ("role: 'status'", "aria-live", "loadingState", "errorState"):
        assert local not in headline, \
            f"statusHeadline builds its own {local!r} inside the block that " \
            f"onRefresh replaces — it would be destroyed before it was read"
    assert "document.body.append(announcer)" in actions, \
        "the announcement region is not parked outside the replaceable subtree"

    # --- views.js: sectionTabs awaits onChange, and the pending state is
    # re-derived by the next paint rather than held across one.
    assert "runAction(event.currentTarget, () => onChange(item.id)" in views, \
        "sectionTabs still fires onChange un-awaited and unguarded"
    tabs = views.split("export function sectionTabs", 1)[1].split("export function timelineView", 1)[0]
    assert "item.id === active ? 'active' : ''" in tabs, \
        "sectionTabs stopped rendering the caller-owned active class"
    assert ".disabled" not in tabs and "pendingLabel" not in tabs, \
        "sectionTabs disables the tab or swaps its label — the label must stay " \
        "and the control must stay focusable"


def _window(text, start, end):
    """The source between two anchors, so an assertion cannot pass on a
    neighbouring function that happens to contain the same string."""
    head = text.index(start)
    return text[head:text.index(end, head)]


@th.django_unit_test("a swept feature trigger outlives the render its own action starts")
def test_feature_pending_states_survive_their_own_actions_contract(opts):
    api_side = (ASSETS / "features/webapps/api.js").read_text()
    page = (ASSETS / "features/webapps/page.js").read_text()
    wizard = (ASSETS / "features/webapps/wizard.js").read_text()
    serving = (ASSETS / "features/webapps/serving.js").read_text()
    advanced = (ASSETS / "features/advanced/page.js").read_text()

    for name, text in (("webapps/api.js", api_side), ("webapps/page.js", page),
                       ("webapps/wizard.js", wizard), ("webapps/serving.js", serving),
                       ("advanced/page.js", advanced)):
        assert "'../../components/actions.js'" in text, \
            f"{name} does not use the shared responsiveness helpers"

    # --- api.js, the three † triggers: each closes the inspector BEFORE it
    # awaits, so the clicked button is detached from the first frame and a
    # pending state pinned to it would never be seen. Their affordance is the
    # scrim their runner opens.
    inspector = api_side.split("export function openFrameworkInspector", 1)[1]
    assert inspector.count("inspector.close(); return ") == 3, \
        "the framework drill-in's three actions no longer close the inspector " \
        "before handing off — re-check where their affordance belongs"
    assert "runAction(event.currentTarget" not in inspector, \
        "a framework drill-in action pins its pending state to the button the " \
        "inspector.close() on the line before has already detached"
    for runner in ("applyFrameworkUpdate(ctx, framework, reload)",
                   "writeFrameworkPin('hold', reload)", "writeFrameworkPin('', reload)"):
        assert runner in inspector, \
            f"the framework drill-in no longer delegates to {runner}"

    # The runner that owns each of those: dialog first (human input, no
    # affordance), then a scrim over a task with no surviving trigger at all.
    update = _window(api_side, "export async function applyFrameworkUpdate",
                     "async function writeFrameworkPin")
    assert update.index("await confirmFrameworkUpdate(framework)") < update.index("runAction(null,"), \
        "the framework update paints a busy scrim while a human is still " \
        "reading its typed-version confirmation"
    assert "busy: {title: 'Updating django-mojo…'" in update, \
        "restarting every node's service no longer opens the busy scrim"
    pin = _window(api_side, "async function writeFrameworkPin", "export function openFrameworkInspector")
    assert "runAction(null," in pin and "busy: {title:" in pin, \
        "writing the fleet-wide update policy has no affordance of its own"

    # The deploy-recovery buttons are NOT †: the inspector closes only after the
    # request lands, so the button carries the wait itself and is gone with the
    # inspector on success.
    act = _window(api_side, "const act = (action, row, button, alert)", "const extras =")
    assert "runAction(button," in act and "restoreOnSuccess: false" in act, \
        "a deploy recovery action does not carry its pending state on the " \
        "button, or tries to restore one onto a closed inspector"

    # --- page.js: every destructive flow confirms first (human input, nothing
    # paints), then runs under the scrim — the trigger is inside the table or
    # the tab that reload() rebuilds.
    destructive = (
        ("function revokeKey", "function workflowPanel"),
        ("function removeAddress", "function addressesCard"),
        ("function deleteWebApp", "function rollbackTo"),
        ("function rollbackTo", "const UPLOAD_LIMITS"),
    )
    for start, end in destructive:
        block = _window(page, start, end)
        assert "runAction(null," in block, \
            f"{start} attaches its affordance to a node, but reload() rebuilds " \
            f"the row or tab the trigger lives in — this one belongs on the scrim"
        assert "busy: {title:" in block, \
            f"{start} is destructive but does not open the busy scrim"
        assert block.index("confirmAction({") < block.index("runAction(null,"), \
            f"{start} starts its affordance before the human has answered the dialog"

    # --- advanced/page.js: same shape for every provider mutation.
    for start, end in (("async function retireCertificate", "function domainCertificatesPanel"),
                       ("async function removeFailedCertificate", "async function certificatesPage"),
                       ("async function deleteRecord", "async function dnsPage"),
                       ("async function retireUpstream", "async function upstreamsPage"),
                       ("async function deleteRoute", "async function routesPage")):
        block = _window(advanced, start, end)
        assert "runAction(null," in block and "busy: {title:" in block, \
            f"{start} runs a provider mutation with no busy scrim over it"
        assert block.index("await confirmAction({") < block.index("runAction(null,"), \
            f"{start} paints while a human is still reading its confirmation"

    # A panel's loading state goes into a body node, never over the panel: the
    # heading and its Request/Refresh buttons live in the panel itself.
    assert "loadInto(panel," not in advanced, \
        "a panel-scoped load paints its skeleton over the panel heading and " \
        "the action buttons that sit beside it"
    assert advanced.count("const body = h('div', {}); panel.append(body);") >= 3, \
        "the panel-scoped loads stopped rendering into a body node of their own"

    # Money spends behind a scrim, on both surfaces that spend it.
    assert "busy: {title: 'Registering your domain…'" in wizard, \
        "the wizard's domain purchase no longer runs behind the busy scrim"
    assert "busy: {title: `Registering ${quote.name}…`" in advanced, \
        "the Domains purchase no longer runs behind the busy scrim"


@th.django_unit_test("the shared controls answer the click that started them")
def test_shared_controls_are_responsive_contract(opts):
    model = (ASSETS / "components/model.js").read_text()
    rows = (ASSETS / "components/rows.js").read_text()
    views = (ASSETS / "components/views.js").read_text()
    core = (ASSETS / "core.js").read_text()

    for name, text in (("model.js", model), ("rows.js", rows), ("views.js", views)):
        assert "'./actions.js'" in text, \
            f"{name} does not use the shared responsiveness helpers"

    # lifecycleControl: confirm first, affordance second, cancel shows nothing.
    lifecycle = model.split("export function lifecycleControl", 1)[1] \
        .split("export function modelHeader", 1)[0]
    assert "if (!result.confirmed) return;" in lifecycle, \
        "a cancelled confirm no longer leaves the switch untouched"
    assert lifecycle.index("await confirmAction({") < lifecycle.index("runAction(button,"), \
        "the pending state starts before the human has answered the dialog"

    # FormView keeps its own (correct) guard and gains only the announcement.
    form = _code(core.split("export class FormView", 1)[1])
    assert "runAction" not in form, \
        "FormView was routed through runAction — that defers its synchronous " \
        "disable by 150ms and re-opens the double-submit window"
    assert "button.disabled = true" in form, \
        "FormView lost its synchronous submit guard"
    assert "const pending = h('div', {class: 'sr-only', role: 'status', 'aria-live': 'polite'});" in form, \
        "a form submit is still silent to assistive technology"
    assert "pending.textContent = `${this.submitLabel}…`;" in form, \
        "the submit announcement is never populated"


# `INFLIGHT` is one process-global Map, so a guard key is a portal-wide name,
# not a module-local one. These two patterns read the key back off a call site.
CALL = re.compile(r"runAction\(")
KEY_LITERAL = re.compile(r"(?<![\w$])key:\s*(['\"`])(.*?)\1", re.DOTALL)
# How far past `runAction(` its own options object can reasonably sit. Every
# call in the tree today puts the key within a few lines; the bound is what
# stops one call's window from swallowing an unrelated `key:` far below it.
KEY_WINDOW = 1500


def _guard_keys(text):
    """Every string `key:` an actual runAction call site passes.

    Read forward from each `runAction(`, never backwards, and stop at the next
    one. Bare `key:` literals elsewhere in the tree are not guard keys at all —
    `core.js` has an SVG path under that name in its icon map, and
    `features/settings/panels.js` writes `{action: 'set', key: row.key}` at the
    settings API — and a naive scan for `key:` would report all of them.
    """
    starts = [match.start() for match in CALL.finditer(text)]
    found = []
    for index, start in enumerate(starts):
        stop = min(starts[index + 1] if index + 1 < len(starts) else len(text),
                   start + KEY_WINDOW)
        match = KEY_LITERAL.search(text, start, stop)
        if match:
            found.append((text.count("\n", 0, match.start()) + 1, match.group(2)))
    return found


@th.django_unit_test("one guard key names exactly one action, portal-wide")
def test_guard_keys_are_unique_contract(opts):
    # #2242 phase 3 review, F1/F2. `runAction`'s re-entry guard is only correct
    # when a key means ONE action: a second click on a key already in flight is
    # handed the first click's promise and its own task never runs. That failure
    # is silent and it looks like success — the control restores, the screen does
    # not move, and whatever the handler already wrote outside the guard (a URL,
    # an `active` flag) now describes work that never happened.
    #
    # The check is tree-wide rather than per-file on purpose: INFLIGHT is a
    # single process-global Map, so the namespace is flat across the whole
    # portal. A per-file check would have missed the real collision in this
    # tree — `features/webapps/api.js` and `features/platform/maintenance.js`
    # both spelled a *different* framework-update action `'framework-update'`.
    sites = {}
    for path in swept():
        for line, key in _guard_keys(path.read_text()):
            # An interpolated key is exempt: one prefix over many ids is exactly
            # what it is for, and `webapps:rollback:${app.id}:${release.id}` is
            # shared by design across every row that renders it.
            if "${" in key:
                continue
            sites.setdefault(key, []).append(f"{_name(path)}:{line}")

    shared = {key: where for key, where in sites.items() if len(where) > 1}
    assert not shared, (
        "a guard key is used by more than one runAction call site, so one of "
        "them silently returns the other's in-flight promise and never runs: "
        + "; ".join(f"{key!r} at {', '.join(where)}" for key, where in sorted(shared.items()))
        + " — give each action its own key, spelled <feature>:<action>[:<id>], "
          "or interpolate the discriminator the two calls differ on")

    # A key that matched nothing would make the assertion above vacuous.
    assert len(sites) >= 4, \
        f"only {len(sites)} literal guard keys were read off runAction call " \
        f"sites under {ASSETS} — the scan is not finding them, so this guard " \
        f"proves nothing"


# A tab nav is the shape where a shared key does the most damage: the handler
# writes `active` and the URL synchronously, outside the guard, before the guard
# drops the render on the floor.
TABS = "class: 'tabs'"
TABS_WINDOW = 800


@th.django_unit_test("a tab nav never guards every tab on one shared key")
def test_tab_navs_do_not_share_a_guard_key_contract(opts):
    # #2242 phase 3 review, F1. features/people/page.js gave Users and Groups
    # one key, `'people-view'`. Click Users, then Groups before the slow list
    # lands: `active` and the URL are already 'groups', runAction finds the key
    # in flight and hands back the *Users* render's promise, render() never runs
    # for Groups, and the screen keeps showing users under a URL saying groups.
    #
    # A tab switch supersedes; it does not queue. See responsiveness.md.
    for path in swept():
        text = path.read_text()
        for match in re.finditer(re.escape(TABS), text):
            window = text[match.start():match.start() + TABS_WINDOW]
            bare = [key for _, key in _guard_keys(window) if "${" not in key]
            assert not bare, (
                f"{_name(path)} builds a tab nav whose tabs all guard on the "
                f"same literal key {bare[0]!r} — the second tab clicked gets the "
                f"first tab's in-flight promise and never renders, while `active` "
                f"and the URL (written synchronously, outside the guard) already "
                f"say otherwise. Key per tab, or drop the guard: a tab switch "
                f"supersedes rather than queues")

    people = (ASSETS / "features/people/page.js").read_text()
    # The People nav dropped its guard rather than keying per tab: keying per
    # tab only moves the same failure out to the third click (Users, Groups,
    # Users — the third finds the first still in flight).
    assert "{key: 'people-view'}" not in people, \
        "the People tab nav is back on one shared guard key for Users and Groups"
    assert "history.replaceState({}, '', routeHref(id)); return render(); }" in people, \
        "the People tab nav no longer calls render() directly — if it was " \
        "re-wrapped in runAction, the guard must be per tab, and the reason " \
        "the guard is wrong here belongs in responsiveness.md"

    # Dropping the guard is only safe because render() supersedes correctly.
    render_body = _window(people, "  async function render(term = '')", "  await render(); return root;")
    assert "const mine = ++generation;" in render_body and "mine !== generation" in render_body, \
        "render() lost its generation token — with the tab nav's guard gone, " \
        "that token is the only thing dropping a superseded paint"
    assert "const listBody = h('div', {});" in render_body, \
        "render() no longer builds a fresh listBody each pass, so two renders " \
        "share one loadInto generation target and can cross-write"

    # components/views.js sectionTabs is every other tab nav in the portal. It
    # was never exposed — it guards on the clicked button — and must stay that way.
    views = (ASSETS / "components/views.js").read_text()
    tabs = views.split("export function sectionTabs", 1)[1].split("export function timelineView", 1)[0]
    assert "runAction(event.currentTarget," in tabs, \
        "sectionTabs stopped keying on the clicked tab — every caller's tabs " \
        "would then share one guard key"


@th.django_unit_test("a spent quote cannot be spent again from the same button")
def test_domain_purchase_latches_its_control_contract(opts):
    # #2242 phase 3 review, F3. Before the sweep the handler set
    # `buy.disabled = true` at the top and never re-enabled it, so a spent quote
    # was terminal. runAction restores the control on the error path, which put
    # a live "Register domain" directly under the message saying the quote was
    # spent. The server refuses the retry (a select_for_update CAS on the quote
    # status plus a confirm-token match), so it is a second confusing refusal
    # rather than a double charge — but the operator should never get there.
    advanced = (ASSETS / "features/advanced/page.js").read_text()
    confirm = _window(advanced, "  function renderConfirm(groupId)",
                      "async function loadDomainCertificates")

    assert "let spent = false;" in confirm, \
        "the domain purchase has no latch, so runAction's error-path restore " \
        "hands the operator back a live button for a quote that is spent"
    assert "spent = true; buy.disabled = true;" in confirm, \
        "the purchase does not latch its control off inside the task"
    # Synchronous, before the first await: the guard and the latch must agree.
    assert confirm.index("spent = true; buy.disabled = true;") < confirm.index("await apiOnce('/api/dnsman/registrar/purchase'"), \
        "the latch is set after the spend has already started, so a click in " \
        "the same turn still finds the control live"
    # validate() re-derives `disabled` on every keystroke; without the flag it
    # would hand the button straight back the moment the operator retyped.
    assert "buy.disabled = spent ||" in confirm, \
        "validate() ignores the latch — typing in either confirmation field " \
        "re-enables a button whose quote has already been spent"
