import {api, apiOnce, badge, formatDate, h, icon, listData, pageHeader, statusTone} from '../../core.js';
import {openBusy, openInspector} from '../../components/overlays.js';
import {activityHref, decodeRouteState, restoreReturnLocation, returnLocation, routeHref} from '../../components/routes.js';
import {errorState, loadingState, permissionDeniedState} from '../../components/views.js';

const TERMINAL = new Set(['succeeded', 'failed', 'cancelled']);
const DEEP_LINKS = {
  hosting_dns: ['#/domains', 'Open Domains & DNS'],
  hosting_vhosts: ['#/vhosts', 'Open Vhosts'],
  edge_fleet: ['#/vhosts', 'Open hosting configuration'],
  webapp_keys: ['#/webapps', 'Manage WebApp keys'],
};
const READINESS_SEVERITY = ['fail', 'pending', 'warn', 'pass'];

function operatorChecks(checks = []) {
  return checks.filter((check) => check.code !== 'django.static_directories' &&
    (check.code !== 'django.local_request' || check.details?.target_source === 'configured_static'));
}

function operatorReport(report) {
  if (!report?.sections) return report;
  const summary = {pass: 0, warn: 0, fail: 0, pending: 0};
  const sections = report.sections.map((section) => {
    const checks = operatorChecks(section.checks);
    checks.forEach((check) => { if (check.status in summary) summary[check.status] += 1; });
    const status = READINESS_SEVERITY.find((value) => checks.some((check) => check.status === value)) || 'pass';
    return {...section, checks, status};
  }).filter((section) => section.checks.length);
  const overall = READINESS_SEVERITY.find((value) => summary[value]) || 'pass';
  return {...report, sections, summary, overall};
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
  if (!report?.sections?.length) return h('div', {class: 'empty'}, h('p', {text: 'Run checks to create a readiness report.'}));
  const byCode = new Map((options?.sections || []).map((entry) => [entry.code, entry]));
  return h('div', {class: 'setup-sections'}, ...report.sections.map((section) => {
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
  return h('form', {class: 'setup-choice', onsubmit: async (event) => {
    event.preventDefault(); button.disabled = true; message.textContent = '';
    const choice = {};
    fields.forEach(({name, input}) => { choice[name] = input.type === 'checkbox' ? input.checked : input.value; });
    try { await actions.choose(current, choice); } catch (error) { message.textContent = error.message; button.disabled = unavailable; }
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
    h('span', {text: step.label}), badge(step.state.replaceAll('_', ' '), statusTone(step.state))));
  const returnTarget = restoreReturnLocation(decodeRouteState().state.return) || routeHref('dashboard');
  return h('section', {class: 'panel setup-operation'},
    h('div', {class: 'panel-heading'}, h('div', {}, h('h2', {text: `${operation.mode === 'fix' ? 'Fix' : 'Check'} operation`}),
      h('p', {text: current ? current.label : 'Operation finished'})), badge(operation.status.replaceAll('_', ' '), statusTone(operation.status))),
    h('div', {class: 'operation-progress'}, h('progress', {max: '100', value: progress, 'aria-label': 'Setup progress'}), h('span', {text: `${progress}%`})),
    h('div', {class: 'operation-layout'},
      h('ol', {class: 'step-list'}, ...stepRows),
      h('div', {class: 'operation-main'}, choiceForm(operation, actions, suggestions),
        !terminal && operation.status !== 'waiting_for_choice' ? h('div', {class: 'running-state'}, icon('activity'), h('div', {}, h('strong', {text: 'Reconciling authoritative state'}), h('p', {text: 'The operation advances one durable step at a time. It is safe to close and resume.'}))) : null,
        h('div', {class: 'form-actions'}, cancellable ? h('button', {class: 'button ghost', onclick: actions.cancel}, 'Cancel operation') : null,
          terminal ? h('a', {class: 'button ghost', href: returnTarget}, 'Return to Dashboard') : null,
          operation.status === 'succeeded' ? h('a', {class: 'button primary', href: routeHref('webapps')}, 'Onboard WebApp') : null))),
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

  async function runAction(title, detail, task) {
    if (activeAction || signal?.aborted) return;
    activeAction = true; actionError = null;
    const busy = openBusy({title, detail}); render();
    try { await task(busy); }
    catch (error) {
      if (error?.code !== 'fresh_auth_required') actionError = error;
    } finally {
      busy.close(); activeAction = false; render();
    }
  }

  const actions = {
    create: async (mode, section = '') => {
      await runAction(mode === 'fix' ? 'Repairing System Setup' : 'Checking System Setup',
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
      await runAction('Saving Setup choice', step.label, async (busy) => {
        operation = await apiOnce('/api/account/admin/setup/choose', {method: 'POST', signal, body: JSON.stringify({
          operation: operation.id, step_id: step.id, definition_version: step.definition_version,
          choice_revision: step.choice_revision, choice,
        })});
        await prepareChoice(); render(); await drive(busy);
      });
    },
    cancel: async () => {
      await runAction('Cancelling Setup operation', 'Waiting for the durable cancellation result', async () => {
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
      await runAction('Resuming System Setup', operation.current_step?.label || 'Reconciling the active operation', drive);
    }
  } catch (error) { root.replaceChildren(errorState(error)); }
  root.dispose = () => { cancelled = true; };
  return root;
}

function evidenceSummary(name, data) {
  if (name === 'api') return data.configured
    ? [`django-mojo ${data.probe?.version || data.django_mojo_version || 'observed'}`, data.public_origin || 'Public API configured']
    : ['Public API not configured', 'Set the protected BASE_URL in System Setup.'];
  if (name === 'fleet') {
    const rows = data.runners || [];
    return [`${rows.length} live edge runner${rows.length === 1 ? '' : 's'}`,
      rows.length ? rows.map((row) => row.runner).join(' · ') : 'No current edge heartbeat evidence.'];
  }
  if (name === 'jobs') {
    const jobs = data.jobs || {};
    return [`${jobs.pending || 0} pending · ${jobs.running || 0} running`,
      `${jobs.failed || 0} recorded failures · Scheduler ${data.scheduler_active ? 'active' : 'inactive'}`];
  }
  if (name === 'sanity') {
    const checks = data.checks || []; const passing = checks.filter((row) => row.ok).length;
    return [`${passing} of ${checks.length} checks passing`, checks.filter((row) => !row.ok).map((row) => row.name).join(' · ') || 'Core application checks passed.'];
  }
  if (name === 'database') return [data.reachable ? 'Database reachable' : 'Database unavailable', data.vendor || 'No vendor evidence'];
  if (name === 'redis') return [data.reachable ? 'Redis reachable' : 'Redis unavailable', 'Cache and coordination service'];
  if (name === 'certificates') return [`${data.counts?.active || 0} active certificates`, `${data.expiring_within_30_days || 0} expiring within 30 days`];
  if (name === 'security') {
    const disabled = data.secure_posture?.disabled || [];
    return [`${data.open_incidents?.count ?? '—'} open incidents`, disabled.length
      ? `Disabled controls: ${disabled.map((value) => value.replaceAll('_', ' ')).join(' · ')}`
      : (data.monitoring_delivery?.present ? 'Secure redirect, cookies, HSTS, and monitoring are enabled.' : 'Monitoring delivery proof is missing')];
  }
  if (name === 'webapps') {
    const rollup = data.rollup || {};
    const health = rollup.current_health || {};
    return [`${health.healthy || 0} of ${rollup.configured_origins || 0} configured origins healthy`,
      `${rollup.count || 0} apps · ${rollup.deployment_keys?.active || 0} active deploy keys · ${rollup.onboarding?.succeeded || 0} completed onboarding`];
  }
  return ['Evidence available', 'Open the response only when troubleshooting.'];
}

function evidenceOwner(name) {
  if (name === 'fleet') return [routeHref('platform', {focus: 'fleet'}), 'View fleet'];
  if (name === 'webapps') return [routeHref('webapps'), 'Open Web Apps'];
  if (name === 'security') return [activityHref('incidents'), 'Open incidents'];
  if (name === 'api') return [routeHref('setup', {focus: 'django.base_url', return: returnLocation()}), 'Configure API'];
  return null;
}

function evidenceCard(name, section) {
  const title = name.replaceAll('_', ' ').replace(/^./, (value) => value.toUpperCase());
  const data = section?.data || {};
  const [value, detail] = evidenceSummary(name, data);
  const owner = evidenceOwner(name);
  return h('section', {class: 'panel platform-evidence', 'data-focus': name, tabindex: '-1'},
    h('div', {class: 'panel-heading'}, h('div', {}, h('h2', {text: title}),
      h('p', {text: section?.reason || `Observed ${section?.observed_at ? formatDate(section.observed_at) : 'now'}`})),
      badge(String(section?.status || 'unavailable').toUpperCase(), statusTone(section?.status))),
    h('div', {class: 'evidence-summary'}, h('strong', {text: value}), h('p', {text: detail}),
      owner ? h('a', {class: 'button ghost compact evidence-owner', href: owner[0]}, owner[1]) : null),
    h('details', {class: 'evidence-disclosure'}, h('summary', {}, icon('activity'), h('span', {text: 'View raw evidence'})),
      h('pre', {class: 'evidence-json', text: JSON.stringify(data, null, 2)})));
}

function platformDestinations(ctx) {
  const advanced = ctx.features?.advanced?.enabled === true;
  return h('section', {class: 'platform-destinations'},
    ctx.capabilities.setup ? h('a', {class: 'destination-card', href: routeHref('setup')},
      icon('settings'), h('div', {}, h('strong', {text: 'System Setup'}),
        h('span', {text: 'Check or repair installation dependencies as a literal superuser.'})), icon('chevron')) : null,
    advanced ? h('a', {class: 'destination-card', href: routeHref('advanced')},
      icon('settings'), h('div', {}, h('strong', {text: 'Advanced diagnostics'}),
        h('span', {text: 'Open bounded hosting and inventory evidence for expert troubleshooting.'})), icon('chevron')) : null);
}

function openDeployment(row) {
  const content = h('div', {class: 'deployment-inspector'},
    h('dl', {class: 'details'},
      h('div', {}, h('dt', {text: 'Commit'}), h('dd', {class: 'mono', text: row.sha})),
      h('div', {}, h('dt', {text: 'Status'}), h('dd', {text: row.status})),
      h('div', {}, h('dt', {text: 'Source'}), h('dd', {text: row.source || 'unknown'})),
      h('div', {}, h('dt', {text: 'Framework'}), h('dd', {text: row.framework_version || 'unknown'}))),
    h('a', {class: 'related-record', href: activityHref('events', {}, {
      search: row.id, return: returnLocation(),
    })}, h('strong', {text: 'Related deployment activity'}), icon('chevron')));
  openInspector({title: `Deployment · ${row.sha?.slice(0, 10) || row.id}`, subtitle: row.id, content, wide: true});
}

export async function platformPage(ctx, route = 'platform') {
  const root = h('div', {class: 'page'});
  async function load() {
    if (!ctx.features?.platform?.capabilities?.view) {
      root.replaceChildren(pageHeader('Control plane', 'Platform', 'Platform health is independently permissioned.'),
        permissionDeniedState('Your role cannot read Platform health or deployments.'), platformDestinations(ctx));
      return;
    }
    const report = await api('/api/account/admin/platform');
    const sections = report.sections || {};
    const deployments = sections.deployments?.data?.items || [];
    const actions = (row) => ctx.capabilities.manage_platform ?
      ['retry', 'verify', 'converge'].map((action) => h('button', {class: 'button compact', onclick: async (event) => {
        event.currentTarget.disabled = true;
        try { await api(`/api/account/admin/platform/deploy/${action}`, {method: 'POST', body: JSON.stringify({deployment: row.id})}); await load(); }
        catch (error) { root.prepend(h('div', {class: 'error-state'}, icon('alert'), h('p', {text: error.message}))); }
      }}, action === 'retry' ? 'Retry same SHA' : action.replace(/^./, (value) => value.toUpperCase()))) : [];
    if (route === 'deployments') {
      root.replaceChildren(pageHeader('Platform control plane', 'Deployments', 'Immutable attempts, UUID-bound proof, and same-SHA recovery actions.'),
        ...deployments.map((row) => h('section', {class: 'panel deployment-row'},
          h('div', {class: 'panel-heading'}, h('div', {}, h('h2', {class: 'mono', text: row.sha}), h('p', {text: row.id})), badge(row.status, statusTone(row.status))),
          h('p', {text: `${row.frozen_roster?.length || 0} frozen edge runner(s) · ${row.node_evidence?.length || 0} current proof row(s)`}),
          h('div', {class: 'form-actions'}, h('button', {class: 'button compact', onclick: () => openDeployment(row)}, 'Inspect'), ...actions(row)))),
        ...(!deployments.length ? [h('div', {class: 'empty'}, h('p', {text: 'No platform deployment attempts have been recorded.'}))] : []));
      const inspector = decodeRouteState().state.inspector;
      const linked = inspector && deployments.find((row) => String(row.id) === String(inspector));
      if (linked) openDeployment(linked);
      return;
    }
    root.replaceChildren(pageHeader('Platform control plane', 'Platform health', 'Bounded evidence for API, fleet, data services, certificates, security, and WebApps.', [
      h('button', {class: 'button ghost', onclick: load}, icon('refresh'), 'Refresh evidence'),
    ]), platformDestinations(ctx), h('div', {class: 'health-grid'}, ...Object.entries(sections).filter(([name]) => name !== 'deployments').map(([name, section]) => evidenceCard(name, section))));
    const focus = decodeRouteState().state.focus;
    const target = focus ? root.querySelector(`[data-focus="${CSS.escape(focus)}"]`) : null;
    if (target) requestAnimationFrame(() => target.focus({preventScroll: false}));
  }
  try { await load(); } catch (error) { root.replaceChildren(h('div', {class: 'error-state'}, icon('alert'), h('p', {text: error.message}))); }
  return root;
}
