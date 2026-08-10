import {api, apiOnce, badge, formatDate, h, icon, listData, openModal, pageHeader, TableView} from '../../core.js';
import {openInspector} from '../../components/overlays.js';
import {activityHref, decodeRouteState, returnLocation, routeHref} from '../../components/routes.js';

function oneTimeSecret(webapp, result, returnFocus) {
  let secret = result.token;
  const secretField = h('textarea', {class: 'secret', readonly: true, rows: '4', text: secret});
  const content = h('div', {},
    h('div', {class: 'callout warning'}, icon('alert'), h('div', {}, h('strong', {text: 'Copy this value now'}), h('p', {text: 'It cannot be retrieved after this window closes. If it is lost, rotate the credential.'}))),
    h('label', {class: 'field'}, h('span', {text: 'GitHub Actions secret: MOJO_DEPLOY_KEY'}), secretField),
    h('button', {class: 'button primary', onclick: async (event) => { await navigator.clipboard.writeText(secret); event.currentTarget.textContent = 'Copied'; }}, icon('key'), 'Copy secret'),
    h('div', {class: 'command'}, h('code', {text: 'gh secret set MOJO_DEPLOY_KEY --repo YOUR_ORG/YOUR_REPO'})));
  openModal({title: `${webapp.slug} deployment key`, subtitle: 'The previous key is already inactive.', content, returnFocus, onClose: () => {
    secretField.value = ''; secretField.textContent = ''; secret = ''; result.token = null;
  }});
}

async function openWebapp(ctx, webapp, reload) {
  const summary = await api(`/api/edge/webapp/summary?webapp=${encodeURIComponent(webapp.id)}`);
  const address = summary.address || {};
  const deploymentKey = summary.deployment_key || {};
  const content = h('div', {class: 'webapp-inspector'},
    h('dl', {class: 'details'},
      h('div', {}, h('dt', {text: 'Environment'}), h('dd', {text: webapp.environment || '—'})),
      h('div', {}, h('dt', {text: 'Repository'}), h('dd', {text: webapp.github_repository || 'Not connected'})),
      h('div', {}, h('dt', {text: 'Address'}), h('dd', {text: address.hostname || 'Not configured'})),
      h('div', {}, h('dt', {text: 'Onboarding'}), h('dd', {text: summary.onboarding?.status || 'unknown'})),
      h('div', {}, h('dt', {text: 'Deployment key'}), h('dd', {text: deploymentKey.active ? 'Active' : 'Not active'}))),
    h('div', {class: 'activity-links'},
      h('a', {class: 'related-record', href: activityHref('logs', {type: 'model', id: webapp.id, model: 'WebApp'}, {return: returnLocation()})}, h('strong', {text: 'Related logs'}), icon('chevron')),
      h('a', {class: 'related-record', href: activityHref('events', {type: 'model', id: webapp.id, model: 'WebApp'}, {return: returnLocation()})}, h('strong', {text: 'Related events'}), icon('chevron')),
      address.domain?.id ? h('a', {class: 'related-record', href: routeHref('domains', {inspector: address.domain.id, return: returnLocation()})}, h('strong', {text: 'Open domain'}), icon('chevron')) : null));
  const inspector = openInspector({title: `WebApp · ${webapp.display_name || webapp.slug}`, content, wide: true});
  if (ctx.capabilities.manage_webapps) content.prepend(h('button', {class: 'button compact', onclick: () => {
    inspector.close(); credentialDialog(webapp, reload);
  }}, icon('key'), 'Manage deployment key'));
}

