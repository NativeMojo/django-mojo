// The public API set up from Home, in place. "Set it up" would otherwise send
// the operator into the current Admin's System Setup for what is one typed
// value; this modal runs the same durable fix operation System Setup runs —
// create, drive, answer the base_url choice, drive to the verified end —
// without leaving v2. System Setup stays one link away for anything richer.
//
// Ported from v1's features/dashboard/setup.js: same endpoints, same payloads,
// same states and the same words. The two portals are separate trees on
// purpose, so this is a copy, not an import.
//
// Endpoints consumed:
//   POST /api/account/admin/setup/create    the durable fix operation
//   POST /api/account/admin/setup/advance   one durable step at a time
//   POST /api/account/admin/setup/choose    answers the base_url choice
//   GET  /api/account/admin/setup/options   reconciles a lost create response
//   GET  /api/account/admin/setup/detail    re-reads the reconciled operation
import {api, apiOnce, h} from '../../core.js';
import {runAction, toast} from '../../components/actions.js';
import {openBusy, openModal} from '../../components/overlays.js';
import {routeHref} from '../../components/routes.js';

const TERMINAL = new Set(['succeeded', 'failed', 'cancelled']);

// The server's validate_base_url is the authority (it also rejects localhost
// and private IPs); this only catches what a URL parse can see, so the obvious
// mistakes fail before an operation is created.
function invalidOrigin(value) {
  try {
    const url = new URL(value);
    return url.protocol !== 'https:'
      || Boolean(url.username || url.password || url.search || url.hash)
      || !['', '/'].includes(url.pathname);
  } catch (_error) { return true; }
}

function wait(milliseconds) {
  return new Promise((resolve) => { setTimeout(resolve, milliseconds); });
}

/**
 * The origin this installation appears to answer on, or ''.
 *
 * Copied from v1's platform page verbatim, including its conditional: a portal
 * already served over HTTPS knows its own origin, and ONLY a loopback host
 * asks the local preview harness what the tunnelled origin is. Anything else
 * offers no suggestion rather than a guess.
 */
async function suggestedBaseUrl(signal) {
  if (window.location.protocol === 'https:') return window.location.origin;
  if (!['localhost', '127.0.0.1', '[::1]'].includes(window.location.hostname)) return '';
  try {
    const response = await fetch('/__preview__/context', {
      signal, credentials: 'same-origin', cache: 'no-store', headers: {Accept: 'application/json'},
    });
    if (!response.ok) return '';
    const value = (await response.json())?.data?.suggested_base_url;
    const parsed = new URL(value);
    return parsed.protocol === 'https:' && parsed.origin === value ? value : '';
  } catch (_error) { return ''; }
}

// Same loop as the Setup page: one durable step at a time until the operation
// waits for a choice or ends. Closing the browser mid-drive is safe — the
// operation is durable and System Setup resumes it.
async function drive(operation, busy) {
  for (let count = 0; count < 80; count += 1) {
    if (!operation || TERMINAL.has(operation.status)
      || operation.status === 'waiting_for_choice') break;
    busy.update({
      detail: operation.current_step?.label || 'Reconciling authoritative state',
      progress: operation.steps?.length
        ? Math.round((operation.cursor / operation.steps.length) * 100) : null,
    });
    operation = await api('/api/account/admin/setup/advance', {
      method: 'POST', body: JSON.stringify({operation: operation.id})});
    if (!TERMINAL.has(operation.status) && operation.status !== 'waiting_for_choice') {
      await wait(250);
    }
  }
  return operation;
}

// Create is never replayed blind (the Setup page's rule): a lost response is
// reconciled through options.active_fix, and anything else surfaces.
async function createOperation(replayKey) {
  try {
    return await apiOnce('/api/account/admin/setup/create', {method: 'POST',
      body: JSON.stringify({mode: 'fix', section: 'django', replay_key: replayKey})});
  } catch (error) {
    if (error?.code === 'fresh_auth_required') throw error;
    const options = await api('/api/account/admin/setup/options');
    if (!options.active_fix) {
      throw new Error(`${error.message} The result is uncertain; open System Setup before trying again.`);
    }
    return api(`/api/account/admin/setup/detail?operation=${encodeURIComponent(options.active_fix.id)}`);
  }
}

