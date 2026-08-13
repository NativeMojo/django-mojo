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

const ONBOARDING_DRAFT_KEY = 'mojo-admin:webapp-onboarding-draft:v1';
const NEW_GROUP_VALUE = 'new';

function pendingDraft() {
  try {
    const value = JSON.parse(sessionStorage.getItem(ONBOARDING_DRAFT_KEY) || 'null');
    return value && typeof value === 'object' && typeof value.operation_id === 'string' ? value : null;
  } catch (_) { return null; }
}

function savePendingDraft(value) {
  sessionStorage.setItem(ONBOARDING_DRAFT_KEY, JSON.stringify(value));
}

function clearPendingDraft() { sessionStorage.removeItem(ONBOARDING_DRAFT_KEY); }

function groupDnsAuthority(ctx, groupId) {
  const group = (ctx.webapp_groups || []).find((row) => String(row.id) === String(groupId));
  return Boolean(group?.can_manage_dns || (!group && ctx.can_create_webapp_group));
}

function stepList(operation) {
  const steps = [['app', 'WebApp'], ['address', 'Domain & DNS'], ['github', 'GitHub'], ['verify', 'Go live']];
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

function addressChoice(operation, ctx, update, groupId) {
  const wrap = h('div', {class: 'wizard-choice'});
  const message = h('div', {class: 'form-message', role: 'alert'});
  const label = h('input', {value: 'www', autocomplete: 'off'});
  const modeButtons = h('div', {class: 'choice-tabs', role: 'group', 'aria-label': 'Domain source'});
  const choices = h('div');
  const hostname = h('strong', {class: 'hostname-value', text: 'www.your-domain.com'});
  let mode = 'existing';
  let quote = null;
  const canManageDns = groupDnsAuthority(ctx, groupId);

  function selectMode(next) {
    mode = next;
    modeButtons.querySelectorAll('button').forEach((button) => {
      const active = button.dataset.mode === mode;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
    });
    renderMode();
  }

  const modes = [['existing', 'Use managed domain']];
  if (canManageDns) modes.push(['purchase', 'Buy new domain']);
  modes.forEach(([value, text]) => modeButtons.append(h('button', {
    class: `choice-tab ${value === mode ? 'active' : ''}`, type: 'button', 'data-mode': value,
    'aria-pressed': value === mode ? 'true' : 'false', onclick: () => selectMode(value),
  }, text)));

  async function renderMode() {
    choices.replaceChildren();
    message.textContent = '';
    if (mode === 'existing') {
      const domain = h('select', {}, h('option', {value: '', text: 'Select a domain'}));
      const refresh = h('button', {class: 'button ghost compact', type: 'button'}, icon('refresh'), 'Refresh domains');
      const updateHostname = () => {
        const domainName = domain.selectedOptions[0]?.dataset.name || 'your-domain.com';
        hostname.textContent = `${label.value.trim() || 'www'}.${domainName}`;
      };
      const loadManagedDomains = async () => {
        refresh.disabled = true;
        domain.replaceChildren(h('option', {value: '', text: 'Select a domain'}));
        try {
          listData(await api(`/api/dnsman/domain?group=${encodeURIComponent(groupId || '')}`))
            .filter((row) => row.status === 'active' && row.verified !== false)
            .forEach((row) => domain.append(h('option', {value: row.id, text: row.name, 'data-name': row.name})));
          if (domain.options.length === 1) message.textContent = canManageDns ?
            'No managed domains yet. Add or connect one, then refresh this list.' :
            'No managed domains are available. Ask a DNS administrator to add one, then refresh this list.';
          updateHostname();
        } catch (error) { message.textContent = error.message; }
        finally { refresh.disabled = false; }
      };
      refresh.addEventListener('click', loadManagedDomains);
      domain.addEventListener('change', updateHostname);
      label.addEventListener('input', updateHostname);
      choices.append(field('Managed domain', domain, 'Choose a domain already controlled by this group.'),
        h('div', {class: 'domain-actions'},
          canManageDns ? h('a', {
            class: 'button ghost compact', href: routeHref('domains'), target: '_blank', rel: 'noopener',
          }, icon('plus'), 'Add or connect a domain') : null,
          refresh),
        h('button', {class: 'button primary', type: 'button', onclick: () => {
          if (!domain.value) { message.textContent = 'Select a managed domain.'; return; }
          chooseStep(operation, {domain: Number(domain.value), label: label.value}, update, message);
        }}, 'Configure DNS and continue'));
      await loadManagedDomains();
      return;
    }
    const name = h('input', {placeholder: 'example.com', autocomplete: 'off'});
    const quoteButton = h('button', {class: 'button', type: 'button'}, 'Get live quote');
    const confirm = h('div');
    quoteButton.addEventListener('click', async () => {
      try {
        quote = await apiOnce('/api/dnsman/registrar/quote', {method: 'POST', body: JSON.stringify({
          group: groupId, domain: name.value, years: 1,
        })});
        hostname.textContent = `${label.value.trim() || 'www'}.${quote.name}`;
        confirm.replaceChildren(h('div', {class: 'callout warning'}, icon('alert'), h('div', {},
          h('strong', {text: `${quote.name} — ${quote.price} ${quote.currency}`}),
          h('p', {text: `Quote expires ${formatDate(quote.expires)}. Type the exact domain and price to authorize the purchase.`}))),
        field('Confirm domain', h('input', {id: 'purchase-domain', autocomplete: 'off'})),
        field('Confirm price', h('input', {id: 'purchase-price', inputmode: 'decimal', autocomplete: 'off'})),
        h('button', {class: 'button danger', type: 'button', onclick: async () => {
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
  wrap.append(h('div', {class: 'wizard-intro'}, icon('globe'), h('div', {},
    h('strong', {text: 'Choose the public address'}),
    h('p', {text: 'We create the DNS record automatically. HTTPS is handled automatically too.'}))),
  modeButtons, field('Subdomain', label, 'Use a concrete label such as www or app. Apex onboarding is intentionally refused.'),
  h('div', {class: 'hostname-preview'}, h('span', {text: 'Your WebApp will open at'}), hostname), choices, message);
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

function wizardChoice(operation, ctx, update, groupId) {
  const message = h('div', {class: 'form-message', role: 'alert'});
  if (operation.cursor === 'app') return h('div', {class: 'wizard-choice'},
    h('p', {text: 'Create or adopt the application profile using the frozen values above.'}),
    h('button', {class: 'button primary', onclick: () => chooseStep(operation, {}, update, message)}, 'Create application'), message);
  if (operation.cursor === 'address') return addressChoice(operation, ctx, update, groupId);
  if (operation.cursor === 'github') return githubChoice(operation, update);
  if (operation.cursor === 'verify') return h('div', {class: 'wizard-choice'},
    h('p', {text: 'Confirm that the public address serves this WebApp. DNS and HTTPS have already been configured.'}),
    h('button', {class: 'button primary', onclick: () => chooseStep(operation, {}, update, message)}, 'Check public address'), message);
  return h('div', {class: 'callout success'}, icon('check'), h('p', {text: 'Onboarding is complete.'}));
}

function onboardingPanel(initial, ctx, reloadApps, groupId) {
  let operation = initial;
  const root = h('section', {class: 'panel onboarding-panel'});
  const update = (next) => { operation = next.operation || next; render(); };
  async function reconcile() {
    try { update(await api(`/api/edge/webapp/onboarding/detail?operation=${encodeURIComponent(operation.operation_id)}`)); await reloadApps(); }
    catch (error) { root.querySelector('.form-message').textContent = error.message; }
  }
  function render() {
    const evidence = Object.entries(operation.evidence || {}).map(([key, value]) =>
      h('div', {}, h('dt', {text: key}), h('dd', {}, badge(value?.status || 'recorded'))));
    root.replaceChildren(...[h('div', {class: 'panel-heading'}, h('div', {},
      h('h2', {text: operation.profile?.display_name || operation.profile?.slug || 'New WebApp'}),
      h('p', {text: `${operation.status} · revision ${operation.revision}`})),
      h('button', {class: 'button compact', onclick: reconcile}, icon('refresh'), 'Reconcile status')),
    stepList(operation), operation.last_error ? h('div', {class: 'callout warning'}, icon('alert'), h('p', {text: operation.last_error})) : null,
    evidence.length ? h('details', {class: 'onboarding-evidence'}, h('summary', {text: 'Technical details'}), h('dl', {class: 'details'}, ...evidence)) : null,
    wizardChoice(operation, ctx, update, groupId), h('div', {class: 'form-message', role: 'alert'})].filter(Boolean));
  }
  render(); return root;
}

async function recoverPendingDraft(draft, ctx, mount, reloadApps) {
  if (!draft?.submitted || !draft.operation_id) return false;
  try {
    const operation = await api(`/api/edge/webapp/onboarding/detail?operation=${encodeURIComponent(draft.operation_id)}`);
    clearPendingDraft();
    await reloadApps();
    mount.replaceChildren(onboardingPanel(operation, ctx, reloadApps, operation.group.id));
    return true;
  } catch (_) { return false; }
}

async function startOnboarding(ctx, mount, reloadApps) {
  let draft = pendingDraft();
  if (await recoverPendingDraft(draft, ctx, mount, reloadApps)) return;
  let frozenPayload = draft?.submitted && draft.payload ? draft.payload : null;
  const restored = frozenPayload || draft || {};
  const groupOptions = [];
  if (ctx.can_create_webapp_group) groupOptions.push(h('option', {value: NEW_GROUP_VALUE, text: 'Create New Group'}));
  (ctx.webapp_groups || []).forEach((row) => groupOptions.push(h('option', {value: row.id, text: row.name})));
  const group = h('select', {}, ...groupOptions);
  const slug = h('input', {placeholder: 'customer-portal', autocomplete: 'off'});
  const name = h('input', {placeholder: 'Customer portal', autocomplete: 'off'});
  const environment = h('select', {}, ...['production', 'staging', 'preview', 'development'].map((value) => h('option', {value, text: value})));
  const bucket = h('select');
  const advanced = h('details', {class: 'wizard-advanced'}, h('summary', {text: 'Advanced'}), field('Release bucket', bucket, 'Storage is selected automatically when only one bucket is available.'));
  const message = h('div', {class: 'form-message', role: 'alert'});
  const submit = h('button', {class: 'button primary'}, frozenPayload ? 'Reconcile saved operation' : 'Continue to Domain & DNS');
  const startOver = h('button', {class: 'button ghost', type: 'button'}, 'Start over');
  let slugEdited = false;
  const slugify = (value) => value.toLowerCase().trim().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
  const controls = [group, slug, name, environment, bucket];
  function setFrozen(frozen) {
    controls.forEach((control) => { control.disabled = frozen; });
    submit.textContent = frozen ? 'Reconcile saved operation' : 'Continue to Domain & DNS';
  }
  function snapshot() {
    if (frozenPayload) return;
    if (!draft) draft = {operation_id: crypto.randomUUID()};
    draft = {...draft, submitted: false,
      group_intent: group.value === NEW_GROUP_VALUE ? 'new' : 'existing',
      group: group.value === NEW_GROUP_VALUE ? null : group.value,
      display_name: name.value, slug: slug.value, environment: environment.value,
      bucket: bucket.value};
    savePendingDraft(draft);
  }
  if (draft) {
    const restoredGroup = restored.group_intent === 'new' ? NEW_GROUP_VALUE : String(restored.group || '');
    if (frozenPayload && restoredGroup &&
        ![...group.options].some((option) => option.value === restoredGroup)) {
      group.append(h('option', {value: restoredGroup, text: restoredGroup === NEW_GROUP_VALUE ?
        'Saved new group' : `Saved group #${restoredGroup}`}));
    }
    if ([...group.options].some((option) => option.value === restoredGroup)) group.value = restoredGroup;
    name.value = restored.display_name || '';
    slug.value = restored.slug || '';
    environment.value = restored.environment || 'production';
    slugEdited = Boolean(restored.slug);
  }
  setFrozen(Boolean(frozenPayload));
  name.addEventListener('input', () => { if (!slugEdited) slug.value = slugify(name.value); snapshot(); });
  slug.addEventListener('input', () => { slugEdited = true; snapshot(); });
  environment.addEventListener('change', snapshot);
  async function loadOptions() {
    submit.disabled = true; message.textContent = '';
    try {
      if (!group.value) throw new Error('No eligible WebApp group is available.');
      const intent = group.value === NEW_GROUP_VALUE ? 'group_intent=new' :
        `group=${encodeURIComponent(group.value)}`;
      const data = await api(`/api/edge/webapp/onboarding/options?${intent}`);
      bucket.replaceChildren(...(data.buckets || []).map((value) => h('option', {value, text: value})));
      if (restored.bucket && [...bucket.options].some((option) => option.value === restored.bucket)) bucket.value = restored.bucket;
      advanced.hidden = bucket.options.length <= 1;
      if (!bucket.options.length) message.textContent = 'No release storage is configured for this installation.';
      submit.disabled = !bucket.options.length;
      snapshot();
      bucket.disabled = Boolean(frozenPayload);
    } catch (error) { message.textContent = error.message; }
  }
  group.addEventListener('change', () => { snapshot(); loadOptions(); });
  bucket.addEventListener('change', snapshot);
  startOver.addEventListener('click', () => {
    clearPendingDraft(); frozenPayload = null;
    draft = {operation_id: crypto.randomUUID(), submitted: false};
    setFrozen(false);
    group.value = ctx.can_create_webapp_group ? NEW_GROUP_VALUE : String(ctx.webapp_groups?.[0]?.id || '');
    name.value = ''; slug.value = ''; environment.value = 'production'; slugEdited = false;
    snapshot(); loadOptions();
  });
  if (frozenPayload) {
    bucket.replaceChildren(h('option', {value: frozenPayload.bucket, text: frozenPayload.bucket}));
    advanced.hidden = true;
    submit.disabled = false;
  } else loadOptions();
  const content = h('div', {class: 'wizard-form'},
    h('div', {class: 'wizard-progress'}, h('span', {class: 'current', text: '1 WebApp'}), h('span', {text: '2 Domain & DNS'}), h('span', {text: '3 GitHub'}), h('span', {text: '4 Go live'})),
    h('div', {class: 'wizard-intro'}, icon('deploy'), h('div', {}, h('strong', {text: 'Name the application'}), h('p', {text: 'Start with the WebApp itself. Domain and GitHub setup come next, one clear step at a time.'}))),
    field('Group', group), field('Display name', name, 'The human-friendly name shown in Admin.'),
    field('WebApp slug', slug, 'A short stable identifier, generated from the name.'), field('Environment', environment),
    advanced, message, h('div', {class: 'form-actions'}, startOver, submit));
  const close = openModal({title: 'Create WebApp', subtitle: 'A guided setup with DNS included.', content});
  submit.addEventListener('click', async () => {
    submit.disabled = true;
    try {
      if (!frozenPayload) {
        snapshot();
        const groupPayload = group.value === NEW_GROUP_VALUE ? {group_intent: 'new'} :
          {group: Number.parseInt(group.value, 10)};
        if (groupPayload.group !== undefined && (!Number.isInteger(groupPayload.group) || groupPayload.group <= 0)) {
          throw new Error('Select an eligible group.');
        }
        frozenPayload = {
          ...groupPayload, operation_id: draft.operation_id,
          slug: slug.value, display_name: name.value,
          environment: environment.value, bucket: bucket.value,
          github_repository: '', deployment_ref: 'main', build_output: 'dist',
        };
        draft = {operation_id: draft.operation_id, submitted: true, payload: frozenPayload};
        savePendingDraft(draft);
        setFrozen(true);
      }
      const result = await apiOnce('/api/edge/webapp/onboarding/create', {
        method: 'POST', body: JSON.stringify(frozenPayload),
      });
      let operation = result.operation;
      if (operation.cursor === 'app') {
        try {
          const next = await apiOnce('/api/edge/webapp/onboarding/choose', {method: 'POST', body: JSON.stringify({
            operation: operation.operation_id, revision: operation.revision, step: 'app', choice: {},
          })});
          operation = next.operation || next;
        } catch (_) {
          // The durable panel keeps the explicit App continuation available.
        }
      }
      clearPendingDraft();
      close(); mount.replaceChildren(onboardingPanel(
        operation, ctx, reloadApps, operation.group.id));
    } catch (error) {
      message.textContent = `${error.message} The submitted values are frozen. Reconcile the same operation, or Start over to abandon it.`;
      submit.disabled = false;
    }
  });
}

export async function webappsPage(ctx) {
  const root = h('div', {class: 'page'}); let linkedInspectorOpened = false;
  const onboarding = h('div', {class: 'onboarding-mount'});
  async function render() {
    const canManageDomains = ctx.capabilities.network || ctx.capabilities.manage_network ||
      (ctx.webapp_groups || []).some((group) => group.can_manage_dns);
    root.replaceChildren(pageHeader('Deployments', 'WebApps', 'Create an application, connect its domain, then deploy from GitHub.', [
      canManageDomains ? h('a', {class: 'button ghost', href: routeHref('domains')}, icon('globe'), 'Domains & DNS') : null,
      ctx.capabilities.manage_webapps ? h('button', {class: 'button primary', onclick: () => startOnboarding(ctx, onboarding, render)}, icon('plus'), 'Onboard WebApp') : null,
    ].filter(Boolean)), onboarding);
    const panel = h('section', {class: 'panel'}, h('div', {class: 'panel-heading'}, h('div', {}, h('h2', {text: 'Applications'}), h('p', {text: 'Manage onboarding and MOJO_DEPLOY_KEY without exposing unrelated API keys.'}))));
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