async function credentialDialog(webapp, reload) {
  const payload = await api(`/api/edge/webapp/key_status?webapp=${encodeURIComponent(webapp.id)}`);
  const status = payload.status;
  const action = status.linked ? 'rotate' : 'mint';
  const actionLabel = status.linked ? 'Rotate key' : status.last_action === 'revoke' ? 'Create new key' : 'Create key';
  const confirm = h('input', {placeholder: webapp.slug, autocomplete: 'off'});
  const message = h('div', {class: 'form-message', role: 'alert'});
  const submit = h('button', {class: `button ${status.linked ? 'danger' : 'primary'}`, disabled: true}, icon('key'), actionLabel);
  confirm.addEventListener('input', () => { submit.disabled = confirm.value !== webapp.slug; });
  const content = h('div', {},
    h('div', {class: 'credential-status'}, h('div', {}, h('span', {text: 'GitHub Actions secret'}), h('strong', {text: 'MOJO_DEPLOY_KEY'})), badge(status.linked && status.active ? 'Active' : status.last_action === 'revoke' ? 'Revoked' : 'Not configured', status.linked && status.active ? 'success' : status.last_action === 'revoke' ? 'warning' : 'neutral')),
    status.linked ? h('dl', {class: 'details'}, h('div', {}, h('dt', {text: 'Created'}), h('dd', {text: formatDate(status.created)})), h('div', {}, h('dt', {text: 'Last used'}), h('dd', {text: formatDate(status.last_used)}))) : null,
    h('div', {class: 'callout'}, icon('alert'), h('p', {text: status.linked ? 'Rotation immediately disables the current key. Update GitHub before running another deployment.' : 'The token is restricted to registering releases for this WebApp.'})),
    h('label', {class: 'field'}, h('span', {text: `Type “${webapp.slug}” to confirm`}), confirm), message,
    h('div', {class: 'form-actions'}, submit,
      status.linked ? h('button', {class: 'button ghost', onclick: () => revokeCredential(webapp, reload)}, 'Revoke instead') : null));
  const close = openModal({title: `${webapp.slug} credential`, subtitle: 'Explicit, audited, and reveal-once.', content, danger: status.linked});
  submit.addEventListener('click', async () => {
    submit.disabled = true;
    try {
      const result = await api('/api/edge/webapp/link_key', {method: 'POST', body: JSON.stringify({webapp: webapp.id, action, operation_id: crypto.randomUUID()})});
      close(); await reload();
      const returnFocus = document.querySelector(`[data-webapp-key="${webapp.id}"]`);
      if (result.token) oneTimeSecret(webapp, result, returnFocus);
      else openModal({title: `${webapp.slug} secret unavailable`, subtitle: 'The credential mutation has a durable receipt.', returnFocus,
        content: h('div', {class: 'callout warning'}, icon('alert'), h('p', {text: 'The reveal response was already consumed or lost. The token cannot be recovered; explicitly rotate to receive a new value.'}))});
    } catch (error) { message.textContent = error.message; submit.disabled = false; }
  });
}

function revokeCredential(webapp, reload) {
  const confirm = h('input', {placeholder: webapp.slug, autocomplete: 'off'});
  const message = h('div', {class: 'form-message', role: 'alert'});
  const button = h('button', {class: 'button danger', disabled: true}, 'Revoke deployment key');
  confirm.addEventListener('input', () => { button.disabled = confirm.value !== webapp.slug; });
  const close = openModal({title: `Revoke ${webapp.slug} key?`, subtitle: 'Future releases will fail until a new key is created.', danger: true, content: h('div', {}, h('label', {class: 'field'}, h('span', {text: `Type “${webapp.slug}” to confirm`}), confirm), message, h('div', {class: 'form-actions'}, button))});
  button.addEventListener('click', async () => {
    button.disabled = true;
    try { await api('/api/edge/webapp/revoke_key', {method: 'POST', body: JSON.stringify({webapp: webapp.id, operation_id: crypto.randomUUID()})}); close(); await reload(); }
    catch (error) { message.textContent = error.message; button.disabled = false; }
  });
}

async function workflowDialog(webapp) {
  const message = h('div', {class: 'form-message', role: 'alert'});
  const content = h('div', {class: 'wizard-form'}, message);
  openModal({title: `${webapp.slug} GitHub workflow`, subtitle: 'Versioned, validated inputs and no embedded secrets.', content});
  try {
    const result = await api('/api/edge/webapp/onboarding/workflow', {method: 'POST', body: JSON.stringify({webapp: webapp.id})});
    const text = h('textarea', {class: 'secret', readonly: true, rows: '18', text: result.yaml});
    content.replaceChildren(h('div', {class: 'callout'}, icon('key'), h('p', {text: 'Save this file at .github/workflows/deploy-webapp.yml and configure MOJO_DEPLOY_KEY separately.'})),
      text, h('button', {class: 'button primary', onclick: async (event) => {
        await navigator.clipboard.writeText(text.value); event.currentTarget.textContent = 'Copied';
      }}, 'Copy workflow'));
  } catch (error) { message.textContent = error.message; }
}

