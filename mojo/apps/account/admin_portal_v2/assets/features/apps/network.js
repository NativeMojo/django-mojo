// Serving (advanced) — the operator half of Apps.
//
// An app's Serving tab answers "how is THIS app reached". These three tables
// answer the same question one FLEET at a time: which hostnames the edge
// serves, which declared destinations exist, and which path prefixes are
// proxied. Same records, different unit of work — which is why they are the
// advanced half of Apps rather than a seventh sidebar entry.
//
// Ported from v1's advanced/page.js (`vhostsPage`, `routesPage`,
// `upstreamsPage`, `createVhostWizard`, `vhostDetail` and the route-repair
// flow). Every confirm, every consequence sentence and the sequential
// one-route-at-a-time walk are v1's, unchanged. The gate is v1's too:
// manage_network ALONE — these pages create, retire and repoint serving rows
// across every tenant, which a read grant must not see.
//
// v1 spelled the three as separate routes with no navigation entry between
// them. Here they are `#/apps-serving?tab=…`, under one back pill to Apps.
import {api, apiOnce, badge, FormView, formatDate, h, icon, openModal, TableView} from '../../core.js';
import {backPill} from '../../app.js';
import {loadInto, runAction} from '../../components/actions.js';
import {decodeRouteState, routeHref} from '../../components/routes.js';
import {permissionDeniedState, sectionTabs} from '../../components/views.js';
import {
  confirmNetwork, ensureGroupChoices, ensureRoute, forgetRoute, groupFields,
  loadCertificates, loadDomains, loadRoutes, loadUpstreams, loadVhosts,
  mutationFailed, parseRoutes, partialRoutes, postOnce, rememberRoute, routeState,
  statusBadge, tablePanel, VHOST_SHAPES, writeRouteRepairs,
} from '../../components/network.js';

function queryParam(name) {
  const query = location.hash.split('?')[1] || '';
  return new URLSearchParams(query).get(name);
}

// ---------------------------------------------------------------------------
// upstreams
// ---------------------------------------------------------------------------

function upstreamDialog(ctx, reload) {
  const form = new FormView({fields: [groupFields(ctx, false),
    {name: 'name', label: 'Upstream name', required: true},
    {name: 'kind', label: 'Kind', type: 'select', required: true, options: [{value: 'http', label: 'HTTP host + port'}, {value: 'unix', label: 'Unix socket'}]},
    {name: 'host', label: 'HTTP host', placeholder: '127.0.0.1'},
    {name: 'port', label: 'HTTP port', type: 'number', min: 1, max: 65535},
    {name: 'socket_path', label: 'Unix socket path', placeholder: '/run/mojo/app.sock'},
  ], value: {kind: 'http'}, submitLabel: 'Declare upstream', onSubmit: async (values) => {
    const payload = {name: values.name, kind: values.kind}; if (values.group) payload.group = values.group;
    if (values.kind === 'http') { payload.host = values.host; payload.port = Number(values.port); }
    else payload.socket_path = values.socket_path;
    await postOnce('/api/edge/upstream/declare', payload);
    close(); await reload();
  }});
  const close = openModal({title: 'Declare upstream', subtitle: 'This allowlist row is the only kind of destination a Vhost may select.', content: form.render()});
}

async function retireUpstream(row, reload) {
  const confirmed = await confirmNetwork({title: `Retire ${row.name}?`, copy: 'Existing Vhosts keep their historical reference but stop serving until repaired.', confirmLabel: 'Retire upstream', danger: true});
  if (!confirmed) return;
  await runAction(null, async () => {
    await postOnce('/api/edge/upstream/retire', {upstream: row.id}); await reload();
  }, {
    key: `upstream-retire:${row.id}`,
    busy: {title: `Retiring ${row.name}…`, detail: 'Vhosts pointing at it stop serving until they are repaired.'},
    onError: (error) => mutationFailed('The upstream was not retired', error),
  });
}

