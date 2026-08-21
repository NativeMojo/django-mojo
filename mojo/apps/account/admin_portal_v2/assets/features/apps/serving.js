// Serving: one tab for everything about HOW this app is reached — the address
// people type, the certificate that makes it https, the shape it is served in,
// and the paths it sends somewhere else.
//
// Four cards, one read. Nothing here is detected in the browser: which pools
// exist, whether a wildcard already covers this name, whether a certificate of
// its own could even be issued, which paths the platform manages — every one of
// those is server evidence, and a viewer who may not save is simply not told
// the fleet's inventory (`pools` and `upstreams` come back null).
//
// Vocabulary rule: this is a customer-facing tab. It says address, certificate,
// path and destination. The control-plane pages under #/vhosts are the operator
// surface and keep their own words; the footer links there, gated on
// manage_network.
//
// Ported from v1 unchanged except for where the operator links point: v2 has no
// serving-configuration or per-address routes page, so those two links open the
// current Admin and say so, rather than pointing at a v2 route that is not
// there. Everything else — the four cards, every refusal, every consequence
// sentence — is v1's, verbatim.
import {api, badge, formatDate, h, icon, TableView} from '../../core.js';
import {confirmAction, openModal} from '../../components/overlays.js';
import {routeHref} from '../../components/routes.js';
import {runAction} from '../../components/actions.js';
import {v1Href} from './shared.js';

// What each serving shape means in plain words, and why it is not a toggle.
// The shape decides which knobs the renderer has a branch for at all, so
// changing it is a rebuild, not a setting — the server ignores `kind` on the
// wire and this card says so rather than offering a control that would fail.
const SHAPES = {
  site: ['A plain site',
    'Everything visitors ask for is served straight from your build.'],
  site_api: ['A site, with some paths sent elsewhere',
    'Your build is served from the fleet, and the paths listed below go to your API instead.'],
  api: ['An API',
    'Every request is passed straight through to your API.'],
  redirect: ['A redirect',
    'Every request is sent on to another address.'],
};

function certTone(status) {
  if (status === 'active') return 'success';
  if (status === 'pending' || status === 'issuing') return 'warning';
  return 'danger';
}

function certLabel(certificate) {
  if (certificate.status === 'active') {
    const expired = certificate.not_after && new Date(certificate.not_after) < new Date();
    return expired ? 'HTTPS expired' : 'HTTPS valid';
  }
  if (certificate.status === 'pending' || certificate.status === 'issuing') return 'HTTPS being set up';
  return `HTTPS ${certificate.status || 'not issued yet'}`;
}

function cardShell(title, action) {
  return h('section', {class: 'serving-card'},
    h('div', {class: 'run-heading'},
      h('h3', {class: 'section-subhead', text: title}), action || null));
}

// ---------------------------------------------------------------------------
// Address
// ---------------------------------------------------------------------------
function addressCard(ctx, app, data, {renderAddresses, onChangeAddress}) {
  const manage = ctx.capabilities.manage_webapps;
  const address = data.address || {};
  const wildcard = address.wildcard || {};
  const live = h('span', {class: 'muted', text: 'Checking…'});
  // Opening the wizard is the only thing this click does; the button is still
  // on screen while the dialog builds, so the pending state belongs on it.
  const change = manage ? h('button', {class: 'button compact', type: 'button',
    onclick: (event) => runAction(event.currentTarget, () => onChangeAddress(),
      {pendingLabel: 'Opening…'})}, icon('globe'), 'Change address') : null;
  const card = cardShell('Address', change);
  if (!address.hostname) {
    card.append(h('p', {class: 'muted', text: 'This app has no address yet.'}));
    return card;
  }
  card.append(h('div', {class: 'serving-row'}, h('code', {text: address.hostname}), live));
  // Server evidence, both halves: the certificate really covers the wildcard
  // name AND this domain's records are ours to write.
  if (wildcard.covered === true && address.dns === 'managed') {
    card.append(h('p', {class: 'muted small', text:
      `Covered by the ${wildcard.name} wildcard — DNS and HTTPS need nothing from you.`}));
  }
  card.append(renderAddresses());
  api(`/api/edge/webapp/health?webapp=${encodeURIComponent(app.id)}`)
    .then((result) => {
      const tone = result.status === 'healthy' ? 'success'
        : result.status === 'not_configured' ? 'neutral' : 'danger';
      const label = result.status === 'healthy' ? 'Responding'
        : result.status === 'not_configured' ? 'Not serving yet'
          : `Not responding — ${result.detail || 'unreachable'}`;
      live.replaceWith(badge(label, tone));
    })
    .catch(() => { live.replaceWith(badge('Couldn’t check', 'neutral')); });
  return card;
}