function field(label, input, help = '') {
  return h('label', {class: 'field'}, h('span', {text: label}), input,
    help ? h('small', {text: help}) : null);
}

function stepList(operation) {
  const steps = [['app', 'App'], ['address', 'Address'], ['github', 'Connect GitHub'], ['verify', 'Verify']];
  const current = steps.findIndex(([id]) => id === operation.cursor);
  return h('ol', {class: 'onboarding-steps', 'aria-label': 'WebApp onboarding progress'},
    ...steps.map(([id, label], index) => h('li', {
      class: index < current || operation.cursor === 'complete' ? 'complete' : index === current ? 'current' : '',
      'aria-current': index === current ? 'step' : null,
    }, h('span', {text: index < current || operation.cursor === 'complete' ? '✓' : String(index + 1)}), label)));
}

async function chooseStep(operation, choice, update, message) {
  try {
    const result = await apiOnce('/api/edge/webapp/onboarding/choose', {method: 'POST', body: JSON.stringify({
      operation: operation.operation_id, revision: operation.revision,
      step: operation.cursor, choice,
    })});
    update(result);
  } catch (error) {
    message.textContent = `${error.message}. If the provider may have accepted the request, use “Reconcile status”; do not replay it blindly.`;
    message.classList.add('refresh-required');
  }
}

function addressChoice(operation, ctx, update) {
  const wrap = h('div', {class: 'wizard-choice'});
  const message = h('div', {class: 'form-message', role: 'alert'});
  const label = h('input', {value: 'www', autocomplete: 'off'});
  const mode = h('select', {}, h('option', {value: 'existing', text: 'Use a managed domain'}),
    h('option', {value: 'purchase', text: 'Purchase a new domain'}));
  const choices = h('div');
  let quote = null;
  async function renderMode() {
    choices.replaceChildren();
    if (mode.value === 'existing') {
      const domain = h('select', {}, h('option', {value: '', text: 'Select a domain'}));
      try {
        listData(await api(`/api/dnsman/domain?group=${encodeURIComponent(ctx.groups?.[0]?.id || '')}`))
          .filter((row) => row.status === 'active' && row.verified !== false)
          .forEach((row) => domain.append(h('option', {value: row.id, text: row.name})));
      } catch (error) { message.textContent = error.message; }
      choices.append(field('Managed domain', domain), h('button', {class: 'button primary', onclick: () => {
        if (!domain.value) { message.textContent = 'Select a managed domain.'; return; }
        chooseStep(operation, {domain: Number(domain.value), label: label.value}, update, message);
      }}, 'Continue'));
      return;
    }
    const name = h('input', {placeholder: 'example.com', autocomplete: 'off'});
    const quoteButton = h('button', {class: 'button'}, 'Get live quote');
    const confirm = h('div');
    quoteButton.addEventListener('click', async () => {
      try {
        quote = await apiOnce('/api/dnsman/registrar/quote', {method: 'POST', body: JSON.stringify({
          group: ctx.groups?.[0]?.id, domain: name.value, years: 1,
        })});
        confirm.replaceChildren(h('div', {class: 'callout warning'}, icon('alert'), h('div', {},
          h('strong', {text: `${quote.name} — ${quote.price} ${quote.currency}`}),
          h('p', {text: `Quote expires ${formatDate(quote.expires)}. Type the exact domain and price to authorize the purchase.`}))),
        field('Confirm domain', h('input', {id: 'purchase-domain', autocomplete: 'off'})),
        field('Confirm price', h('input', {id: 'purchase-price', inputmode: 'decimal', autocomplete: 'off'})),
        h('button', {class: 'button danger', onclick: async () => {
          const payload = {purchase: quote.purchase, confirm_token: quote.token,
            confirm_domain: confirm.querySelector('#purchase-domain').value,
            confirm_price: confirm.querySelector('#purchase-price').value, label: label.value};
          await chooseStep(operation, payload, update, message);
          quote.token = null; quote = null;
        }}, 'Purchase and continue'));
      } catch (error) { message.textContent = error.message; }
    });
    choices.append(field('Domain to purchase', name), quoteButton, confirm);
  }
  mode.addEventListener('change', renderMode);
  wrap.append(field('Subdomain', label, 'Apex onboarding is intentionally refused.'), field('Address source', mode), choices, message);
  renderMode();
  return wrap;
}

