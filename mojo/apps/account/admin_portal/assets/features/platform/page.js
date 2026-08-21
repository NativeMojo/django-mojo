import {api, apiOnce, badge, formatDate, h, icon, listData, pageHeader, statusTone} from '../../core.js';
import {runAction} from '../../components/actions.js';
import {openBusy} from '../../components/overlays.js';
import {decodeRouteState, restoreReturnLocation, routeHref} from '../../components/routes.js';
import {errorState, loadingState} from '../../components/views.js';

const TERMINAL = new Set(['succeeded', 'failed', 'cancelled']);
const DEEP_LINKS = {
  hosting_dns: ['#/domains', 'Open Domains & DNS'],
  hosting_vhosts: ['#/vhosts', 'Open Vhosts'],
  edge_fleet: ['#/vhosts', 'Open hosting configuration'],
  webapp_keys: ['#/deployments', 'Manage WebApp keys'],
};
const READINESS_SEVERITY = ['fail', 'pending', 'warn', 'pass'];
const STEP_STATE_LABELS = {
  planned: 'Up next',
  waiting_for_choice: 'Needs input',
  mutation_attempted: 'Applying changes',
  reconciling: 'Verifying changes',
  proven: 'Complete',
  failed: 'Needs attention',
};

function operatorChecks(checks = []) {
  if (!Array.isArray(checks)) return [];
  return checks.filter(isReadinessCheck).map((check) => ({
    ...check,
    code: typeof check.code === 'string' && check.code ? check.code : 'unknown',
    explanation: typeof check.explanation === 'string' && check.explanation
      ? check.explanation : 'Readiness check',
    remediation: typeof check.remediation === 'string' ? check.remediation : '',
  })).filter((check) => check.code !== 'django.static_directories' &&
    (check.code !== 'django.local_request' || check.details?.target_source === 'configured_static'));
}

function isReadinessObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function isReadinessCheck(value) {
  return isReadinessObject(value) && READINESS_SEVERITY.includes(value.status);
}

function operatorReport(report) {
  const source = isReadinessObject(report) ? report : {};
  const hasSections = Array.isArray(source.sections);
  const rawSections = hasSections ? source.sections : [];
  let truncated = source.truncated === true || !hasSections;
  const summary = {pass: 0, warn: 0, fail: 0, pending: 0};
  const sections = [];
  rawSections.forEach((section) => {
    if (!isReadinessObject(section)) { truncated = true; return; }
    const rawChecks = Array.isArray(section.checks) ? section.checks : [];
    if (!Array.isArray(section.checks) || rawChecks.some((check) => !isReadinessCheck(check))) truncated = true;
    const checks = operatorChecks(rawChecks);
    checks.forEach((check) => { if (check.status in summary) summary[check.status] += 1; });
    const status = READINESS_SEVERITY.find((value) => checks.some((check) => check.status === value)) || 'pass';
    if (checks.length) sections.push({
      ...section,
      code: typeof section.code === 'string' && section.code ? section.code : 'unknown',
      label: typeof section.label === 'string' && section.label ? section.label : 'Readiness section',
      checks,
      status,
    });
  });
  const overall = READINESS_SEVERITY.find((value) => summary[value]) || 'pass';
  return {...source, sections, summary, overall, truncated};
}

function checkAction(section, check, config, actions) {
  if (['pass', 'pending'].includes(check.status)) return null;
  if (check.code === 'django.base_url' && check.fixable) {
    return h('button', {class: 'button compact', onclick: () => actions.create('fix', 'django')}, 'Configure BASE_URL');
  }
  if (check.fixable && config.fixable) {
    return h('button', {class: 'button compact', onclick: () => actions.create('fix', section.code)}, 'Fix');
  }
  const deepLink = DEEP_LINKS[section.code];
  if (deepLink) return h('a', {class: 'button ghost compact', href: deepLink[0]}, deepLink[1]);
  if (check.code === 'django.local_request' && ['configured_static', 'default_80'].includes(check.details?.target_source)) {
    return h('span', {class: 'owner-guidance', text: 'Change deployment setting'});
  }
  return null;
}