// ---------------------------------------------------------------------------
// Certificate
// ---------------------------------------------------------------------------

// Two phases on purpose. Requesting only orders it; the app keeps serving what
// it serves today until someone deliberately switches, because a certificate
// that is still being issued would serve nothing at all.
function requestDedicated(app, reload) {
  return confirmAction({title: 'Order a certificate just for this app?',
    confirmLabel: 'Order it',
    copy: 'This asks for a certificate covering only this address. It usually takes a few minutes, and nothing about your app changes until you switch to it.'}).then((answer) => {
    if (!answer.confirmed) return undefined;
    // reload() rebuilds this whole tab, so there is no button left to restore.
    return runAction(null, async () => {
      await api('/api/edge/webapp/certificate', {method: 'POST', body: JSON.stringify({webapp: app.id})});
      await reload();
    }, {
      key: `webapp-serving-certificate:${app.id}`,
      busy: {title: 'Ordering a certificate…', detail: 'Nothing changes until you switch to it.'},
      success: 'Ordered. It usually takes a few minutes.',
    });
  });
}

function switchCertificate(app, certificateId, reload) {
  return confirmAction({title: 'Switch to this app’s own certificate?',
    confirmLabel: 'Switch',
    copy: 'Your app starts serving https with the certificate issued for this address alone. Visitors see no interruption.'}).then((answer) => {
    if (!answer.confirmed) return undefined;
    return runAction(null, async () => {
      await api('/api/edge/webapp/serving', {method: 'POST', body: JSON.stringify({webapp: app.id, certificate: certificateId})});
      await reload();
    }, {
      key: `webapp-serving-switch-certificate:${app.id}`,
      busy: {title: 'Switching certificate…', detail: 'Visitors see no interruption.'},
      success: 'Switched.',
    });
  });
}

function certificateCard(ctx, app, data, reload) {
  const manage = ctx.capabilities.manage_webapps;
  const certificate = data.certificate || {};
  const address = data.address || {};
  const domain = address.domain || null;
  const card = cardShell('Certificate');
  if (!certificate.id) {
    card.append(h('p', {class: 'muted', text: 'No certificate is on record for this address yet.'}));
    return card;
  }
  card.append(h('div', {class: 'serving-row'},
    h('code', {text: certificate.common_name}),
    badge(certLabel(certificate), certTone(certificate.status))));
  const renewal = certificate.renew_after
    ? `Renews ${formatDate(certificate.renew_after)}`
    : certificate.not_after ? `Expires ${formatDate(certificate.not_after)}` : '';
  const expiry = certificate.days_remaining != null && certificate.days_remaining >= 0
    ? ` · ${certificate.days_remaining} days left` : '';
  if (renewal) card.append(h('p', {class: 'muted small', text: `${renewal}${expiry}`}));
  card.append(h('p', {class: 'muted small', text: certificate.wildcard
    ? `Shared with every app on ${domain ? domain.name : 'this domain'}`
    : 'Used only by this app'}));
  if (domain) {
    card.append(h('p', {class: 'muted small'},
      h('a', {href: routeHref('domains', {domain: domain.id})}, 'See this domain')));
  }

  // No dead button: where the issuer could not honour an exact-name request,
  // the server's own sentence goes where the control would have been.
  if (certificate.dedicated_supported !== true) {
    card.append(h('p', {class: 'muted small', text: certificate.dedicated_reason
      || 'A certificate just for this app isn’t available here.'}));
    return card;
  }
  const own = certificate.dedicated || null;
  if (own && own.ready === true && own.id !== certificate.id) {
    card.append(h('div', {class: 'row-actions'},
      h('button', {class: 'button primary', type: 'button',
        onclick: () => switchCertificate(app, own.id, reload)}, icon('lock'),
      'Switch to it')));
    return card;
  }
  if (own && own.ready !== true && own.id !== certificate.id) {
    card.append(h('p', {class: 'muted small', text:
      `A certificate just for this app is ${own.status === 'failed'
        ? 'not finished — try ordering it again' : 'being issued. Check back in a few minutes'}.`}));
    if (manage && own.status === 'failed') {
      card.append(h('div', {class: 'row-actions'},
        h('button', {class: 'button', type: 'button',
          onclick: () => requestDedicated(app, reload)}, 'Try again')));
    }
    return card;
  }
  if (manage && certificate.wildcard) {
    card.append(h('div', {class: 'row-actions'},
      h('button', {class: 'button', type: 'button',
        onclick: () => requestDedicated(app, reload)}, icon('certificate'),
      'Use a dedicated certificate…')));
  }
  return card;
}

