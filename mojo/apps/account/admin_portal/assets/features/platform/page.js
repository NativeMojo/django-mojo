import {api, badge, h, icon, pageHeader, statusTone} from '../../core.js';
import {openInspector} from '../../components/overlays.js';
import {activityHref, decodeRouteState, returnLocation, routeHref} from '../../components/routes.js';
import {permissionDeniedState} from '../../components/views.js';

const TERMINAL = new Set(['succeeded', 'failed', 'cancelled']);
const DEEP_LINKS = {
  hosting_dns: ['#/domains', 'Open Domains & DNS'],
  hosting_vhosts: ['#/vhosts', 'Open Vhosts'],
  edge_fleet: ['#/vhosts', 'Open hosting configuration'],
  webapp_keys: ['#/webapps', 'Manage WebApp keys'],
};

function reportView(report, options, actions) {
  if (!report?.sections?.length) return h('div', {class: 'empty'}, h('p', {text: 'Run checks to create a readiness report.'}));
  const byCode = new Map((options?.sections || []).map((entry) => [entry.code, entry]));
  return h('div', {class: 'setup-sections'}, ...report.sections.map((section) => {
    const config = byCode.get(section.code) || {};
    const deepLink = DEEP_LINKS[section.code];
    const sectionActions = h('div', {class: 'section-actions'},
      h('button', {class: 'button ghost compact', onclick: () => actions.create('check', section.code)}, icon('refresh'), 'Check'),
      config.fixable ? h('button', {class: 'button compact', onclick: () => actions.create('fix', section.code)}, icon('settings'), 'Fix') : null,
      deepLink ? h('a', {class: 'button ghost compact', href: deepLink[0]}, deepLink[1]) : null);
    return h('section', {class: 'panel setup-section', 'data-section': section.code},
      h('div', {class: 'panel-heading'}, h('div', {}, h('div', {class: 'heading-line'}, h('h2', {text: section.label}), badge(section.status.toUpperCase(), statusTone(section.status))),
        h('p', {text: `${section.checks.length} readiness checks`})), sectionActions),
      h('div', {class: 'check-list'}, ...section.checks.map((check) =>
        h('article', {class: 'check-row'},
          h('div', {class: `status-dot ${statusTone(check.status)}`, title: check.status}),
          h('div', {}, h('strong', {text: check.explanation}), check.remediation ? h('p', {text: check.remediation}) : null,
            h('small', {class: 'mono', text: check.code})),
          badge(check.status.toUpperCase(), statusTone(check.status))))));
  }));
}

function readinessStrip(report) {
  const summary = report?.summary || {pass: 0, warn: 0, fail: 0, pending: 0};
  return h('section', {class: 'readiness-strip', 'aria-label': 'Readiness summary'},
    h('div', {class: 'readiness-overall'}, h('span', {text: 'Overall readiness'}), badge(String(report?.overall || 'pending').toUpperCase(), statusTone(report?.overall))),
    ...['pass', 'warn', 'fail', 'pending'].map((key) => h('div', {class: 'readiness-stat'}, h('strong', {text: String(summary[key] || 0)}), h('span', {text: key}))));
}

function networkChecklist(report) {
  const map = new Map((report?.sections || []).map((section) => [section.code, section]));
  const rows = [
    ['hosting_dns', 'Domains & DNS', '#/domains', 'globe'],
    ['hosting_vhosts', 'Vhosts & routes', '#/vhosts', 'deploy'],
    ['edge_fleet', 'Fleet convergence', '#/vhosts', 'activity'],
    ['webapp_keys', 'WebApp deploy keys', '#/webapps', 'key'],
  ];
  return h('section', {class: 'panel checklist-panel'},
    h('div', {class: 'panel-heading'}, h('div', {}, h('h2', {text: 'Network & Hosting'}), h('p', {text: 'The shortest path from readiness evidence to the permanent control.'}))),
    h('div', {class: 'checklist-grid'}, ...rows.map(([code, label, href, iconName]) => {
      const section = map.get(code); const status = section?.status || 'pending';
      return h('a', {class: 'checklist-link', href}, icon(iconName), h('div', {}, h('strong', {text: label}), h('span', {text: section ? `${section.checks.length} checks` : 'Awaiting report'})), badge(status.toUpperCase(), statusTone(status)));
    })));
}