async function upstreamsTab(ctx, actions) {
  const root = h('div', {class: 'serving-tab'});
  async function render() {
    actions.replaceChildren(...[
      ctx.user.is_superuser ? h('button', {class: 'button primary', onclick: () => upstreamDialog(ctx, render)}, icon('plus'), 'Declare upstream') : null,
    ].filter(Boolean));
    const panel = tablePanel('Declared destinations', 'Retirement preserves history; destinations are never silently repointed.');
    root.replaceChildren(panel);
    const body = h('div', {}); panel.append(body);
    await loadInto(body, async (current) => {
      const rows = await loadUpstreams();
      if (!current()) return;
      body.replaceChildren(new TableView({rows, empty: 'No upstream destinations are declared.', columns: [
        {label: 'Upstream', render: (row) => h('div', {}, h('strong', {text: row.name}), h('small', {text: row.kind}))},
        {label: 'Target', render: (row) => h('code', {text: row.kind === 'unix' ? row.socket_path : `${row.host}:${row.port}`})},
        {label: 'Owner', render: (row) => row.group?.name || 'Shared'},
        {label: 'Status', render: (row) => statusBadge(row.is_enabled ? 'active' : 'inactive')},
        {label: '', render: (row) => (ctx.user.is_superuser && row.is_enabled ? h('button', {class: 'button compact', onclick: () => retireUpstream(row, render)}, 'Retire') : null)},
      ]}).render());
    }, {message: 'Loading upstreams…', retry: render});
  }
  await render(); return root;
}

// ---------------------------------------------------------------------------
// vhosts
// ---------------------------------------------------------------------------