// ---------------------------------------------------------------------------
// How it's served
// ---------------------------------------------------------------------------
function servedCard(ctx, app, data, reload) {
  const manage = ctx.capabilities.manage_webapps;
  const serving = data.serving || {};
  const pools = serving.pools;
  const card = cardShell('How it’s served');
  if (!serving.kind) {
    card.append(h('p', {class: 'muted', text: 'Give this app an address and there will be something to set here.'}));
    return card;
  }
  const shape = SHAPES[serving.kind] || ['Custom', 'This app is served in a shape set up for it.'];
  card.append(h('div', {class: 'serving-row'}, h('strong', {text: shape[0]})));
  card.append(h('p', {class: 'muted small', text: shape[1]}));
  card.append(h('p', {class: 'muted small', text: 'This is decided when the app is created, so it can’t be changed here.'}));

  // A viewer sees the same facts with no controls — the server is the
  // authority either way, and `pools` being null IS the server saying so.
  if (!manage || !Array.isArray(pools)) {
    card.append(h('dl', {class: 'detail-grid'},
      h('dt', {text: 'Serving pool'}), h('dd', {text: serving.pool || '—'}),
      h('dt', {text: 'Single-page fallback'}),
      h('dd', {text: serving.spa === true ? 'On' : 'Off'})));
    return card;
  }
  const poolSelect = h('select', {}, ...pools.map((name) => h('option', {
    value: name, selected: name === serving.pool}, name)));
  const spaBox = h('input', {type: 'checkbox', checked: serving.spa === true});
  const save = h('button', {class: 'button primary', type: 'button'}, 'Save');
  save.addEventListener('click', () => runAction(null, async () => {
    await api('/api/edge/webapp/serving', {method: 'POST', body: JSON.stringify({
      webapp: app.id, pool: poolSelect.value, spa: spaBox.checked})});
    await reload();
  }, {
    key: `webapp-serving-save:${app.id}`,
    busy: {title: 'Saving how your app is served…',
      detail: 'Moving to another serving pool republishes your app on those servers, and every address this app answers on moves with it.'},
    success: 'Saved.',
  }));
  card.append(
    h('label', {class: 'field'}, h('span', {text: 'Serving pool'}), poolSelect),
    h('label', {class: 'check-field'}, spaBox,
      h('span', {text: 'Send unknown paths to your app instead of showing a 404'})),
    h('div', {class: 'row-actions'}, save));
  return card;
}

// ---------------------------------------------------------------------------
// Routes
// ---------------------------------------------------------------------------
function addRouteDialog(app, upstreams, reload) {
  const path = h('input', {placeholder: '/api', autocomplete: 'off', spellcheck: 'false'});
  const destination = h('select', {}, ...upstreams.map((row) => h('option', {value: row.id}, row.name)));
  const message = h('div', {class: 'form-message', role: 'alert'});
  const submit = h('button', {class: 'button primary', type: 'button'}, icon('route'), 'Add route');
  const content = h('div', {},
    h('p', {class: 'modal-copy', text: 'Requests starting with this path go to the destination you pick instead of coming from your build. Everything else is unchanged.'}),
    h('label', {class: 'field'}, h('span', {text: 'Path'}), path),
    h('label', {class: 'field'}, h('span', {text: 'Goes to'}), destination),
    message, h('div', {class: 'form-actions'}, submit));
  const close = openModal({title: `Add a route to ${app.slug}`,
    subtitle: 'It applies to every address this app answers on.', content});
  // Success closes the dialog, so nothing is restored onto a detached button;
  // a refusal keeps it open with the server's sentence in `message`.
  submit.addEventListener('click', () => runAction(submit, async () => {
    const value = path.value.trim();
    if (!value) { message.textContent = 'Type the path you want to send elsewhere.'; return; }
    await api('/api/edge/webapp/add_route', {method: 'POST', body: JSON.stringify({
      webapp: app.id, path_prefix: value, upstream: Number(destination.value)})});
    close(); await reload();
  }, {
    pendingLabel: 'Adding…', restoreOnSuccess: false,
    onError: (error) => { message.textContent = error.message; },
  }));
}

