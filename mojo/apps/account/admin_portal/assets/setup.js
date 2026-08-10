import {api, badge, h, icon, pageHeader} from './core.js';

function tone(status) {
  return status === 'pass' || status === 'succeeded' ? 'success' :
    status === 'fail' || status === 'failed' ? 'danger' :
      status === 'warn' || status === 'waiting_for_choice' ? 'warning' : 'neutral';
}

function reportView(report) {
  if (!report?.sections?.length) return h('div', {class: 'empty'}, h('p', {text: 'Run checks to create a readiness report.'}));
  return h('div', {class: 'setup-sections'}, ...report.sections.map((section) =>
    h('section', {class: 'panel setup-section'},
      h('div', {class: 'panel-heading'}, h('div', {}, h('h2', {text: section.label}),
        h('p', {text: `${section.checks.length} readiness checks`})), badge(section.status.toUpperCase(), tone(section.status))),
      h('div', {class: 'check-list'}, ...section.checks.map((check) =>
        h('article', {class: 'check-row'},
          h('div', {class: `status-dot ${tone(check.status)}`, title: check.status}),
          h('div', {}, h('strong', {text: check.explanation}),
            check.remediation ? h('p', {text: check.remediation}) : null,
            h('small', {class: 'mono', text: check.code})),
          badge(check.status.toUpperCase(), tone(check.status))))))));
}

function operationView(operation, actions) {
  const current = operation.current_step;
  const progress = operation.steps.length ? Math.round((operation.cursor / operation.steps.length) * 100) : 100;
  const choice = operation.status === 'waiting_for_choice' && current?.id === 'base_url' ?
    h('form', {class: 'setup-choice', onsubmit: async (event) => {
      event.preventDefault();
      const button = event.currentTarget.querySelector('button');
      button.disabled = true;
      await actions.choose(current, {base_url: event.currentTarget.elements.base_url.value});
    }}, h('label', {class: 'field'}, h('span', {text: 'Canonical public HTTPS origin'}),
      h('input', {name: 'base_url', type: 'url', required: true, placeholder: 'https://mojo.example.com'})),
    h('button', {class: 'button primary', type: 'submit'}, icon('check'), 'Save and continue')) : null;
  return h('section', {class: 'panel setup-operation'},
    h('div', {class: 'panel-heading'}, h('div', {}, h('h2', {text: `${operation.mode === 'fix' ? 'Fix' : 'Check'} operation`}),
      h('p', {text: current ? current.label : 'Operation finished'})), badge(operation.status.replaceAll('_', ' '), tone(operation.status))),
    h('progress', {class: 'progress', max: '100', value: progress, 'aria-label': 'Setup progress'}),
    choice,
    h('div', {class: 'form-actions'},
      operation.status === 'waiting_for_choice' ? null :
        !['succeeded', 'failed', 'cancelled'].includes(operation.status) ? h('button', {class: 'button primary', onclick: actions.advance}, icon('check'), 'Continue') : null,
      !['succeeded', 'failed', 'cancelled'].includes(operation.status) ? h('button', {class: 'button ghost', onclick: actions.cancel}, 'Cancel') : null),
    h('details', {class: 'operation-log', open: true}, h('summary', {text: 'Live operation log'}),
      h('ol', {}, ...(operation.log || []).map((entry) => h('li', {}, h('time', {text: new Date(entry.at).toLocaleTimeString()}), h('span', {text: entry.message}))))));
}

export async function setupPage() {
  const root = h('div', {class: 'page'});
  let report;
  let operation;

  async function refreshReport() {
    report = await api('/api/account/admin/setup/readiness');
    render();
  }

  async function create(mode) {
    operation = await api('/api/account/admin/setup/create', {
      method: 'POST', body: JSON.stringify({mode, replay_key: crypto.randomUUID()}),
    });
    operation = await api('/api/account/admin/setup/advance', {
      method: 'POST', body: JSON.stringify({operation: operation.id}),
    });
    if (operation.report?.sections) report = operation.report;
    render();
  }

  const actions = {
    advance: async () => {
      operation = await api('/api/account/admin/setup/advance', {method: 'POST', body: JSON.stringify({operation: operation.id})});
      if (operation.report?.sections) report = operation.report;
      render();
    },
    choose: async (step, choice) => {
      operation = await api('/api/account/admin/setup/choose', {method: 'POST', body: JSON.stringify({operation: operation.id, step_id: step.id, definition_version: step.definition_version, choice_revision: step.choice_revision, choice})});
      await actions.advance();
    },
    cancel: async () => {
      operation = await api('/api/account/admin/setup/cancel', {method: 'POST', body: JSON.stringify({operation: operation.id})});
      render();
    },
  };

  function render() {
    root.replaceChildren(
      pageHeader('Installation control plane', 'System Setup', 'Configure this installation and prove that every required service is ready.', [
        h('button', {class: 'button ghost', onclick: refreshReport}, 'Run all checks'),
        h('button', {class: 'button primary', onclick: () => create('fix')}, icon('settings'), 'Fix Setup'),
      ]),
      operation ? operationView(operation, actions) : null,
      reportView(report));
  }

  render();
  try { await refreshReport(); } catch (error) {
    root.append(h('div', {class: 'error-state'}, icon('alert'), h('p', {text: error.message})));
  }
  return root;
}