async function createVhostWizard(ctx, reload) {
  const [domains, certificates, upstreams] = await Promise.all([loadDomains(), loadCertificates(), loadUpstreams()]);
  const body = h('div', {class: 'wizard'}); let kind = null;
  const close = openModal({title: 'Create Vhost', subtitle: 'Choose one known-good serving shape, then supply only its fields.', content: body, wide: true});
  function shapes() {
    body.replaceChildren(h('div', {class: 'shape-grid'}, ...VHOST_SHAPES.map(([value, label, copy, iconName]) => h('button', {class: 'shape-card', onclick: () => { kind = value; details(); }}, icon(iconName), h('div', {}, h('strong', {text: label}), h('p', {text: copy}))))));
  }
  function details() {
    const domain = h('select', {}, h('option', {value: '', text: 'Choose active domain'}), ...domains.filter((row) => row.status === 'active').map((row) => h('option', {value: row.id, text: row.name})));
    const certificate = h('select', {}, h('option', {value: '', text: 'Choose active certificate'}), ...certificates.filter((row) => row.status === 'active').map((row) => h('option', {value: row.id, text: row.common_name})));
    const upstream = h('select', {}, h('option', {value: '', text: 'Choose upstream'}), ...upstreams.filter((row) => row.is_enabled).map((row) => h('option', {value: row.id, text: `${row.name} (#${row.id})`})));
    const label = h('input', {placeholder: 'Blank for apex, * for wildcard'}); const pool = h('input', {value: 'default'});
    const bodySize = h('input', {type: 'number', min: '1', max: '4096', value: '50'}); const enabled = h('input', {type: 'checkbox', checked: true});
    const spa = h('input', {type: 'checkbox'}); const serveStatic = h('input', {type: 'checkbox'}); const redirect = h('input', {placeholder: 'www.example.com'});
    const routes = h('textarea', {rows: '4', placeholder: '/api | 12\n/ws | 14'}); const message = h('div', {class: 'form-message', role: 'alert'});
    // Creating a Vhost publishes a new desired generation and then walks its
    // routes one at a time; a second click mid-walk would create a second
    // Vhost. Scrim plus the guard, not an inline state.
    const create = h('button', {class: 'button primary', onclick: () => runAction(create, async () => {
      message.textContent = '';
      const payload = {domain: Number(domain.value), kind, label: label.value.trim(), certificate: Number(certificate.value), pool: pool.value.trim(), body_size_mb: Number(bodySize.value), is_enabled: enabled.checked, spa: spa.checked, serve_static: serveStatic.checked, quiet_paths: []};
      if (!payload.domain || !payload.certificate || !payload.pool) throw new Error('Domain, certificate, and pool are required.');
      if (kind === 'api') { payload.upstream = Number(upstream.value); if (!payload.upstream) throw new Error('An API host requires an upstream.'); }
      else payload.upstream = null;
      if (kind === 'redirect') { payload.redirect_to = redirect.value.trim(); if (!payload.redirect_to) throw new Error('A redirect target is required.'); }
      else payload.redirect_to = null;
      const routeRows = kind === 'site_api' ? parseRoutes(routes.value, upstreams) : [];
      const vhost = await postOnce('/api/edge/vhost', payload);
      const results = [];
      routeRows.forEach((row) => rememberRoute({vhost: vhost.id, path_prefix: row.path_prefix, upstream: row.upstream}, row.upstream_name));
      for (const row of routeRows) {
        try {
          // Sequential on purpose: a partial state names exactly which rows landed.
          const desired = {vhost: vhost.id, path_prefix: row.path_prefix, upstream: row.upstream};
          await ensureRoute(desired, () => postOnce('/api/edge/route', desired));
          forgetRoute(desired);
          results.push({...row, ok: true});
        } catch (error) { results.push({...row, ok: false, error: error.message}); }
      }
      const failed = results.filter((row) => !row.ok);
      if (failed.length) { partialRoutes.set(vhost.id, failed); writeRouteRepairs(); }
      await reload();
      body.replaceChildren(h('div', {class: `result-state ${failed.length ? 'warning' : 'success'}`}, icon(failed.length ? 'alert' : 'check'), h('h3', {text: failed.length ? 'Vhost created; routes need repair' : 'Vhost created'}), h('p', {text: failed.length ? `${failed.length} route(s) did not land. Open Routes to retry only those rows.` : 'The desired generation was published and the fleet can converge.'}), h('div', {class: 'form-actions'}, failed.length ? h('a', {class: 'button primary', href: routeHref('apps-serving', {tab: 'routes'}), onclick: close}, 'Open Routes') : h('button', {class: 'button primary', onclick: close}, 'Done'))));
    }, {
      busy: {title: 'Creating the Vhost…', detail: 'Publishing the desired generation, then adding its routes one at a time.'},
      restoreOnSuccess: false,
      onError: (error) => { message.textContent = error.message; },
    })}, 'Create Vhost');
    // `.filter(Boolean)` matters here and did not in v1's `h()` calls: DOM's
    // replaceChildren turns a bare `null` argument into the literal text
    // "null", which v1 printed above the Routes field on every Site + API
    // wizard. The branches are v1's; only the filter is new.
    body.replaceChildren(...[
      h('div', {class: 'wizard-steps'}, badge('1 Shape', 'success'), badge('2 Details', 'warning'), badge('3 Converge')),
      h('button', {class: 'button ghost compact', onclick: shapes}, '← Change shape'),
      h('div', {class: 'field-grid'}, h('label', {class: 'field'}, h('span', {text: 'Domain'}), domain), h('label', {class: 'field'}, h('span', {text: 'Certificate'}), certificate), h('label', {class: 'field'}, h('span', {text: 'Label'}), label), h('label', {class: 'field'}, h('span', {text: 'Pool'}), pool), h('label', {class: 'field'}, h('span', {text: 'Body size MB'}), bodySize)),
      kind === 'api' ? h('label', {class: 'field'}, h('span', {text: 'Upstream'}), upstream) : null,
      kind === 'redirect' ? h('label', {class: 'field'}, h('span', {text: 'Redirect target host'}), redirect) : null,
      kind === 'site_api' ? h('label', {class: 'field'}, h('span', {text: 'Routes (path | upstream-id)'}), routes, h('small', {text: upstreams.map((row) => `#${row.id} ${row.name}`).join(' · ')})) : null,
      ['site', 'site_api'].includes(kind) ? h('label', {class: 'check-field'}, spa, h('span', {text: 'Single-page app fallback'})) : null,
      ['api', 'site_api'].includes(kind) ? h('label', {class: 'check-field'}, serveStatic, h('span', {text: 'Serve /static/ locally'})) : null,
      h('label', {class: 'check-field'}, enabled, h('span', {text: 'Enable immediately'})), message, h('div', {class: 'form-actions'}, create),
    ].filter(Boolean));
  }
  shapes();
}

