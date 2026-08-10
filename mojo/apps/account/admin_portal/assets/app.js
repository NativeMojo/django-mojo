import {api, h, icon} from './core.js';
import {dashboardPage, peoplePage, webappsPage} from './pages.js';
import {networkPage} from './network.js';
import {setupPage} from './setup.js';

const app = document.getElementById('app');
const NETWORK_ROUTES = new Set(['domains', 'credentials', 'dns', 'certificates', 'upstreams', 'vhosts', 'routes']);
const TITLES = {
  setup: 'System Setup', system: 'System overview', users: 'People', groups: 'People', webapps: 'WebApps',
  domains: 'Domains', credentials: 'DNS credentials', dns: 'DNS records', certificates: 'Certificates',
  upstreams: 'Upstreams', vhosts: 'Vhosts', routes: 'Routes',
};

function navItem(hash, name, iconName, active) {
  return h('a', {href: hash, class: active ? 'active' : ''}, icon(iconName), h('span', {text: name}));
}

function routeName() {
  return location.hash.replace(/^#\//, '').split('?')[0].split('/')[0] || 'system';
}

async function logout(path) {
  await fetch(`${path}_session`, {method: 'DELETE'}).catch(() => {});
  window.MojoAuth?.logout?.(); location.assign(path);
}

function setTheme(value) {
  localStorage.setItem('mojo-admin-theme', value);
  document.documentElement.dataset.theme = value;
}

function cycleTheme(button) {
  const current = document.documentElement.dataset.theme;
  const next = current === 'system' ? 'light' : current === 'light' ? 'dark' : 'system';
  setTheme(next); button.setAttribute('aria-label', `Color theme: ${next}. Activate to change.`);
}

function sidebarFor(ctx, route) {
  const sidebar = h('aside', {class: 'sidebar', 'aria-label': 'Admin navigation'},
    h('div', {class: 'brand'}, h('span', {class: 'brand-mark', text: 'M'}), h('div', {}, h('strong', {text: 'MOJO'}), h('small', {text: 'ADMIN'}))),
    h('nav', {class: 'nav'},
      h('div', {class: 'nav-label', text: 'Control plane'}),
      ctx.capabilities.setup ? navItem('#/setup', 'System Setup', 'settings', route === 'setup') : null,
      navItem('#/system', 'System', 'home', route === 'system'),
      (ctx.capabilities.people || ctx.capabilities.groups) ? navItem('#/users', 'People', 'users', route === 'users' || route === 'groups') : null,
      ctx.capabilities.network ? h('div', {class: 'nav-label nav-space', text: 'Network & hosting'}) : null,
      ctx.capabilities.network ? navItem('#/domains', 'Domains', 'globe', route === 'domains') : null,
      ctx.capabilities.network ? navItem('#/credentials', 'Credentials', 'key', route === 'credentials') : null,
      ctx.capabilities.network ? navItem('#/dns', 'DNS records', 'dns', route === 'dns') : null,
      ctx.capabilities.network ? navItem('#/certificates', 'Certificates', 'certificate', route === 'certificates') : null,
      ctx.capabilities.network ? navItem('#/upstreams', 'Upstreams', 'server', route === 'upstreams') : null,
      ctx.capabilities.network ? navItem('#/vhosts', 'Vhosts', 'deploy', route === 'vhosts') : null,
      ctx.capabilities.network ? navItem('#/routes', 'Routes', 'route', route === 'routes') : null,
      ctx.capabilities.webapps ? h('div', {class: 'nav-label nav-space', text: 'Applications'}) : null,
      ctx.capabilities.webapps ? navItem('#/webapps', 'WebApps', 'deploy', route === 'webapps') : null),
    h('div', {class: 'sidebar-footer'}, h('span', {text: `django-mojo ${ctx.version}`})));
  sidebar.addEventListener('click', (event) => { if (event.target.closest('a')) sidebar.classList.remove('open'); });
  return sidebar;
}

async function render(ctx) {
  const route = routeName();
  const sidebar = sidebarFor(ctx, route);
  const theme = document.documentElement.dataset.theme;
  const main = h('main', {class: 'main'}, h('header', {class: 'topbar'},
    h('button', {class: 'mobile-menu', 'aria-label': 'Toggle navigation', 'aria-expanded': 'false', onclick: (event) => {
      const open = sidebar.classList.toggle('open'); event.currentTarget.setAttribute('aria-expanded', String(open));
    }}, '☰'),
    h('div', {class: 'topbar-title', text: TITLES[route] || 'Admin'}),
    h('div', {class: 'topbar-actions'},
      h('button', {class: 'icon-button', 'aria-label': `Color theme: ${theme}. Activate to change.`, onclick: (event) => cycleTheme(event.currentTarget)}, icon('sun')),
      h('div', {class: 'user-menu'}, h('span', {class: 'avatar small', text: (ctx.user.display_name || ctx.user.email || '?').slice(0, 2).toUpperCase()}), h('div', {}, h('strong', {text: ctx.user.display_name || ctx.user.email}), h('small', {text: ctx.user.is_superuser ? 'Superuser' : 'Administrator'}))),
      h('button', {class: 'button ghost compact', onclick: () => logout(ctx.admin_path)}, 'Sign out'))),
    h('div', {id: 'page-content', class: 'content'}, h('div', {class: 'loading', text: 'Loading…'})));
  app.replaceChildren(sidebar, main);
  let page;
  if (route === 'setup') page = await setupPage(ctx);
  else if (route === 'users' || route === 'groups') page = await peoplePage(ctx, route);
  else if (route === 'webapps') page = await webappsPage(ctx);
  else if (NETWORK_ROUTES.has(route) && ctx.capabilities.network) page = await networkPage(ctx, route);
  else page = await dashboardPage(ctx);
  document.getElementById('page-content').replaceChildren(page);
  document.querySelector('.page-header h1')?.focus?.({preventScroll: true});
}

async function start() {
  setTheme(localStorage.getItem('mojo-admin-theme') || 'system');
  if (!window.MojoAuth) throw new Error('Authentication client unavailable');
  window.MojoAuth.init({baseURL: location.origin});
  const ctx = await api('/api/account/admin/bootstrap');
  await render(ctx);
}

window.addEventListener('hashchange', () => start().catch(showFatal));
function showFatal(error) {
  const adminPath = `/${location.pathname.split('/').filter(Boolean)[0] || 'admin'}/`;
  app.replaceChildren(h('div', {class: 'fatal'}, icon('alert'), h('h1', {text: 'Admin could not load'}), h('p', {text: error.message}), h('a', {class: 'button primary', href: `/auth?redirect=${encodeURIComponent(adminPath)}`}, 'Sign in again')));
}
start().catch(showFatal);
