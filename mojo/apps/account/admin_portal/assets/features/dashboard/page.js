import {h, pageHeader} from '../../core.js';

function card(title, value, copy, tone = '') {
  return h('article', {class: `kpi ${tone}`}, h('div', {class: 'kpi-label', text: title}), h('strong', {text: value}), h('p', {text: copy}));
}

export async function dashboardPage(ctx) {
  const caps = Object.values(ctx.capabilities).filter(Boolean).length;
  return h('div', {class: 'page'},
    pageHeader('Overview', 'System', 'A compact view of this MOJO control plane.'),
    h('section', {class: 'kpi-grid'},
      card('Framework', `v${ctx.version}`, 'Installed django-mojo version', 'accent'),
      card('Access', ctx.user.is_superuser ? 'Superuser' : 'Admin', 'Current control-plane role'),
      card('Groups', String(ctx.groups.length), 'Active memberships in scope'),
      card('Modules', String(caps), 'Available Admin capabilities')),
    h('section', {class: 'panel welcome'},
      h('div', {class: 'panel-heading'}, h('div', {}, h('h2', {text: 'Installation control center'}),
        h('p', {text: 'Identity, setup, domains, DNS, certificates, Vhosts, Routes, and WebApp deployment keys share one compact operator surface.'}))),
      h('div', {class: 'roadmap'}, ...['People', 'Setup', 'Domains', 'DNS', 'Certificates', 'Vhosts', 'WebApps'].map((name) =>
        h('div', {class: 'roadmap-item live'}, h('span', {text: 'Available'}), h('strong', {text: name}))))));
}