async function vhostDetail(row, reload) {
  const detail = await api(`/api/edge/vhost/${row.id}?graph=default`);
  const form = new FormView({fields: [
    {name: 'pool', label: 'Fleet pool', required: true},
    {name: 'body_size_mb', label: 'Maximum body size MB', type: 'number', min: 1, max: 4096},
    {name: 'spa', label: 'Single-page app fallback', type: 'checkbox'},
    {name: 'serve_static', label: 'Serve /static/ locally', type: 'checkbox'},
    {name: 'is_enabled', label: 'Vhost enabled', type: 'checkbox'},
  ], value: detail, submitLabel: 'Save and publish', onSubmit: async (values) => {
    values.body_size_mb = Number(values.body_size_mb);
    await postOnce(`/api/edge/vhost/${detail.id}`, values);
    close(); await reload();
  }});
  const close = openModal({title: detail.server_name, subtitle: `${detail.kind.replaceAll('_', ' ')} · shape is immutable; delete and recreate to change it.`, content: form.render()});
}

async function hostingProof(ctx) {
  if (!ctx.capabilities.setup) return null;
  try {
    const report = await api('/api/account/admin/setup/readiness?section=hosting_vhosts'); const section = report.sections?.[0];
    return h('section', {class: 'convergence-card'}, icon('activity'), h('div', {}, h('span', {text: 'Hosting convergence'}), h('strong', {text: section?.checks?.[0]?.explanation || 'Readiness has no hosting evidence yet.'})), statusBadge(section?.status || 'pending'));
  } catch (_) { return null; }
}

function edgeHttpPosture(ctx) {
  const edge = ctx.edge || {};
  const known = edge.available === true && typeof edge.http_enabled === 'boolean';
  const http = known ? (edge.http_enabled ? 'enabled' : 'disabled') : 'unknown';
  const issuance = edge.available === true && edge.dnsman_issuance ? edge.dnsman_issuance : 'unknown';
  return h('section', {class: 'convergence-card'}, icon('certificate'), h('div', {},
    h('span', {text: 'Certificate serving posture'}),
    h('strong', {text: `Public HTTP vhosts on this node: ${http}`}),
    h('small', {class: 'muted', text: `DNSMAN issuance: ${issuance}`})),
  statusBadge(known ? 'healthy' : 'pending'));
}

async function vhostsTab(ctx, actions) {
  const root = h('div', {class: 'serving-tab'});
  async function render() {
    actions.replaceChildren(...[
      ctx.capabilities.manage_network ? h('button', {class: 'button primary', onclick: () => createVhostWizard(ctx, render)}, icon('plus'), 'Create Vhost') : null,
    ].filter(Boolean));
    root.replaceChildren(edgeHttpPosture(ctx));
    const proof = await hostingProof(ctx); if (proof) root.append(proof);
    const panel = tablePanel('Serving configuration', 'API, Site, Site + API, and Redirect are the only supported shapes.'); root.append(panel);
    const body = h('div', {}); panel.append(body);
    await loadInto(body, async (current) => {
      const rows = await loadVhosts();
      if (!current()) return;
      body.replaceChildren(new TableView({rows, empty: 'No Vhosts are configured.', onSelect: ctx.capabilities.manage_network ? (row) => vhostDetail(row, render) : null, columns: [
        {label: 'Hostname', render: (row) => h('div', {}, h('strong', {text: row.server_name}), h('small', {text: row.domain?.name || ''}))},
        {label: 'Shape', render: (row) => badge(row.kind.replaceAll('_', ' '))},
        {label: 'Pool', render: (row) => h('span', {class: 'mono', text: row.pool})},
        {label: 'Status', render: (row) => statusBadge(row.is_enabled ? 'active' : 'inactive')},
        {label: 'Routes', render: (row) => h('a', {class: 'button compact', href: routeHref('apps-serving', {tab: 'routes', vhost: row.id}), onclick: (event) => event.stopPropagation()}, icon('route'), 'Open')},
      ]}).render());
    }, {message: 'Loading Vhosts…', retry: render});
  }
  await render(); return root;
}

// ---------------------------------------------------------------------------
// routes
// ---------------------------------------------------------------------------