function reportView(report, options, actions) {
  const partial = report?.truncated === true ? h('div', {class: 'callout warning'}, icon('alert'),
    h('p', {text: 'Only part of this readiness report could be shown. Truncated or malformed entries were omitted; rerun the checks for a complete report.'})) : null;
  if (!report?.sections?.length) return h('div', {}, partial,
    h('div', {class: 'empty'}, h('p', {text: partial ? 'No valid readiness checks were returned.' : 'Run checks to create a readiness report.'})));
  const byCode = new Map((options?.sections || []).map((entry) => [entry.code, entry]));
  return h('div', {class: 'setup-sections'}, partial, ...report.sections.map((section) => {
    const config = byCode.get(section.code) || {};
    const sectionActions = h('div', {class: 'section-actions'},
      h('button', {class: 'button ghost compact', onclick: () => actions.create('check', section.code)}, icon('refresh'), 'Check'));
    return h('section', {class: 'panel setup-section', 'data-section': section.code, 'data-focus': section.code},
      h('div', {class: 'panel-heading'}, h('div', {}, h('div', {class: 'heading-line'}, h('h2', {text: section.label}), badge(section.status.toUpperCase(), statusTone(section.status))),
        h('p', {text: `${section.checks.length} readiness checks`})), sectionActions),
      h('div', {class: 'check-list'}, ...section.checks.map((check) =>
        h('article', {class: 'check-row', 'data-check': check.code, 'data-focus': check.code},
          h('div', {class: `status-dot ${statusTone(check.status)}`, title: check.status}),
          h('div', {}, h('strong', {text: check.explanation}), check.remediation ? h('p', {text: check.remediation}) : null,
            h('details', {class: 'check-technical'}, h('summary', {text: 'Technical details'}),
              h('small', {class: 'mono', text: check.code}),
              check.details ? h('pre', {class: 'evidence-json', text: JSON.stringify(check.details, null, 2)}) : null)),
          h('div', {class: 'check-actions'}, badge(check.status.toUpperCase(), statusTone(check.status)),
            checkAction(section, check, config, actions))))));
  }));
}

function readinessStrip(report) {
  const summary = report?.summary || {pass: 0, warn: 0, fail: 0, pending: 0};
  return h('section', {class: 'readiness-strip', 'aria-label': 'Readiness summary'},
    h('div', {class: 'readiness-overall'}, h('span', {text: 'Overall readiness'}), badge(String(report?.overall || 'pending').toUpperCase(), statusTone(report?.overall))),
    ...['pass', 'warn', 'fail', 'pending'].map((key) => h('div', {class: 'readiness-stat'}, h('strong', {text: String(summary[key] || 0)}), h('span', {text: key}))));
}

// Whose AWS estate this is, stated once at the top of the page an operator
// opens to change the installation. A missing `infrastructure` key is an older
// server that predates the switch, and that is a managed install.
function infrastructureNote(ctx) {
  const managed = ctx?.infrastructure?.managed !== false;
  return h('p', {class: 'infrastructure-note', text: managed
    ? 'Infrastructure: managed by this portal'
    : 'Infrastructure: external — AWS resources are managed by your infrastructure team\'s IaC, and this portal does not change them.'});
}

