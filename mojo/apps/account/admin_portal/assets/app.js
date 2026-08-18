import {api, h, icon} from './core.js';
import {clearBusy, closeAllOverlays, openModal} from './components/overlays.js';
import {decodeRouteState, routeHref} from './components/routes.js';
import {loadingState} from './components/views.js';
import {featureForRoute, installFeatureStyles, navigationFor} from './features/registry.js';

const app = document.getElementById('app');
let context = null; let content = null; let navigation = null; let title = null;
let generation = 0; let controller = null; let dispose = null;
let reauthClose = null;

function routeName() { return decodeRouteState().route; }

function navigate(route, {replace = false, state = {}} = {}) {
  const hash = routeHref(route, state);
  if (location.hash === hash) { render().catch(showFatal); return; }
  if (replace) { history.replaceState({}, '', hash); render().catch(showFatal); }
  else location.hash = hash;
}

async function logout(path) {
  await fetch(`${path}_session`, {method: 'DELETE'}).catch(() => {});
  window.MojoAuth?.logout?.(); location.assign(path);
}

function setTheme(value) { localStorage.setItem('mojo-admin-theme', value); document.documentElement.dataset.theme = value; }
function cycleTheme(button) {
  const current = document.documentElement.dataset.theme;
  const next = current === 'system' ? 'light' : current === 'light' ? 'dark' : 'system';
  setTheme(next); button.setAttribute('aria-label', `Color theme: ${next}. Activate to change.`);
}

function refreshNavigation(route) {
  let section = null; const children = [];
  for (const item of navigationFor(context)) {
    if (item.section !== section) { section = item.section; children.push(h('div', {class: `nav-label ${children.length ? 'nav-space' : ''}`, text: section})); }
    const active = (item.matches || [item.route]).includes(route);
    children.push(h('a', {href: `#/${item.route}`, class: active ? 'active' : ''}, icon(item.icon), h('span', {text: item.label})));
  }
  navigation.replaceChildren(...children);
}

function mountShell() {
  navigation = h('nav', {class: 'nav'});
  const sidebar = h('aside', {class: 'sidebar', 'aria-label': 'Admin navigation'},
    h('div', {class: 'brand'}, h('img', {class: 'brand-mark', src: 'assets/mojo-logo.png', alt: ''}), h('div', {}, h('strong', {text: 'MOJO'}), h('small', {text: 'ADMIN'}))),
    navigation, h('div', {class: 'sidebar-footer'}, h('span', {text: `django-mojo ${context.version}`})));
  sidebar.addEventListener('click', (event) => { if (event.target.closest('a')) sidebar.classList.remove('open'); });
  title = h('div', {class: 'topbar-title', text: 'Admin'});
  content = h('div', {id: 'page-content', class: 'content'}, loadingState('Loading Admin'));
  const theme = document.documentElement.dataset.theme;
  const main = h('main', {class: 'main'}, h('header', {class: 'topbar'},
    h('button', {class: 'mobile-menu', 'aria-label': 'Toggle navigation', 'aria-expanded': 'false', onclick: (event) => {
      const open = sidebar.classList.toggle('open'); event.currentTarget.setAttribute('aria-expanded', String(open));
    }}, '☰'), title,
    h('div', {class: 'topbar-actions'},
      h('button', {class: 'icon-button', 'aria-label': `Color theme: ${theme}. Activate to change.`, onclick: (event) => cycleTheme(event.currentTarget)}, icon('sun')),
      h('div', {class: 'user-menu'}, h('span', {class: 'avatar small', text: (context.user.display_name || context.user.email || '?').slice(0, 2).toUpperCase()}), h('div', {}, h('strong', {text: context.user.display_name || context.user.email}), h('small', {text: context.user.is_superuser ? 'Superuser' : 'Administrator'}))),
      h('button', {class: 'button ghost compact', onclick: () => logout(context.admin_path)}, 'Sign out'))), content);
  app.replaceChildren(sidebar, main);
}