function routeDialog(vhosts, upstreams, reload) {
  const form = new FormView({fields: [
    {name: 'vhost', label: 'Site + API Vhost', type: 'select', required: true, placeholder: 'Choose a Vhost', options: vhosts.filter((row) => row.kind === 'site_api').map((row) => ({value: row.id, label: row.server_name}))},
    {name: 'path_prefix', label: 'Path prefix', required: true, placeholder: '/api'},
    {name: 'upstream', label: 'Upstream', type: 'select', required: true, placeholder: 'Choose an upstream', options: upstreams.filter((row) => row.is_enabled).map((row) => ({value: row.id, label: row.name}))},
  ], submitLabel: 'Create route', onSubmit: async (values) => {
    const desired = {vhost: Number(values.vhost), path_prefix: values.path_prefix, upstream: Number(values.upstream)};
    const upstreamName = upstreams.find((row) => row.id === desired.upstream)?.name;
    rememberRoute(desired, upstreamName);
    await ensureRoute(desired, () => postOnce('/api/edge/route', desired));
    forgetRoute(desired); close(); await reload();
  }});
  const close = openModal({title: 'Create route', subtitle: 'The path selects a platform-declared destination; arbitrary proxy targets are impossible.', content: form.render()});
}

async function repairRoutes(vhost, reload) {
  const pending = partialRoutes.get(vhost) || []; const remaining = [];
  for (const row of pending) {
    try {
      const desired = {vhost, path_prefix: row.path_prefix, upstream: row.upstream};
      await ensureRoute(desired, () => postOnce('/api/edge/route', desired));
      forgetRoute(desired);
    } catch (error) { remaining.push({...row, error: error.message}); }
  }
  if (remaining.length) partialRoutes.set(vhost, remaining); else partialRoutes.delete(vhost);
  writeRouteRepairs();
  await reload();
}

async function deleteRoute(row, reload) {
  const confirmed = await confirmNetwork({title: `Delete ${row.path_prefix}?`, copy: 'Requests below this prefix will fall back to the static site.', confirmLabel: 'Delete route', danger: true});
  if (!confirmed) return;
  await runAction(null, async () => {
    let mutationError = null;
    try { await apiOnce(`/api/edge/route/${row.id}`, {method: 'DELETE'}); } catch (error) { mutationError = error; }
    const remaining = await loadRoutes();
    if (remaining.some((item) => item.id === row.id)) throw new Error(mutationError ? `Route deletion was not confirmed: ${mutationError.message}` : 'Route deletion was not visible in authoritative state.');
    await reload();
  }, {
    key: `route-delete:${row.id}`,
    busy: {title: `Deleting ${row.path_prefix}…`, detail: 'Deleting, then reading authoritative state back.'},
    onError: (error) => mutationFailed('The route was not deleted', error),
  });
}