// Shared with the Dashboard's public-API setup modal, which prefills the same
// suggestion this page's choice form shows.
export async function suggestedBaseUrl(signal) {
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

function isS3Choice(step) {
  return step?.id === 'section:aws_s3' || step?.section === 'aws_s3';
}

function choiceField(name, spec, required, suggestions, current) {
  const id = `choice-${name}`;
  const s3 = isS3Choice(current);
  const label = s3 && name === 'bucket' ? 'Existing S3 bucket'
    : s3 && name === 'adopt_existing' ? 'Use this bucket for private system media'
      : name.replaceAll('_', ' ').replace(/^./, (value) => value.toUpperCase());
  if (spec.type === 'boolean') {
    const input = h('input', {id, name, type: 'checkbox', required: required || null,
      checked: spec.enum?.length === 1 ? Boolean(spec.enum[0]) : false});
    return {name, input, node: h('label', {class: 'check-field'}, input, h('span', {text: label}))};
  }
  if (Array.isArray(spec.enum)) {
    const input = h('select', {id, name, required: required || null},
      h('option', {value: '', text: spec.enum.length ? `Choose ${label.toLowerCase()}` : 'No choices discovered'}),
      ...spec.enum.map((value) => h('option', {value, text: value})));
    input.disabled = !spec.enum.length;
    return {name, input, node: h('label', {class: 'field'}, h('span', {text: label}), input)};
  }
  const type = spec.format === 'email' ? 'email' : spec.format === 'https-origin' ? 'url' : 'text';
  const input = h('input', {id, name, type, value: suggestions[name], required: required || null, autocomplete: 'off'});
  return {name, input, node: h('label', {class: 'field'}, h('span', {text: label}), input)};
}

function choiceForm(operation, actions, suggestions) {
  const current = operation.current_step;
  const schema = current?.choice_schema;
  if (operation.status !== 'waiting_for_choice' || !schema?.properties) return null;
  const required = new Set(schema.required || []);
  const fields = Object.entries(schema.properties).map(([name, spec]) => choiceField(name, spec, required.has(name), suggestions, current));
  const unavailable = fields.some((field) => field.input.tagName === 'SELECT' && field.input.disabled);
  const message = h('div', {class: 'form-message', role: 'alert'}, unavailable ? 'No suitable existing resource was discovered. Repair provider access, then cancel and rerun this section.' : '');
  const button = h('button', {class: 'button primary', type: 'submit', disabled: unavailable}, icon('check'),
    isS3Choice(current) ? 'Use this bucket' : 'Save and continue');
  // † The whole operation view — this form and its submit button with it — is
  // rebuilt by the render() that setupAction fires before the request even
  // leaves, so a pending state pinned to `button` would be detached inside a
  // frame. The affordance is the busy scrim that setupAction opens.
  return h('form', {class: 'setup-choice', onsubmit: (event) => {
    event.preventDefault(); message.textContent = '';
    const choice = {};
    fields.forEach(({name, input}) => { choice[name] = input.type === 'checkbox' ? input.checked : input.value; });
    // setupAction reports its own failures through the page's errorState.
    return runAction(null, () => actions.choose(current, choice),
      {key: `setup-choice:${current.id}`});
  }}, h('div', {class: 'choice-intro'}, h('strong', {text: current.label}), h('p', {text: isS3Choice(current)
    ? 'Choose an existing bucket from this AWS account. Setup verifies it before making changes, preserves its objects and unrelated configuration, and configures the private media access django-mojo needs.'
    : current.kind === 'base_url' && suggestions.base_url
      ? `Confirm the detected public API address ${suggestions.base_url}. It is saved only after you continue.`
      : 'Only the choice fields declared by the setup service are accepted. Secrets are never collected here.'})),
  ...fields.map((field) => field.node), message, button);
}

function operationView(operation, actions, suggestions) {
  const current = operation.current_step;
  const progress = operation.steps?.length ? Math.round((operation.cursor / operation.steps.length) * 100) : 100;
  const terminal = TERMINAL.has(operation.status);
  const cancellable = !terminal && !['mutation_attempted', 'reconciling'].includes(current?.state);
  const stepRows = (operation.steps || []).map((step, index) => h('li', {class: index === operation.cursor ? 'active' : ''},
    h('span', {class: `step-marker ${statusTone(step.state)}`}, step.state === 'proven' ? icon('check') : String(index + 1)),
    h('span', {text: step.label}), badge(STEP_STATE_LABELS[step.state] || step.state.replaceAll('_', ' '), statusTone(step.state))));
  const returnTarget = restoreReturnLocation(decodeRouteState().state.return) || routeHref('dashboard');
  const terminalOutcome = operation.status === 'succeeded'
    ? ['Changes applied and verified', 'Setup reran authoritative readiness checks and confirmed this operation.']
    : operation.status === 'failed'
      ? ['Setup could not verify every change', 'Review the remaining checks below for the exact next action.']
      : operation.status === 'cancelled'
        ? ['Setup operation cancelled', 'No further steps will run for this operation.'] : null;
  return h('section', {class: 'panel setup-operation'},
    h('div', {class: 'panel-heading'}, h('div', {}, h('h2', {text: `${operation.mode === 'fix' ? 'Fix' : 'Check'} operation`}),
      h('p', {text: current ? current.label : 'Operation finished'})), badge(operation.status.replaceAll('_', ' '), statusTone(operation.status))),
    h('div', {class: 'operation-progress'}, h('progress', {max: '100', value: progress, 'aria-label': 'Setup progress'}), h('span', {text: `${progress}%`})),
    h('div', {class: 'operation-layout'},
      h('ol', {class: 'step-list'}, ...stepRows),
      h('div', {class: 'operation-main'}, choiceForm(operation, actions, suggestions),
        terminalOutcome ? h('div', {class: `running-state ${statusTone(operation.status)}`},
          operation.status === 'succeeded' ? icon('check') : icon('activity'),
          h('div', {}, h('strong', {text: terminalOutcome[0]}), h('p', {text: terminalOutcome[1]}))) : null,
        !terminal && operation.status !== 'waiting_for_choice' ? h('div', {class: 'running-state'}, icon('activity'), h('div', {}, h('strong', {text: 'Reconciling authoritative state'}), h('p', {text: 'The operation advances one durable step at a time. It is safe to close and resume.'}))) : null,
        h('div', {class: 'form-actions'}, cancellable ? h('button', {class: 'button ghost', onclick: () => actions.cancel()}, 'Cancel operation') : null,
          terminal ? h('a', {class: 'button ghost', href: returnTarget}, 'Return to Dashboard') : null,
          operation.status === 'succeeded' ? h('a', {class: 'button primary', href: routeHref('deployments')}, 'Onboard WebApp') : null))),
    h('details', {class: 'operation-log'}, h('summary', {text: 'Technical details'}),
      h('ol', {}, ...(operation.log || []).map((entry) => h('li', {}, h('time', {text: formatDate(entry.at)}), h('span', {text: entry.message}))))));
}

export async function setupPage(ctx, signal = null) {
  const root = h('div', {class: 'page'}, loadingState('Loading System Setup'));
  let report; let options; let operation; let driving = false; let cancelled = false;
  let activeAction = false; let replayKey = null; let actionError = null;
  let routeFocusApplied = false;
  const suggestions = {base_url: ''};

  async function prepareChoice() {
    const current = operation?.current_step;
    const bucket = current?.choice_schema?.properties?.bucket;
    if (operation?.status !== 'waiting_for_choice' || !isS3Choice(current) || Array.isArray(bucket?.enum)) return;
    let names = [];
    try {
      names = listData(await api('/api/aws/s3/bucket', {signal}))
        .map((row) => typeof row === 'string' ? row : row?.name)
        .filter((name) => typeof name === 'string' && name.length)
        .sort((left, right) => left.localeCompare(right));
    } catch (_error) { /* The empty enum renders actionable provider guidance. */ }
    operation = {...operation, current_step: {...current, choice_schema: {
      ...current.choice_schema,
      properties: {...current.choice_schema.properties,
        bucket: {...bucket, type: 'string', enum: [...new Set(names)]},
        adopt_existing: {type: 'boolean', enum: [true]},
      },
      required: [...new Set([...(current.choice_schema.required || []), 'bucket', 'adopt_existing'])],
    }}};
  }

  async function refreshReport() { report = operatorReport(await api('/api/account/admin/setup/readiness', {signal})); render(); }

  function wait(milliseconds) {
    return new Promise((resolve) => {
      const timeout = setTimeout(resolve, milliseconds);
      signal?.addEventListener('abort', () => { clearTimeout(timeout); resolve(); }, {once: true});
    });
  }

  async function drive(busy = null) {
    if (driving || !operation || TERMINAL.has(operation.status)) return;
    driving = true; cancelled = false;
    try {
      await prepareChoice();
      render();
      for (let count = 0; count < 80 && !cancelled && !signal?.aborted; count += 1) {
        if (TERMINAL.has(operation.status) || operation.status === 'waiting_for_choice') break;
        busy?.update({title: operation.mode === 'fix' ? 'Repairing System Setup' : 'Checking System Setup',
          detail: operation.current_step?.label || 'Reconciling authoritative state',
          progress: operation.steps?.length ? Math.round((operation.cursor / operation.steps.length) * 100) : null});
        operation = await api('/api/account/admin/setup/advance', {method: 'POST', signal, body: JSON.stringify({operation: operation.id})});
        if (operation.report?.sections) report = operatorReport(operation.report);
        await prepareChoice();
        render();
        if (!TERMINAL.has(operation.status) && operation.status !== 'waiting_for_choice') await wait(250);
      }
      if (operation?.report?.sections) report = operatorReport(operation.report);
      if (TERMINAL.has(operation?.status)) await refreshReport();
    } finally { driving = false; render(); }
  }

  // The local re-entry guard, error capture and 440 special-case that used to
  // live here are the shared wrapper's job now. What stays is scrim OWNERSHIP:
  // `drive` reports its progress through `busy.update`, so this page has to
  // hold the handle rather than let runAction open and close one it cannot see.
  // Every setup action is one operation on one installation, so they share a
  // guard key — a second click while one is driving returns the first promise.
  function setupAction(title, detail, task) {
    if (signal?.aborted) return undefined;
    return runAction(null, async () => {
      activeAction = true; actionError = null;
      const busy = openBusy({title, detail}); render();
      try { await task(busy); }
      finally { activeAction = false; busy.close(); render(); }
    }, {
      key: 'setup-operation',
      onError: (error) => { actionError = error; render(); },
    });
  }

  const actions = {
    create: async (mode, section = '') => {
      await setupAction(mode === 'fix' ? 'Repairing System Setup' : 'Checking System Setup',
        section ? `Preparing ${section.replaceAll('_', ' ')}` : 'Preparing the durable operation', async (busy) => {
          replayKey = replayKey || crypto.randomUUID();
          try {
            operation = await apiOnce('/api/account/admin/setup/create', {method: 'POST', signal, body: JSON.stringify({mode, section, replay_key: replayKey})});
          } catch (error) {
            if (signal?.aborted || error?.code === 'fresh_auth_required') throw error;
            options = await api('/api/account/admin/setup/options', {signal});
            const reconciled = mode === 'fix' ? options.active_fix : null;
            if (!reconciled) throw new Error(`${error.message} The result is uncertain; refresh Setup before trying again.`);
            operation = await api(`/api/account/admin/setup/detail?operation=${encodeURIComponent(reconciled.id)}`, {signal});
          }
          await prepareChoice(); render(); await drive(busy);
          if (TERMINAL.has(operation?.status)) replayKey = null;
        });
    },
    choose: async (step, choice) => {
      await setupAction('Saving Setup choice', step.label, async (busy) => {
        operation = await apiOnce('/api/account/admin/setup/choose', {method: 'POST', signal, body: JSON.stringify({
          operation: operation.id, step_id: step.id, definition_version: step.definition_version,
          choice_revision: step.choice_revision, choice,
        })});
        await prepareChoice(); render(); await drive(busy);
      });
    },
    cancel: async () => {
      await setupAction('Cancelling Setup operation', 'Waiting for the durable cancellation result', async () => {
        cancelled = true;
        operation = await apiOnce('/api/account/admin/setup/cancel', {method: 'POST', signal, body: JSON.stringify({operation: operation.id})});
        replayKey = null; render();
      });
    },
  };

  function render() {
    root.replaceChildren(...[
      pageHeader('Installation control plane', 'System Setup', 'Configure a new installation, repair partial setup, and prove every dependency from one place.', [
        h('button', {class: 'button ghost', disabled: activeAction || driving, onclick: () => actions.create('check')}, icon('refresh'), 'Run all checks'),
        h('button', {class: 'button primary', disabled: activeAction || driving || (operation && !TERMINAL.has(operation.status)), onclick: () => actions.create('fix')}, icon('settings'), 'Fix all'),
      ]),
      infrastructureNote(ctx),
      actionError ? errorState(actionError) : null,
      report ? readinessStrip(report) : null,
      operation ? operationView(operation, actions, suggestions) : null,
      reportView(report, options, actions),
    ].filter(Boolean));
    const focus = decodeRouteState().state.focus;
    const target = focus ? root.querySelector(`[data-focus="${CSS.escape(focus)}"]`) : null;
    if (target && !routeFocusApplied) requestAnimationFrame(() => {
      routeFocusApplied = true;
      target.tabIndex = -1; target.focus({preventScroll: false});
    });
    const choice = root.querySelector('.setup-choice');
    if (choice && !choice.contains(document.activeElement)) requestAnimationFrame(() => choice.querySelector('input, select, button')?.focus({preventScroll: true}));
  }

  render();
  try {
    [options, report, suggestions.base_url] = await Promise.all([
      api('/api/account/admin/setup/options', {signal}),
      api('/api/account/admin/setup/readiness', {signal}),
      suggestedBaseUrl(signal),
    ]);
    report = operatorReport(report);
    operation = options.active_fix || null;
    await prepareChoice();
    render();
    if (operation && !TERMINAL.has(operation.status) && operation.status !== 'waiting_for_choice') {
      await setupAction('Resuming System Setup', operation.current_step?.label || 'Reconciling the active operation', drive);
    }
  } catch (error) { root.replaceChildren(errorState(error)); }
  root.dispose = () => { cancelled = true; };
  return root;
}
