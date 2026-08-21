// The owner-only Assistant setup view, mounted inside the panel.
//
// Owner-tier for read AND write: the endpoint proves a live literal superuser
// in its own body and answers 440 when the session is not recent, so this view
// has to survive a step-up and retry. core.js owns that: it prompts, retries
// the same request, and raises `fresh_auth_required` only when the operator
// declines -- which runAction renders as nothing at all.
//
// The key field is WRITE-ONLY and never pre-filled. It has no placeholder
// standing in for a stored value either: a masked placeholder reads as "a
// value is already here", and this field always means "replace it".

import {api, h, icon} from '../core.js';
import {announce, runAction, toast} from '../components/actions.js';
import {errorState, loadingState} from '../components/views.js';

const ENDPOINT = '/api/account/admin/assistant';

const SOURCE_COPY = {
  admin: 'stored here, in this Admin',
  deployment: 'from the deployment settings file',
  fallback: 'from the LLM_HANDLER_API_KEY fallback',
  none: 'not configured anywhere',
};

function readiness(state) {
  if (!state.assistant_installed || !state.realtime_installed) {
    return 'The assistant and realtime applications must both be installed.';
  }
  if (!state.key.configured) return 'No credential resolves, so the Assistant cannot answer.';
  if (!state.enabled) return 'A credential is configured, but the Assistant is switched off.';
  return 'The Assistant is on and a credential resolves.';
}

export function mountSetup({ctx, panel, onBack}) {
  const host = h('div', {class: 'assistant-setup'});
  let disposed = false;

  function paint(state) {
    if (disposed) return;
    const enabled = h('input', {type: 'checkbox', checked: state.enabled});
    const apiKey = h('input', {type: 'password', autocomplete: 'off', spellcheck: 'false'});
    const clearKey = h('input', {type: 'checkbox'});
    const listId = 'assistant-model-choices';
    const model = h('input', {type: 'text', list: listId, value: state.model.selected || '',
      placeholder: 'Automatic', autocomplete: 'off', spellcheck: 'false'});
    const choices = h('datalist', {id: listId},
      ...(state.model.choices || []).map((choice) => h('option', {
        value: choice.id, label: choice.label})));

    const keyLine = state.key.configured
      ? `Configured, ${SOURCE_COPY[state.key.source] || 'from an unknown source'}`
        + (state.key.hint ? ` · ends ${state.key.hint}` : '')
      : 'No API key is configured.';
    const verifyLine = state.verify.at
      ? `${state.verify.message || 'Checked'} (${state.verify.at})`
      : 'The stored key has not been checked yet.';

    const save = h('button', {class: 'button primary compact', type: 'button'}, 'Save');
    const verify = h('button', {class: 'button ghost compact', type: 'button'}, 'Check key');
    const refresh = h('button', {class: 'icon-button', type: 'button',
      'aria-label': 'Refresh the model list'}, icon('refresh'));
    const back = h('button', {class: 'button ghost compact', type: 'button'}, 'Back to chat');

    save.addEventListener('click', (event) => runAction(event.currentTarget, async () => {
      const payload = {
        action: 'save',
        enabled: enabled.checked,
        model: model.value.trim(),
        clear_api_key: clearKey.checked,
      };
      // The field is omitted rather than sent empty: an empty string is not a
      // credential, and "leave it alone" must not read as "store nothing".
      if (apiKey.value) payload.api_key = apiKey.value;
      const result = await api(ENDPOINT, {method: 'POST', body: JSON.stringify(payload)});
      apiKey.value = '';
      toast('Assistant settings saved.');
      await syncReadiness(result.state);
      paint(result.state);
    }, {pendingLabel: 'Saving…'}));

    verify.addEventListener('click', (event) => runAction(event.currentTarget, async () => {
      const payload = {action: 'verify'};
      if (apiKey.value) payload.api_key = apiKey.value;
      const result = await api(ENDPOINT, {method: 'POST', body: JSON.stringify(payload)});
      announce(result.result?.message || 'Key checked.');
      toast(result.result?.message || 'Key checked.',
        {tone: result.result?.ok ? 'success' : 'danger'});
      paint(result.state);
    }, {pendingLabel: 'Checking…'}));

    refresh.addEventListener('click', (event) => runAction(event.currentTarget, async () => {
      // The one control allowed to reach the provider for the model catalogue.
      const fresh = await api(`${ENDPOINT}?refresh=models`);
      paint(fresh);
    }, {announceLabel: 'Refreshing the model list'}));

    back.addEventListener('click', () => { onBack?.(); });

    host.replaceChildren(
      h('h3', {text: 'Assistant setup'}),
      h('p', {class: 'assistant-note', text: readiness(state)}),
      h('label', {class: 'check-field'}, enabled, h('span', {text: 'Assistant enabled'})),
      h('div', {class: 'assistant-setup-row'},
        h('div', {}, h('strong', {text: 'Provider key'}), h('span', {text: keyLine})),
        verify),
      h('label', {class: 'field'}, h('span', {text: 'Replace the API key'}), apiKey,
        h('small', {text: 'Stored encrypted in the database. It is never shown again, here or anywhere else.'})),
      h('label', {class: 'check-field'}, clearKey,
        h('span', {text: 'Clear the stored key on save'})),
      h('label', {class: 'field'}, h('span', {text: 'Model'}), model, choices,
        h('small', {text: `Leave blank for automatic. Effective now: ${state.model.effective}`})),
      h('div', {class: 'assistant-setup-row'},
        h('div', {}, h('strong', {text: 'Last check'}), h('span', {text: verifyLine})),
        refresh),
      h('div', {class: 'form-actions'}, back, save));
  }

  async function syncReadiness(state) {
    const current = ctx.features?.assistant?.capabilities;
    if (!current) return;
    const ready = Boolean(state.enabled && state.key.configured);
    if (current.ready === ready) return;
    // Re-read the bootstrap rather than trusting this page's arithmetic: the
    // server owns that predicate, and the composer appears (or does not) on its
    // answer without a reload.
    try {
      const bootstrap = await api('/api/account/admin/bootstrap');
      const next = bootstrap.features?.assistant?.capabilities;
      if (next) Object.assign(current, next);
    } catch (_) {
      current.ready = ready;
    }
  }

  host.replaceChildren(loadingState('Loading Assistant setup'));
  api(ENDPOINT).then((state) => paint(state)).catch((error) => {
    if (error?.code === 'fresh_auth_required') return;
    host.replaceChildren(errorState(error, () => panel.showSetup?.()));
  });

  panel.body.replaceChildren(host);
  return {node: host, dispose() { disposed = true; }};
}
