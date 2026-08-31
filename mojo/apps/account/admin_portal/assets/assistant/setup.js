// The owner-only Assistant setup view, mounted inside the panel.
//
// Owner-tier for read AND write: the endpoint proves a live literal superuser
// in its own body and answers 440 when the session is not recent, so this view
// has to survive a step-up and retry. core.js owns that: it prompts, retries
// the same request, and raises `fresh_auth_required` only when the operator
// declines -- which runAction renders as nothing at all.
//
// Two credentials live here. The PLATFORM key (LLM_HANDLER_API_KEY) is what
// every LLM feature uses -- incident triage, the LLM agent -- and it is the
// Assistant's fallback. The ASSISTANT key (LLM_ADMIN_API_KEY) is an optional
// override for the Assistant alone. Both key fields are WRITE-ONLY and never
// pre-filled. They have no placeholder standing in for a stored value either:
// a masked placeholder reads as "a value is already here", and these fields
// always mean "replace it".

import {api, formatDate, h, icon} from '../core.js';
import {announce, copyButton, runAction, toast} from '../components/actions.js';
import {confirmAction} from '../components/overlays.js';
import {errorState, loadingState} from '../components/views.js';

const ENDPOINT = '/api/account/admin/assistant';

const SOURCE_COPY = {
  admin: 'stored here, in this Admin',
  deployment: 'from the deployment settings file',
  fallback: 'using the platform key',
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

function keyLine(key, absent) {
  if (!key.configured) return absent;
  return `Configured, ${SOURCE_COPY[key.source] || 'from an unknown source'}`
    + (key.hint ? ` · ends ${key.hint}` : '');
}

function verifyLine(verify) {
  return verify.at
    ? `${verify.message || 'Checked'} (${verify.at})`
    : 'This stored key has not been checked yet.';
}

function mcpReadiness(state) {
  if (!state.assistant_installed) return 'The assistant application must be installed.';
  if (!state.mcp.enabled) {
    return 'Off. Remote agents cannot discover or sign in; existing connections '
      + 'are paused, not revoked.';
  }
  if (!state.mcp.url) {
    return 'On, but no public address is configured — set the Public API address '
      + 'in System Setup before a client can connect.';
  }
  return 'On. Agents connect through this installation’s own sign-in page; '
    + 'nothing is pasted.';
}

function discoveryLine(discovery) {
  return discovery.checked_at
    ? `${discovery.detail} (${formatDate(discovery.checked_at)})`
    : 'Not checked yet. Checks run at most once a minute.';
}

// What a connection can actually reach. `tools` is the Assistant's tool door,
// where every change still needs an approval; `api` is full REST reach as that
// person, where the approval step does not apply.
const ACCESS_LABELS = {
  tools: 'Tools',
  api: 'Full API',
  both: 'Tools + full API',
};

function grantRow(grant, disconnect) {
  const td = (text) => h('td', {text});
  const client = grant.client || {};
  const user = grant.user || {};
  const button = h('button', {class: 'button ghost compact', type: 'button'}, 'Disconnect');
  button.addEventListener('click', (event) => runAction(
    event.currentTarget, () => disconnect(grant), {pendingLabel: 'Disconnecting…'}));
  return h('tr', {},
    td(client.name || client.client_id || 'Unknown client'),
    td(user.email || ''),
    td(ACCESS_LABELS[grant.access] || ACCESS_LABELS.tools),
    td(formatDate(grant.created)),
    td(grant.last_used ? formatDate(grant.last_used) : 'Never'),
    td(formatDate(grant.expires)),
    h('td', {}, button));
}

export function mountSetup({ctx, panel, onBack}) {
  const host = h('div', {class: 'assistant-setup'});
  let disposed = false;

  function paint(state) {
    if (disposed) return;
    const enabled = h('input', {type: 'checkbox', checked: state.enabled});
    // This control owns only the database half. A deployment-enforced stop is
    // displayed separately and must never be persisted by an unrelated save.
    const emergencyStop = h('input', {type: 'checkbox', checked: state.emergency_stop_database});
    const autonomousTriage = h('input', {type: 'checkbox', checked: state.autonomous_triage});
    const mcpEnabled = h('input', {type: 'checkbox', checked: state.mcp.enabled});
    const handlerKey = h('input', {type: 'password', autocomplete: 'off', spellcheck: 'false'});
    const clearHandlerKey = h('input', {type: 'checkbox'});
    const apiKey = h('input', {type: 'password', autocomplete: 'off', spellcheck: 'false'});
    const clearKey = h('input', {type: 'checkbox'});
    const listId = 'assistant-model-choices';
    const model = h('input', {type: 'text', list: listId, value: state.model.selected || '',
      placeholder: 'Automatic', autocomplete: 'off', spellcheck: 'false'});
    const choices = h('datalist', {id: listId},
      ...(state.model.choices || []).map((choice) => h('option', {
        value: choice.id, label: choice.label})));

    const save = h('button', {class: 'button primary compact', type: 'button'}, 'Save');
    const verifyHandler = h('button', {class: 'button ghost compact', type: 'button'}, 'Check platform key');
    const verifyAssistant = h('button', {class: 'button ghost compact', type: 'button'}, 'Check Assistant key');
    const refresh = h('button', {class: 'icon-button', type: 'button',
      'aria-label': 'Refresh the model list'}, icon('refresh'));
    const back = h('button', {class: 'button ghost compact', type: 'button'}, 'Back to chat');
    const checkDiscovery = h('button', {class: 'button ghost compact', type: 'button'}, 'Check now');
    const revokeAll = h('button', {class: 'button danger compact', type: 'button',
      disabled: state.mcp.grant_count === 0}, 'Disconnect all');
    const resetBreakers = h('button', {class: 'button danger compact', type: 'button'},
      'Reset breakers');
    const activatePolicy = h('button', {class: 'button danger compact', type: 'button'},
      'Activate deployed policy');
    const historicalBefore = h('input', {type: 'datetime-local'});
    const historicalLimit = h('input', {type: 'number', min: '1', max: '100', value: '20'});
    const historicalTriage = h('button', {class: 'button ghost compact', type: 'button'},
      'Queue historical triage');

    save.addEventListener('click', (event) => runAction(event.currentTarget, async () => {
      const payload = {
        action: 'save',
        enabled: enabled.checked,
        model: model.value.trim(),
        clear_api_key: clearKey.checked,
        clear_handler_api_key: clearHandlerKey.checked,
        mcp_enabled: mcpEnabled.checked,
        emergency_stop: emergencyStop.checked,
        autonomous_triage: autonomousTriage.checked,
      };
      // A key field is omitted rather than sent empty: an empty string is not
      // a credential, and "leave it alone" must not read as "store nothing".
      if (apiKey.value) payload.api_key = apiKey.value;
      if (handlerKey.value) payload.handler_api_key = handlerKey.value;
      const result = await api(ENDPOINT, {method: 'POST', body: JSON.stringify(payload)});
      apiKey.value = '';
      handlerKey.value = '';
      toast('Assistant settings saved.');
      await syncReadiness(result.state);
      paint(result.state);
    }, {pendingLabel: 'Saving…'}));

    function checkKey(target, field) {
      return async () => {
        const payload = {action: 'verify', target};
        if (field.value) payload.api_key = field.value;
        const result = await api(ENDPOINT, {method: 'POST', body: JSON.stringify(payload)});
        announce(result.result?.message || 'Key checked.');
        toast(result.result?.message || 'Key checked.',
          {tone: result.result?.ok ? 'success' : 'danger'});
        paint(result.state);
      };
    }
    verifyHandler.addEventListener('click', (event) => runAction(
      event.currentTarget, checkKey('handler', handlerKey), {pendingLabel: 'Checking…'}));
    verifyAssistant.addEventListener('click', (event) => runAction(
      event.currentTarget, checkKey('assistant', apiKey), {pendingLabel: 'Checking…'}));

    refresh.addEventListener('click', (event) => runAction(event.currentTarget, async () => {
      // The one control allowed to reach the provider for the model catalogue.
      const fresh = await api(`${ENDPOINT}?refresh=models`);
      paint(fresh);
    }, {announceLabel: 'Refreshing the model list'}));

    async function disconnect(grant) {
      const client = grant.client || {};
      const user = grant.user || {};
      const {confirmed} = await confirmAction({
        title: 'Disconnect this agent?',
        copy: `${client.name || client.client_id || 'This client'} connected as `
          + `${user.email || 'an operator'} loses access immediately. `
          + 'It can reconnect by signing in again.',
        confirmLabel: 'Disconnect', danger: true});
      if (!confirmed) return;
      const result = await api(ENDPOINT, {method: 'POST',
        body: JSON.stringify({action: 'revoke_grant', grant_id: grant.id})});
      toast(result.revoked ? 'Agent disconnected.' : 'That connection was already gone.');
      await syncReadiness(result.state);
      paint(result.state);
    }

    checkDiscovery.addEventListener('click', (event) => runAction(
      event.currentTarget, async () => {
        // The one control allowed to reach this installation's own public
        // address; the server caches a network verdict for 60 seconds.
        paint(await api(`${ENDPOINT}?check=discovery`));
      }, {pendingLabel: 'Checking…'}));

    revokeAll.addEventListener('click', (event) => runAction(
      event.currentTarget, async () => {
        const {confirmed} = await confirmAction({
          title: 'Disconnect every agent?',
          copy: `All ${state.mcp.grant_count} connected agents lose access `
            + 'immediately. Each can reconnect by signing in again.',
          confirmLabel: 'Disconnect all', danger: true});
        if (!confirmed) return;
        const result = await api(ENDPOINT, {method: 'POST',
          body: JSON.stringify({action: 'revoke_all_grants'})});
        toast(`Disconnected ${result.revoked} agent(s).`);
        await syncReadiness(result.state);
        paint(result.state);
      }, {pendingLabel: 'Disconnecting…'}));

    resetBreakers.addEventListener('click', (event) => runAction(
      event.currentTarget, async () => {
        const {confirmed} = await confirmAction({
          title: 'Reset every LLM breaker?',
          copy: 'Only do this after the provider or credential problem is resolved. '
            + 'The emergency stop is not changed.',
          confirmLabel: 'Reset breakers', danger: true});
        if (!confirmed) return;
        const result = await api(ENDPOINT, {method: 'POST',
          body: JSON.stringify({action: 'reset_breaker'})});
        toast(`Reset ${result.reset} breaker(s).`);
        paint(result.state);
      }, {pendingLabel: 'Resetting…'}));

    activatePolicy.addEventListener('click', (event) => runAction(
      event.currentTarget, async () => {
        const {confirmed} = await confirmAction({
          title: 'Activate this deployment policy?',
          copy: 'Keep the emergency stop on until every node runs the same policy.',
          confirmLabel: 'Activate policy', danger: true});
        if (!confirmed) return;
        const result = await api(ENDPOINT, {method: 'POST',
          body: JSON.stringify({action: 'activate_policy'})});
        toast('Deployed LLM policy activated.');
        paint(result.state);
      }, {pendingLabel: 'Activating…'}));

    historicalTriage.addEventListener('click', (event) => runAction(
      event.currentTarget, async () => {
        const before = historicalBefore.value
          ? new Date(historicalBefore.value).toISOString() : '';
        const limit = Number(historicalLimit.value);
        if (!before || !Number.isInteger(limit) || limit < 1 || limit > 100) {
          throw new Error('Choose a cutoff and a limit from 1 through 100.');
        }
        const {confirmed} = await confirmAction({
          title: 'Queue historical incident triage?',
          copy: `Queue up to ${limit} incidents created before ${before}.`,
          confirmLabel: 'Queue triage', danger: true});
        if (!confirmed) return;
        const result = await api(ENDPOINT, {method: 'POST', body: JSON.stringify({
          action: 'historical_triage', before, limit})});
        toast(`Queued ${result.queued} incident(s).`);
        paint(result.state);
      }, {pendingLabel: 'Queueing…'}));

    back.addEventListener('click', () => { onBack?.(); });

    const address = state.mcp.url || '<connect address>';

    // replaceChildren is a raw DOM call, not h(): a null child becomes the
    // text "null" on screen. Conditional children (the discovery alert) are
    // collected and filtered first — the same hazard webapps' manageSection
    // names.
    host.replaceChildren(...[
      h('h3', {text: 'Assistant setup'}),
      h('p', {class: 'assistant-note', text: readiness(state)}),
      h('label', {class: 'check-field'}, enabled, h('span', {text: 'Assistant enabled'})),

      h('h4', {text: 'LLM safety'}),
      h('label', {class: 'check-field'}, emergencyStop,
        h('span', {text: 'Emergency stop all ordinary provider requests'})),
      state.emergency_stop_static
        ? h('p', {class: 'assistant-note', text: 'The deployment-file emergency stop is active. Remove it and redeploy before calls can resume.'})
        : null,
      h('label', {class: 'check-field'}, autonomousTriage,
        h('span', {text: 'Allow autonomous incident triage for new incidents'})),
      h('p', {class: 'assistant-note', text: state.autonomous_triage_activated_at
        ? `Autonomous watermark: ${formatDate(state.autonomous_triage_activated_at)}`
        : 'Autonomous triage has no activation watermark.'}),
      h('div', {class: 'assistant-setup-row'},
        h('div', {}, h('strong', {text: 'Last 24 hours'}),
          h('span', {text: `${state.safety?.requests?.length || 0} aggregate usage rows · `
            + `${state.safety?.breakers?.length || 0} breaker rows`})),
        h('div', {}, activatePolicy, resetBreakers)),
      h('div', {class: 'assistant-setup-row'},
        h('label', {class: 'field'}, h('span', {text: 'Historical cutoff'}),
          historicalBefore),
        h('label', {class: 'field'}, h('span', {text: 'Maximum incidents'}),
          historicalLimit), historicalTriage),

      h('h4', {text: 'Platform LLM key'}),
      h('p', {class: 'assistant-note',
        text: 'Used by every LLM feature (incident triage, the LLM agent) and by the Assistant when it has no key of its own.'}),
      h('div', {class: 'assistant-setup-row'},
        h('div', {}, h('strong', {text: 'Platform key'}),
          h('span', {text: keyLine(state.handler_key, 'No platform key is configured.')})),
        verifyHandler),
      h('label', {class: 'field'}, h('span', {text: 'Replace the platform key'}), handlerKey,
        h('small', {text: 'Stored encrypted in the database. It is never shown again, here or anywhere else.'})),
      h('label', {class: 'check-field'}, clearHandlerKey,
        h('span', {text: 'Clear the stored platform key on save'})),
      h('div', {class: 'assistant-setup-row'},
        h('div', {}, h('strong', {text: 'Last check'}),
          h('span', {text: verifyLine(state.handler_verify)}))),

      h('h4', {text: 'Assistant key'}),
      h('p', {class: 'assistant-note',
        text: 'Optional. When set, the Assistant uses this key instead of the platform key.'}),
      h('div', {class: 'assistant-setup-row'},
        h('div', {}, h('strong', {text: 'Assistant resolves'}),
          h('span', {text: keyLine(state.key, 'No API key is configured.')})),
        verifyAssistant),
      h('label', {class: 'field'}, h('span', {text: 'Replace the Assistant key'}), apiKey,
        h('small', {text: 'Stored encrypted in the database. It is never shown again, here or anywhere else.'})),
      h('label', {class: 'check-field'}, clearKey,
        h('span', {text: 'Clear the stored Assistant key on save'})),
      h('div', {class: 'assistant-setup-row'},
        h('div', {}, h('strong', {text: 'Last check'}),
          h('span', {text: verifyLine(state.verify)}))),

      h('h4', {text: 'Model'}),
      h('label', {class: 'field'}, h('span', {text: 'Model'}), model, choices,
        h('small', {text: `Leave blank for automatic. Effective now: ${state.model.effective}`})),
      h('div', {class: 'assistant-setup-row'},
        h('div', {}, h('span', {text: 'Re-read the model catalogue from the provider.'})),
        refresh),

      h('h4', {text: 'Remote agent access (MCP)'}),
      h('p', {class: 'assistant-note', text: mcpReadiness(state)}),
      h('label', {class: 'check-field'}, mcpEnabled,
        h('span', {text: 'Allow remote agents to connect'})),
      h('div', {class: 'assistant-setup-row'},
        h('div', {}, h('strong', {text: 'Connect address'}),
          h('span', {text: state.mcp.url || 'Available once a public address is configured.'})),
        state.mcp.url ? copyButton(state.mcp.url) : null),
      h('details', {class: 'assistant-connect'},
        h('summary', {text: 'How to connect a client'}),
        h('dl', {class: 'assistant-kv'},
          h('dt', {text: 'Claude Code'}),
          h('dd', {},
            h('code', {text: `claude mcp add --scope user --transport http admin-assistant ${address}`}),
            h('small', {text: 'then run /mcp inside Claude Code to sign in.'})),
          h('dt', {text: 'Claude Desktop / claude.ai'}),
          h('dd', {text: 'Settings → Connectors → Add custom connector, paste the '
            + 'address, and sign in when prompted.'}),
          h('dt', {text: 'ChatGPT'}),
          h('dd', {text: 'Settings → Connectors → Developer mode → Create, paste '
            + 'the address; the sign-in service is discovered automatically.'}))),
      h('div', {class: 'assistant-setup-row'},
        h('div', {}, h('strong', {text: 'Discovery check'}),
          h('span', {text: discoveryLine(state.mcp.discovery)}),
          // Text, never a link: the operator can paste it into a browser to
          // see for themselves what their front door serves.
          state.mcp.discovery_url
            ? h('span', {text: `Discovery document: ${state.mcp.discovery_url}`})
            : null),
        checkDiscovery),
      state.mcp.discovery.code === 'unreachable'
        ? h('div', {class: 'assistant-alert is-warning', role: 'alert'},
            h('strong', {text: 'Clients cannot discover the sign-in service.'}),
            'nginx must forward /.well-known/oauth-authorization-server/ and '
            + '/.well-known/oauth-protected-resource/ to the application — the '
            + 'location block is in the Admin docs under Remote agent access.')
        : null,
      h('div', {class: 'assistant-setup-row'},
        h('div', {}, h('strong', {text: 'Connected agents'}),
          h('span', {text: `${state.mcp.grant_count} active`})),
        revokeAll),
      state.mcp.grants.length
        ? h('div', {class: 'assistant-block assistant-grants'},
            h('div', {class: 'table-wrap'},
              h('table', {},
                h('thead', {}, h('tr', {}, ...['Client', 'Signed in as', 'Access',
                  'Connected', 'Last used', 'Expires', ''].map((label) => h('th', {scope: 'col', text: label})))),
                h('tbody', {}, ...state.mcp.grants.map((grant) => grantRow(grant, disconnect))))),
            h('p', {class: 'assistant-note', text: 'Full API rows can call every '
              + 'API as that person; the approval step does not apply to those calls.'}))
        : h('p', {class: 'assistant-note', text: 'No agent is connected.'}),

      h('div', {class: 'form-actions'}, back, save),
    ].filter(Boolean));
  }

  async function syncReadiness(state) {
    const current = ctx.features?.assistant?.capabilities;
    if (!current) return;
    const ready = Boolean(state.enabled && state.key.configured);
    const mcp = Boolean(state.assistant_installed && state.mcp?.enabled && state.mcp?.url);
    if (current.ready === ready && current.mcp === mcp) return;
    // Re-read the bootstrap rather than trusting this page's arithmetic: the
    // server owns both predicates, and the composer and the "Remote access on"
    // chip follow its answer without a reload.
    try {
      const bootstrap = await api('/api/account/admin/bootstrap');
      const next = bootstrap.features?.assistant?.capabilities;
      if (next) Object.assign(current, next);
    } catch (_) {
      current.ready = ready;
      current.mcp = mcp;
    }
    panel.syncChrome?.();
  }

  host.replaceChildren(loadingState('Loading Assistant setup'));
  api(ENDPOINT).then((state) => paint(state)).catch((error) => {
    if (error?.code === 'fresh_auth_required') return;
    host.replaceChildren(errorState(error, () => panel.showSetup?.()));
  });

  panel.body.replaceChildren(host);
  return {node: host, dispose() { disposed = true; }};
}
