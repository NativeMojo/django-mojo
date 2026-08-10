import {api, h, icon} from './core.js';
import {dashboardPage, peoplePage, webappsPage} from './pages.js';

const app = document.getElementById('app');

function navItem(hash, name, iconName, active) {
  return h('a', {href: hash, class: active ? 'active' : ''}, icon(iconName), h('span', {text: name}));
}

function routeName() { return location.hash.replace(/^#\//, '').split('/')[0] || 'system'; }

async function logout(path) {
  await fetch(`${path}_session`, {method: 'DELETE'}).catch(() => {});
  window.MojoAuth?.logout?.();
  location.assign(path);
}

function setTheme(value) {
  localStorage.setItem('mojo-admin-theme', value);
  document.documentElement.dataset.theme = value;
}

async function render(ctx) {
  const route = routeName();
  const nav = [
    ['#/system', 'System', 'home', true],
    ['#/users', 'People', 'users', ctx.capabilities.people || ctx.capabilities.groups],
    ['#/webapps', 'WebApps', 'deploy', ctx.capabilities.webapps],
  ].filter((item) => item[3]);
  const sidebar = h('aside', {class: 'sidebar'},
    h('div', {class: 'brand'}, h('span', {class: 'brand-mark', text: 'M'}), h('div', {}, h('strong', {text: 'MOJO'}), h('small', {text: 'ADMIN'}))),
    h('nav', {class: 'nav'}, h('div', {class: 'nav-label', text: 'Control plane'}), ...nav.map(([hash, name, iconName]) => navItem(hash, name, iconName, (route === 'groups' || route === 'users') ? name === 'People' : hash === `#/${route}`)),
      h('div', {class: 'nav-label muted', text: 'Next phases'}), ...['Fleet', 'Network', 'Operations', 'Security', 'Configuration'].map((name) => h('span', {class: 'nav-disabled'}, icon('settings'), h('span', {text: name})))),
    h('div', {class: 'sidebar-footer'}, h('span', {text: `django-mojo ${ctx.version}`})));
  const main = h('main', {class: 'main'}, h('header', {class: 'topbar'},
    h('button', {class: 'mobile-menu', 'aria-label': 'Toggle navigation', onclick: () => sidebar.classList.toggle('open')}, '☰'),
    h('div', {class: 'topbar-title', text: route === 'system' ? 'System overview' : route === 'webapps' ? 'WebApps' : 'People'}),
    h('div', {class: 'topbar-actions'},
      h('button', {class: 'icon-button', title: 'Cycle color theme', onclick: () => { const current = document.documentElement.dataset.theme; setTheme(current === 'system' ? 'light' : current === 'light' ? 'dark' : 'system'); }}, icon('sun')),
      h('div', {class: 'user-menu'}, h('span', {class: 'avatar small', text: (ctx.user.display_name || ctx.user.email || '?').slice(0, 2).toUpperCase()}), h('div', {}, h('strong', {text: ctx.user.display_name || ctx.user.email}), h('small', {text: ctx.user.is_superuser ? 'Superuser' : 'Administrator'}))),
      h('button', {class: 'button ghost compact', onclick: () => logout(ctx.admin_path)}, 'Sign out'))),
    h('div', {id: 'page-content', class: 'content'}, h('div', {class: 'loading', text: 'Loading…'})));
  app.replaceChildren(sidebar, main);
  let page;
  if (route === 'users' || route === 'groups') page = await peoplePage(ctx, route);
  else if (route === 'webapps') page = await webappsPage(ctx);
  else page = await dashboardPage(ctx);
  document.getElementById('page-content').replaceChildren(page);
}

async function start() {
  setTheme(localStorage.getItem('mojo-admin-theme') || 'system');
  if (!window.MojoAuth) throw new Error('Authentication client unavailable');
  window.MojoAuth.init({baseURL: location.origin});
  const ctx = await api('/api/account/admin/bootstrap');
  await render(ctx);
}

window.addEventListener('hashchange', () => start().catch(showFatal));
function showFatal(error) { const adminPath = `/${location.pathname.split('/').filter(Boolean)[0] || 'admin'}/`; app.replaceChildren(h('div', {class: 'fatal'}, icon('alert'), h('h1', {text: 'Admin could not load'}), h('p', {text: error.message}), h('a', {class: 'button primary', href: `/auth?redirect=${encodeURIComponent(adminPath)}`}, 'Sign in again'))); }
start().catch(showFatal);