function githubChoice(operation, update) {
  const profile = operation.profile || {};
  const repository = h('input', {value: profile.github_repository || '', placeholder: 'owner/repository', autocomplete: 'off'});
  const ref = h('input', {value: profile.deployment_ref || 'main', autocomplete: 'off'});
  const output = h('input', {value: profile.build_output || 'dist', autocomplete: 'off'});
  const attest = h('input', {type: 'checkbox'});
  const message = h('div', {class: 'form-message', role: 'alert'});
  return h('div', {class: 'wizard-choice'},
    field('Repository', repository, 'Only a repository visible to this group’s GitHub App installation can be verified.'),
    field('Branch or tag', ref), field('Build output directory', output),
    h('label', {class: 'check-row'}, attest, h('span', {text: 'Continue with an explicit attestation if GitHub evidence is unavailable'})),
    h('button', {class: 'button primary', onclick: () => chooseStep(operation, {
      repository: repository.value, ref: ref.value, output: output.value,
      attest_unavailable: attest.checked,
    }, update, message)}, 'Connect and verify'), message);
}

function wizardChoice(operation, ctx, update) {
  const message = h('div', {class: 'form-message', role: 'alert'});
  if (operation.cursor === 'app') return h('div', {class: 'wizard-choice'},
    h('p', {text: 'Create or adopt the application profile using the frozen values above.'}),
    h('button', {class: 'button primary', onclick: () => chooseStep(operation, {}, update, message)}, 'Create application'), message);
  if (operation.cursor === 'address') return addressChoice(operation, ctx, update);
  if (operation.cursor === 'github') return githubChoice(operation, update);
  if (operation.cursor === 'verify') return h('div', {class: 'wizard-choice'},
    h('p', {text: 'Run a DNS-pinned HTTPS request to the owned hostname. Redirects and non-public addresses are refused.'}),
    h('button', {class: 'button primary', onclick: () => chooseStep(operation, {}, update, message)}, 'Verify HTTPS root'), message);
  return h('div', {class: 'callout success'}, icon('check'), h('p', {text: 'Onboarding is complete.'}));
}

function onboardingPanel(initial, ctx, reloadApps) {
  let operation = initial;
  const root = h('section', {class: 'panel onboarding-panel'});
  const update = (next) => { operation = next.operation || next; render(); };
  async function reconcile() {
    try { update(await api(`/api/edge/webapp/onboarding/detail?operation=${encodeURIComponent(operation.operation_id)}`)); await reloadApps(); }
    catch (error) { root.querySelector('.form-message').textContent = error.message; }
  }
  function render() {
    const evidence = Object.entries(operation.evidence || {}).map(([key, value]) =>
      h('div', {}, h('dt', {text: key}), h('dd', {}, badge(value.status || 'recorded'))));
    root.replaceChildren(h('div', {class: 'panel-heading'}, h('div', {},
      h('h2', {text: operation.profile?.display_name || operation.profile?.slug || 'New WebApp'}),
      h('p', {text: `${operation.status} · revision ${operation.revision}`})),
      h('button', {class: 'button compact', onclick: reconcile}, icon('refresh'), 'Reconcile status')),
    stepList(operation), operation.last_error ? h('div', {class: 'callout warning'}, icon('alert'), h('p', {text: operation.last_error})) : null,
    evidence.length ? h('dl', {class: 'details onboarding-evidence'}, ...evidence) : null,
    wizardChoice(operation, ctx, update), h('div', {class: 'form-message', role: 'alert'}));
  }
  render(); return root;
}