function removeRoute(app, row, reload) {
  return confirmAction({title: `Remove ${row.path_prefix}?`, danger: true,
    confirmLabel: 'Remove route',
    copy: 'This path goes back to being served from your build, on every address this app answers on.'}).then((answer) => {
    if (!answer.confirmed) return undefined;
    // The Remove button lives in the table that reload() rebuilds.
    return runAction(null, async () => {
      await api('/api/edge/webapp/remove_route', {method: 'POST', body: JSON.stringify({
        webapp: app.id, path_prefix: row.path_prefix})});
      await reload();
    }, {
      key: `webapp-serving-remove-route:${app.id}:${row.path_prefix}`,
      busy: {title: `Removing ${row.path_prefix}…`, detail: 'Your app keeps serving everything else.'},
      success: 'Removed.',
    });
  });
}

function routesCard(ctx, app, data, reload) {
  const manage = ctx.capabilities.manage_webapps;
  const serving = data.serving || {};
  const upstreams = data.upstreams;
  const canAdd = manage && serving.routes_supported === true
    && Array.isArray(upstreams) && upstreams.length > 0;
  const add = canAdd ? h('button', {class: 'button compact', type: 'button',
    onclick: () => addRouteDialog(app, upstreams, reload)}, icon('route'), 'Add route') : null;
  const card = cardShell('Routes', add);
  if (serving.routes_supported !== true) {
    card.append(h('p', {class: 'muted', text: 'Every path on this app is served straight from your build, so there is nothing to send elsewhere.'}));
    return card;
  }
  card.append(h('p', {class: 'muted small', text: 'Paths listed here go somewhere other than your build. Sign-in and account paths are set up for you and can’t be changed.'}));
  card.append(new TableView({columns: [
    {label: 'Path', render: (row) => h('div', {class: 'serving-row'},
      h('code', {text: row.path_prefix}),
      row.managed ? h('span', {class: 'route-managed'}, badge('Managed for you', 'neutral')) : null)},
    {label: 'Goes to', render: (row) => h('span', {text: (row.upstream && row.upstream.name) || '—'})},
    {label: '', render: (row) => (manage && !row.managed
      ? h('button', {class: 'button ghost compact danger-text', type: 'button',
        onclick: () => removeRoute(app, row, reload)}, 'Remove') : null)},
  ], rows: data.routes || [], empty: 'Everything is served from your build.'}).render());
  return card;
}

// ---------------------------------------------------------------------------
// Operator links — the control-plane pages, for people who run the fleet.
// ---------------------------------------------------------------------------
function operatorLinks(ctx, data) {
  if (!ctx.capabilities.manage_network) return null;
  const primary = (data.address || {}).vhost;
  // Both destinations are v1 pages — v2 has no fleet serving-configuration
  // screen. They open the current Admin, labelled so nobody is dropped into
  // different chrome unannounced.
  return h('p', {class: 'muted small serving-operator'},
    'Fleet tools: ',
    h('a', {href: v1Href(ctx, 'vhosts')}, 'Serving configuration'),
    primary ? ' · ' : null,
    primary ? h('a', {href: v1Href(ctx, `routes?vhost=${encodeURIComponent(primary)}`)},
      'Routes for this address') : null,
    h('span', {class: 'apps-note', text: ' · opens the current Admin'}));
}

/**
 * Paint the Serving tab into `body`.
 *
 * `current` is loadInto's generation token — two fast tab clicks are two
 * different renders, and the loser must not paint over the winner.
 * `renderAddresses` and `onChangeAddress` come from the app page so the
 * addresses table and the change-address wizard stay in one place.
 */
export async function servingPanel(ctx, app, body, current, {reload, renderAddresses, onChangeAddress}) {
  const data = await api(`/api/edge/webapp/serving?webapp=${encodeURIComponent(app.id)}`);
  if (!current()) return;
  body.replaceChildren(
    ...[
      addressCard(ctx, app, data, {renderAddresses, onChangeAddress}),
      certificateCard(ctx, app, data, reload),
      servedCard(ctx, app, data, reload),
      routesCard(ctx, app, data, reload),
      operatorLinks(ctx, data),
    ].filter(Boolean));
}