// System Setup has no v2 screen, so the escape hatch is the current Admin's —
// carrying the same focus and return state v1's own dialog passed it.
function setupHref(ctx) {
  const base = ctx.admin_path || '/admin/';
  return `${base}${routeHref('setup', {focus: 'django.base_url', return: routeHref('dashboard')})}`;
}

export function openApiSetup(ctx, {onDone = null} = {}) {
  const input = h('input', {type: 'url', autocomplete: 'url', autofocus: true,
    placeholder: 'https://api.your-domain.com'});
  const message = h('div', {class: 'form-message', role: 'alert'});
  const save = h('button', {class: 'button primary', type: 'submit'}, 'Save and verify');
  let replayKey = null;

  // The detected origin fills only a field the operator has not typed into.
  let touched = false;
  input.addEventListener('input', () => { touched = true; });
  suggestedBaseUrl(null).then((value) => {
    if (value && !touched && !input.value && input.isConnected) input.value = value;
  });

  const submit = () => {
    message.textContent = '';
    const value = input.value.trim();
    if (invalidOrigin(value)) {
      message.textContent = 'Use one public HTTPS address, without a path or credentials.';
      input.focus();
      return undefined;
    }
    const origin = new URL(value).origin;
    // Headless with its own busy handle, exactly like the Setup page: drive()
    // narrates through busy.update, so the scrim cannot belong to runAction.
    return runAction(null, async () => {
      const busy = openBusy({title: 'Setting up the public API',
        detail: 'Creating the durable Setup operation'});
      try {
        replayKey = replayKey || crypto.randomUUID();
        let operation = await createOperation(replayKey);
        operation = await drive(operation, busy);
        const step = operation?.status === 'waiting_for_choice' ? operation.current_step : null;
        if (step && (step.id === 'base_url' || step.kind === 'base_url')) {
          busy.update({detail: 'Saving the public address'});
          operation = await apiOnce('/api/account/admin/setup/choose', {method: 'POST',
            body: JSON.stringify({
              operation: operation.id, step_id: step.id,
              definition_version: step.definition_version,
              choice_revision: step.choice_revision, choice: {base_url: origin},
            })});
          operation = await drive(operation, busy);
        }
        if (TERMINAL.has(operation?.status)) replayKey = null;
        if (operation?.status === 'succeeded') {
          close();
          toast('Public API address saved and verified.');
          onDone?.();
          return;
        }
        if (operation?.status === 'waiting_for_choice') {
          // A repair that needs more than the address (another section's fix
          // is active, or the service asked something this dialog does not
          // carry). The operation is durable — System Setup resumes it.
          message.textContent = 'This repair needs choices that belong in System Setup. The operation is saved and resumes there.';
          return;
        }
        message.textContent = operation?.status === 'cancelled'
          ? 'The Setup operation was cancelled before it finished.'
          : 'The address was saved, but Setup could not verify it end to end. Open System Setup for the full readiness report.';
      } finally { busy.close(); }
    }, {key: 'home:api-setup',
      onError: (error) => { message.textContent = error.message; }});
  };

  const close = openModal({
    title: 'Set up the public API',
    subtitle: 'The one public HTTPS address this installation answers on.',
    content: h('form', {class: 'home-setup', onsubmit: (event) => {
      event.preventDefault();
      submit();
    }},
    h('label', {class: 'field'},
      h('span', {text: 'Public HTTPS address'}), input,
      h('small', {text: 'One HTTPS origin, no path. Saving runs the same durable repair as System Setup and proves the address end to end.'})),
    message,
    h('div', {class: 'form-actions'},
      h('a', {class: 'button ghost', href: setupHref(ctx)},
        'Open System Setup',
        h('span', {class: 'leaves-v2', text: ' · current Admin'})),
      save)),
  });
  return close;
}