async function render() {
  if (!context) return;
  const requestedRoute = routeName(); const feature = featureForRoute(requestedRoute, context);
  const route = feature.routes.includes(requestedRoute) ? requestedRoute : 'dashboard';
  if (route !== requestedRoute) history.replaceState({}, '', routeHref(route));
  const current = ++generation; controller?.abort();
  const renderController = new AbortController(); controller = renderController;
  if (typeof dispose === 'function') { try { dispose(); } finally { dispose = null; } }
  closeAllOverlays(); refreshNavigation(route); title.textContent = feature.title(route, context);
  content.replaceChildren(loadingState(`Loading ${feature.title(route, context)}`));
  const page = await feature.render({ctx: context, route, navigate, signal: renderController.signal});
  if (!(page instanceof Node)) throw new Error(`Admin feature ${feature.id} must render exactly one Node`);
  if (renderController.signal.aborted || current !== generation) { page.dispose?.(); return; }
  dispose = typeof page.dispose === 'function' ? () => page.dispose() : null;
  content.replaceChildren(page); content.querySelector('.page-header h1, h1')?.focus?.({preventScroll: true});
}

async function start() {
  setTheme(localStorage.getItem('mojo-admin-theme') || 'system');
  if (!window.MojoAuth) throw new Error('Authentication client unavailable');
  window.MojoAuth.init({baseURL: location.origin});
  context = await api('/api/account/admin/bootstrap');
  installFeatureStyles(context); mountShell(); await render();
}

function showFatal(error) {
  if (error?.code === 'fresh_auth_required') return;
  controller?.abort(); dispose?.(); closeAllOverlays();
  const adminPath = `/${location.pathname.split('/').filter(Boolean)[0] || 'admin'}/`;
  app.replaceChildren(h('div', {class: 'fatal'}, icon('alert'), h('h1', {text: 'Admin could not load'}), h('p', {text: error.message}), h('a', {class: 'button primary', href: `/auth?redirect=${encodeURIComponent(adminPath)}`}, 'Sign in again')));
}

function showFreshAuth(event) {
  const detail = event.detail || {};
  if (typeof detail.resolve !== 'function' || typeof detail.reject !== 'function') return;
  detail.handled = true; clearBusy();
  if (reauthClose) return;
  let settled = false;
  const identity = context.user.username;
  const message = h('div', {class: 'form-message', role: 'alert'});
  const password = h('input', {type: 'password', autocomplete: 'current-password', required: true, autofocus: true});
  const cancel = h('button', {class: 'button ghost', type: 'button'}, 'Cancel');
  const passkey = window.MojoAuth.isPasskeySupported?.()
    ? h('button', {class: 'button ghost', type: 'button'}, 'Use passkey') : null;
  const confirm = h('button', {class: 'button primary', type: 'submit'}, 'Confirm');
  const buttons = [cancel, passkey, confirm].filter(Boolean);
  const finish = (error = null) => {
    if (settled) return;
    settled = true; reauthClose?.();
    if (error) detail.reject(error); else detail.resolve();
  };
  const authenticate = async (action) => {
    buttons.forEach((button) => { button.disabled = true; }); message.textContent = '';
    try {
      const result = await action();
      if (result?.mfa_required || result?.requires_password_change) {
        throw new Error('This account needs another sign-in step. Use a passkey, or sign out and sign in again.');
      }
      if (result?.user?.id != null && String(result.user.id) !== String(context.user.id)) {
        window.MojoAuth.logout();
        throw new Error('The confirmed account does not match this Admin session. Sign in again.');
      }
      finish();
    } catch (error) {
      message.textContent = window.MojoAuth.getError?.(error) || error.message || 'Could not confirm your identity.';
      buttons.forEach((button) => { button.disabled = false; }); password.focus();
    }
  };
  const form = h('form', {onsubmit: (submitEvent) => {
    submitEvent.preventDefault();
    authenticate(() => window.MojoAuth.login(context.user.username, password.value));
  }},
  h('p', {class: 'modal-copy', text: 'Your session is still active. Confirm your identity here before this protected change can continue.'}),
  h('label', {class: 'field'}, h('span', {text: `Password for ${identity}`}), password),
  message, h('div', {class: 'form-actions'}, ...buttons));
  reauthClose = openModal({title: 'Recent authentication required', content: form,
    dismissible: false, onClose: () => {
      reauthClose = null;
      if (!settled) { settled = true; detail.reject(detail.error); }
    }});
  cancel.addEventListener('click', () => finish(detail.error));
  passkey?.addEventListener('click', () => authenticate(
    () => window.MojoAuth.loginWithPasskey(context.user.username)));
}

window.addEventListener('hashchange', () => render().catch(showFatal));
window.addEventListener('mojo-admin:fresh-auth', showFreshAuth);
start().catch(showFatal);