function startOnboarding(ctx, mount, reloadApps) {
  const group = h('select', {}, ...(ctx.groups || []).map((row) => h('option', {value: row.id, text: row.name})));
  const slug = h('input', {placeholder: 'customer-portal', autocomplete: 'off'});
  const name = h('input', {placeholder: 'Customer portal', autocomplete: 'off'});
  const environment = h('select', {}, ...['production', 'staging', 'preview', 'development'].map((value) => h('option', {value, text: value})));
  const repository = h('input', {placeholder: 'owner/repository', autocomplete: 'off'});
  const ref = h('input', {value: 'main', autocomplete: 'off'});
  const output = h('input', {value: 'dist', autocomplete: 'off'});
  const bucket = h('select');
  const message = h('div', {class: 'form-message', role: 'alert'});
  const submit = h('button', {class: 'button primary'}, 'Start onboarding');
  api(`/api/edge/webapp/onboarding/options?group=${encodeURIComponent(group.value)}`).then((data) => {
    bucket.replaceChildren(...(data.buckets || []).map((value) => h('option', {value, text: value})));
  }).catch((error) => { message.textContent = error.message; });
  const content = h('div', {class: 'wizard-form'}, field('Group', group), field('Application slug', slug),
    field('Display name', name), field('Environment', environment), field('Release bucket', bucket),
    field('GitHub repository', repository), field('Deployment ref', ref), field('Build output', output),
    message, h('div', {class: 'form-actions'}, submit));
  const close = openModal({title: 'Onboard WebApp', subtitle: 'App → Address → Connect GitHub → Verify', content});
  submit.addEventListener('click', async () => {
    submit.disabled = true;
    try {
      const result = await api('/api/edge/webapp/onboarding/create', {method: 'POST', body: JSON.stringify({
        group: Number(group.value), slug: slug.value, display_name: name.value,
        environment: environment.value, bucket: bucket.value,
        github_repository: repository.value, deployment_ref: ref.value, build_output: output.value,
      })});
      close(); mount.replaceChildren(onboardingPanel(result.operation, ctx, reloadApps));
    } catch (error) { message.textContent = error.message; submit.disabled = false; }
  });
}

export async function webappsPage(ctx) {
  const root = h('div', {class: 'page'}); let linkedInspectorOpened = false;
  const onboarding = h('div', {class: 'onboarding-mount'});
  async function render() {
    root.replaceChildren(pageHeader('Deployments', 'WebApps', 'Durable App → Address → GitHub → Verify onboarding.'), onboarding);
    const panel = h('section', {class: 'panel'}, h('div', {class: 'panel-heading'}, h('div', {}, h('h2', {text: 'Applications'}), h('p', {text: 'Manage onboarding and MOJO_DEPLOY_KEY without exposing unrelated API keys.'})),
      ctx.capabilities.manage_webapps ? h('button', {class: 'button primary', onclick: () => startOnboarding(ctx, onboarding, render)}, icon('plus'), 'Onboard WebApp') : null));
    root.append(panel);
    try {
      const rows = listData(await api('/api/edge/webapp'));
      const statuses = new Map();
      await Promise.all(rows.map(async (row) => {
        try {
          const payload = await api(`/api/edge/webapp/key_status?webapp=${encodeURIComponent(row.id)}`);
          statuses.set(row.id, payload.status);
        } catch (_) { statuses.set(row.id, null); }
      }));
      panel.append(new TableView({columns: [
        {label: 'WebApp', render: (r) => h('div', {}, h('strong', {text: r.slug}), h('small', {class: 'mono', text: `#${r.id}`}))},
        {label: 'Current release', render: (r) => r.current_release ? badge(r.current_release.version || r.current_release.id, 'success') : badge('No release')},
        {label: 'Deploy key', render: (r) => { const status = statuses.get(r.id); return status?.linked && status?.active ? badge('Active', 'success') : status?.last_action === 'revoke' ? badge('Revoked', 'warning') : badge('Missing', 'neutral'); }},
        {label: 'Created', render: (r) => formatDate(r.created)},
        {label: '', render: (row) => ctx.capabilities.manage_webapps ? h('div', {class: 'row-actions'},
          h('button', {class: 'button compact', 'data-webapp-key': row.id, onclick: (event) => { event.stopPropagation(); credentialDialog(row, render); }}, icon('key'), 'Manage key'),
          row.github_repository ? h('button', {class: 'button compact', onclick: (event) => { event.stopPropagation(); workflowDialog(row); }}, 'Workflow') : null) : null},
      ], rows, empty: 'No WebApps are visible in your scope.', onSelect: (row) => openWebapp(ctx, row, render)}).render());
      const inspector = decodeRouteState().state.inspector;
      const linked = inspector && rows.find((row) => String(row.id) === String(inspector));
      if (linked && !linkedInspectorOpened) { linkedInspectorOpened = true; await openWebapp(ctx, linked, render); }
    } catch (error) { panel.append(h('div', {class: 'error-state'}, icon('alert'), h('p', {text: error.message}))); }
  }
  await render(); return root;
}