async function routesTab(ctx, actions) {
  const root = h('div', {class: 'serving-tab'});
  async function render() {
    const [vhosts, upstreams, rows] = await Promise.all([loadVhosts(), loadUpstreams(), loadRoutes()]);
    [...partialRoutes.entries()].forEach(([vhost, plans]) => {
      const remaining = plans.flatMap((plan) => {
        const state = routeState(rows, {vhost, ...plan}).state;
        if (state === 'applied') return [];
        return [{...plan, error: state === 'mismatch' ? `${plan.path_prefix} points to a different authoritative upstream; review it before repair.` : plan.error}];
      });
      if (remaining.length) partialRoutes.set(vhost, remaining); else partialRoutes.delete(vhost);
    });
    writeRouteRepairs();
    const filter = queryParam('vhost'); const filtered = filter ? rows.filter((row) => String(row.vhost?.id || row.vhost) === filter) : rows;
    actions.replaceChildren(...[
      ctx.capabilities.manage_network ? h('button', {class: 'button primary', onclick: () => routeDialog(vhosts, upstreams, render)}, icon('plus'), 'Create route') : null,
    ].filter(Boolean));
    root.replaceChildren();
    [...partialRoutes.entries()].forEach(([vhost, pending]) => {
      const detail = pending.map((row) => row.error).filter(Boolean).join(' · ');
      // The callout this button lives in is rebuilt by render(), and the repair
      // walks one route at a time — a second click mid-walk would double-write.
      root.append(h('div', {class: 'callout warning'}, icon('alert'), h('div', {}, h('strong', {text: `${pending.length} route(s) need repair`}), h('p', {text: detail || 'The Vhost and earlier routes remain in place. Retry only the missing rows.'})), h('button', {class: 'button compact', onclick: () => runAction(null, () => repairRoutes(vhost, render), {
        key: `route-repair:${vhost}`,
        busy: {title: 'Repairing routes…', detail: 'Retrying only the rows that did not land, one at a time.'},
        onError: (error) => mutationFailed('The routes were not repaired', error),
      })}, 'Repair routes')));
    });
    const panel = tablePanel('Proxied paths', filter ? 'Filtered to one Vhost. Open the Routes tab without a filter to see them all.' : 'Longest matching prefix wins.'); root.append(panel);
    if (filter) {
      panel.append(h('p', {class: 'muted small serving-filter'},
        h('a', {href: routeHref('apps-serving', {tab: 'routes'})}, 'Clear the filter')));
    }
    panel.append(new TableView({rows: filtered, empty: 'No proxied routes are configured.', columns: [
      {label: 'Vhost', render: (row) => row.vhost?.server_name || `Vhost ${row.vhost?.id || row.vhost}`},
      {label: 'Path', render: (row) => h('code', {text: row.path_prefix})},
      {label: 'Upstream', render: (row) => row.upstream?.name || `#${row.upstream?.id || row.upstream}`},
      {label: 'Modified', render: (row) => formatDate(row.modified)},
      {label: '', render: (row) => (ctx.capabilities.manage_network ? h('button', {class: 'icon-button danger-text', 'aria-label': `Delete route ${row.path_prefix}`, onclick: () => deleteRoute(row, render)}, icon('trash')) : null)},
    ]}).render());
  }
  // Three reads before the page can paint anything, and a failure that used to
  // be a bare sentence with no way back.
  async function load() {
    await loadInto(root, () => render(), {message: 'Loading routes…', retry: load});
  }
  await load();
  return root;
}

// ---------------------------------------------------------------------------
// the sub-page
// ---------------------------------------------------------------------------

const TABS = [
  {id: 'vhosts', label: 'Vhosts', render: vhostsTab},
  {id: 'routes', label: 'Routes', render: routesTab},
  {id: 'upstreams', label: 'Upstreams', render: upstreamsTab},
];

export async function servingAdvancedPage(ctx, navigate) {
  // v1 gated all three on manage_network alone. Nothing softer: these tables
  // are the whole fleet, across every tenant.
  if (ctx.capabilities.manage_network !== true) {
    return permissionDeniedState(
      'Serving needs the manage_dns permission on this installation.');
  }
  await ensureGroupChoices(ctx);
  const requested = decodeRouteState().state.tab || '';
  const tab = TABS.find((item) => item.id === requested) || TABS[0];
  // A hash that named a tab this page does not have is corrected in place, so
  // the address bar and the screen never disagree.
  if (requested && requested !== tab.id) {
    history.replaceState({}, '', routeHref('apps-serving', {tab: tab.id}));
  }

  const actions = h('div', {class: 'page-actions'});
  const bodyHost = h('div', {class: 'serving-body'});
  const root = h('div', {class: 'page apps-serving-page'},
    backPill('Apps', 'apps'),
    h('header', {class: 'page-header'},
      h('div', {},
        h('div', {class: 'eyebrow', text: 'Apps · Advanced'}),
        h('h1', {text: 'Serving (advanced)', tabindex: '-1'}),
        h('p', {text: 'The hostnames this fleet serves, the destinations they may '
          + 'proxy to, and the paths that go somewhere else. One fleet at a time '
          + '— an app’s own Serving tab is the per-app view of the same records.'})),
      actions),
    sectionTabs({
      items: TABS.map(({id, label}) => ({id, label})),
      active: tab.id,
      label: 'Serving sections',
      onChange: (id) => navigate('apps-serving', {state: {tab: id}}),
    }),
    bodyHost);

  const body = await tab.render(ctx, actions);
  bodyHost.replaceChildren(body);
  root.dispose = () => body.dispose?.();
  return root;
}