function choiceField(name, spec, required) {
  const id = `choice-${name}`;
  const label = name.replaceAll('_', ' ').replace(/^./, (value) => value.toUpperCase());
  if (spec.type === 'boolean') {
    const input = h('input', {id, name, type: 'checkbox', checked: spec.enum?.length === 1 ? Boolean(spec.enum[0]) : false});
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
  const input = h('input', {id, name, type, required: required || null, autocomplete: 'off'});
  return {name, input, node: h('label', {class: 'field'}, h('span', {text: label}), input)};
}

function choiceForm(operation, actions) {
  const current = operation.current_step;
  const schema = current?.choice_schema;
  if (operation.status !== 'waiting_for_choice' || !schema?.properties) return null;
  const required = new Set(schema.required || []);
  const fields = Object.entries(schema.properties).map(([name, spec]) => choiceField(name, spec, required.has(name)));
  const unavailable = fields.some((field) => field.input.tagName === 'SELECT' && field.input.disabled);
  const message = h('div', {class: 'form-message', role: 'alert'}, unavailable ? 'No suitable existing resource was discovered. Repair provider access, then cancel and rerun this section.' : '');
  const button = h('button', {class: 'button primary', type: 'submit', disabled: unavailable}, icon('check'), 'Save and continue');
  return h('form', {class: 'setup-choice', onsubmit: async (event) => {
    event.preventDefault(); button.disabled = true; message.textContent = '';
    const choice = {};
    fields.forEach(({name, input}) => { choice[name] = input.type === 'checkbox' ? input.checked : input.value; });
    try { await actions.choose(current, choice); } catch (error) { message.textContent = error.message; button.disabled = unavailable; }
  }}, h('div', {class: 'choice-intro'}, h('strong', {text: current.label}), h('p', {text: 'Only the choice fields declared by the setup service are accepted. Secrets are never collected here.'})),
  ...fields.map((field) => field.node), message, button);
}

function operationView(operation, actions) {
  const current = operation.current_step;
  const progress = operation.steps?.length ? Math.round((operation.cursor / operation.steps.length) * 100) : 100;
  const terminal = TERMINAL.has(operation.status);
  const cancellable = !terminal && !['mutation_attempted', 'reconciling'].includes(current?.state);
  const stepRows = (operation.steps || []).map((step, index) => h('li', {class: index === operation.cursor ? 'active' : ''},
    h('span', {class: `step-marker ${statusTone(step.state)}`}, step.state === 'proven' ? icon('check') : String(index + 1)),
    h('span', {text: step.label}), badge(step.state.replaceAll('_', ' '), statusTone(step.state))));
  return h('section', {class: 'panel setup-operation'},
    h('div', {class: 'panel-heading'}, h('div', {}, h('h2', {text: `${operation.mode === 'fix' ? 'Fix' : 'Check'} operation`}),
      h('p', {text: current ? current.label : 'Operation finished'})), badge(operation.status.replaceAll('_', ' '), statusTone(operation.status))),
    h('div', {class: 'operation-progress'}, h('progress', {max: '100', value: progress, 'aria-label': 'Setup progress'}), h('span', {text: `${progress}%`})),
    h('div', {class: 'operation-layout'},
      h('ol', {class: 'step-list'}, ...stepRows),
      h('div', {class: 'operation-main'}, choiceForm(operation, actions),
        !terminal && operation.status !== 'waiting_for_choice' ? h('div', {class: 'running-state'}, icon('activity'), h('div', {}, h('strong', {text: 'Reconciling authoritative state'}), h('p', {text: 'The operation advances one durable step at a time. It is safe to close and resume.'}))) : null,
        h('div', {class: 'form-actions'}, cancellable ? h('button', {class: 'button ghost', onclick: actions.cancel}, 'Cancel operation') : null))),
    h('details', {class: 'operation-log', open: true}, h('summary', {text: 'Live operation log'}),
      h('ol', {}, ...(operation.log || []).map((entry) => h('li', {}, h('time', {text: new Date(entry.at).toLocaleTimeString()}), h('span', {text: entry.message}))))));
}

export async function setupPage() {
  const root = h('div', {class: 'page'});
  let report; let options; let operation; let driving = false; let cancelled = false;

  async function refreshReport() { report = await api('/api/account/admin/setup/readiness'); render(); }

  async function drive() {
    if (driving || !operation || TERMINAL.has(operation.status)) return;
    driving = true; cancelled = false;
    try {
      for (let count = 0; count < 80 && !cancelled; count += 1) {
        if (TERMINAL.has(operation.status) || operation.status === 'waiting_for_choice') break;
        operation = await api('/api/account/admin/setup/advance', {method: 'POST', body: JSON.stringify({operation: operation.id})});
        if (operation.report?.sections) report = operation.report;
        render();
        if (!TERMINAL.has(operation.status) && operation.status !== 'waiting_for_choice') await new Promise((resolve) => setTimeout(resolve, 250));
      }
      if (operation?.report?.sections) report = operation.report;
      if (TERMINAL.has(operation?.status)) await refreshReport();
    } finally { driving = false; render(); }
  }

  const actions = {
    create: async (mode, section = '') => {
      operation = await api('/api/account/admin/setup/create', {method: 'POST', body: JSON.stringify({mode, section, replay_key: crypto.randomUUID()})});
      render(); await drive();
    },
    choose: async (step, choice) => {
      operation = await api('/api/account/admin/setup/choose', {method: 'POST', body: JSON.stringify({
        operation: operation.id, step_id: step.id, definition_version: step.definition_version,
        choice_revision: step.choice_revision, choice,
      })});
      render(); await drive();
    },
    cancel: async () => {
      cancelled = true;
      operation = await api('/api/account/admin/setup/cancel', {method: 'POST', body: JSON.stringify({operation: operation.id})});
      render();
    },
  };

  function render() {
    root.replaceChildren(...[
      pageHeader('Installation control plane', 'System Setup', 'Configure a new installation, repair partial setup, and prove every dependency from one place.', [
        h('button', {class: 'button ghost', disabled: driving, onclick: () => actions.create('check')}, icon('refresh'), 'Run all checks'),
        h('button', {class: 'button primary', disabled: driving || (operation && !TERMINAL.has(operation.status)), onclick: () => actions.create('fix')}, icon('settings'), 'Fix all'),
      ]),
      report ? readinessStrip(report) : null,
      operation ? operationView(operation, actions) : null,
      networkChecklist(report),
      reportView(report, options, actions),
    ].filter(Boolean));
  }

  render();
  try {
    [options, report] = await Promise.all([api('/api/account/admin/setup/options'), api('/api/account/admin/setup/readiness')]);
    operation = options.active_fix || null; render();
    if (operation && !TERMINAL.has(operation.status) && operation.status !== 'waiting_for_choice') drive();
  } catch (error) { root.append(h('div', {class: 'error-state'}, icon('alert'), h('p', {text: error.message}))); }
  return root;
}

function evidenceCard(name, section) {
  const title = name.replaceAll('_', ' ').replace(/^./, (value) => value.toUpperCase());
  const data = section?.data || {};
  return h('section', {class: 'panel platform-evidence'},
    h('div', {class: 'panel-heading'}, h('div', {}, h('h2', {text: title}),
      h('p', {text: section?.reason || `Observed ${section?.observed_at ? new Date(section.observed_at).toLocaleString() : 'now'}`})),
      badge(String(section?.status || 'unavailable').toUpperCase(), statusTone(section?.status))),
    h('pre', {class: 'evidence-json', text: JSON.stringify(data, null, 2)}));
}

function platformDestinations(ctx) {
  const advanced = ctx.features?.advanced?.enabled === true;
  const network = ctx.capabilities.network || ctx.capabilities.manage_network;
  return h('section', {class: 'platform-destinations'},
    ctx.capabilities.setup ? h('a', {class: 'destination-card', href: routeHref('setup')},
      icon('settings'), h('div', {}, h('strong', {text: 'System Setup'}),
        h('span', {text: 'Check or repair installation dependencies as a literal superuser.'})), icon('chevron')) : null,
    network ? h('a', {class: 'destination-card', href: routeHref('domains')},
      icon('globe'), h('div', {}, h('strong', {text: 'Domains & DNS'}),
        h('span', {text: 'Add domains and manage the public records your WebApps use.'})), icon('chevron')) : null,
    advanced ? h('details', {class: 'advanced-disclosure'},
      h('summary', {}, icon('settings'), h('span', {}, h('strong', {text: 'Advanced'}),
        h('small', {text: 'Raw network, hosting, inventory, and protected settings'})), icon('chevron')),
      h('nav', {'aria-label': 'Advanced resources'},
        ...[
          ['advanced', 'Overview'], ['domains', 'Domains'], ['credentials', 'Credentials'],
          ['dns', 'DNS records'], ['certificates', 'Certificates'], ['upstreams', 'Upstreams'],
          ['vhosts', 'Vhosts'], ['routes', 'Routes'],
        ].map(([route, label]) => h('a', {href: routeHref(route), text: label})))) : null);
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
  }
  try { await load(); } catch (error) { root.replaceChildren(h('div', {class: 'error-state'}, icon('alert'), h('p', {text: error.message}))); }
  return root;
}
