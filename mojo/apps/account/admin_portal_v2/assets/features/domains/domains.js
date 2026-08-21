// Domains — the names this installation controls, and one page per name.
//
// Ported from v1's advanced/page.js `domainsPage`, including the "what's on
// this domain" panel, the registrar dialogs and the three-step purchase
// wizard with its typed confirmation and single non-retried attempt.
//
// v2 adds three things, all read-only and all from endpoints this portal
// already calls: an at-a-glance strip, an Email identity panel that says who
// this installation may send as, and the disclosure line pointing at the raw
// DNS editor.
import {api, badge, FormView, formatDate, h, icon, listData, openModal, TableView} from '../../core.js';
import {loadInto, runAction} from '../../components/actions.js';
import {activityHref, decodeRouteState, returnLocation, routeHref} from '../../components/routes.js';
import {errorState} from '../../components/views.js';
import {rowSection, statusRow} from '../../components/rows.js';
import {
  groupFields, loadCertificates, loadCredentials, loadDomains, loadVhosts,
  postOnce, providerMutation, statusBadge, tablePanel,
} from '../../components/network.js';
import {domainCertificatesPanel} from './certificates.js';

const EMAIL_SUMMARY_URL = '/api/aws/email/summary';

function queryParam(name) {
  const query = location.hash.split('?')[1] || '';
  return new URLSearchParams(query).get(name);
}

// ---------------------------------------------------------------------------
// registrar dialogs
// ---------------------------------------------------------------------------

function editDomain(domain, reload) {
  const form = new FormView({fields: [
    {name: 'auto_renew', label: 'Automatic renewal', type: 'checkbox'},
    {name: 'privacy', label: 'WHOIS privacy', type: 'checkbox'},
  ], value: domain, submitLabel: 'Save domain', onSubmit: async (values) => {
    await postOnce(`/api/dnsman/domain/${domain.id}`, values);
    close(); await reload();
  }});
  const close = openModal({title: domain.name, subtitle: 'Registrar settings for this managed domain.', content: form.render()});
}

async function registerExisting(ctx, reload) {
  const credentials = await loadCredentials();
  const form = new FormView({fields: [groupFields(ctx),
    {name: 'domain', label: 'Domain name', required: true, placeholder: 'example.com'},
    {name: 'credential', label: 'Verified credential', type: 'select', required: true, placeholder: 'Choose a credential', options: credentials.filter((row) => row.verified && row.is_active).map((row) => ({value: row.id, label: `${row.name} · ${row.provider}`}))},
  ], submitLabel: 'Register existing domain', onSubmit: async (values) => {
    const before = await loadDomains();
    await providerMutation(`domain:register:${values.domain.toLowerCase()}`,
      () => postOnce('/api/dnsman/registrar/register-existing', values),
      loadDomains,
      (observed) => (observed.some((row) => row.name === values.domain.toLowerCase()) ? 'applied' : (observed.length === before.length ? 'not-applied' : 'unconfirmed')));
    close(); await reload();
  }});
  const close = openModal({title: 'Register an existing domain', subtitle: 'The provider credential proves control before anything is stored.', content: form.render()});
}

async function adoptDomain(ctx, reload) {
  const discovery = await api('/api/dnsman/registrar/discover?untracked=true');
  const rows = discovery.domains || discovery.results || listData(discovery);
  const form = new FormView({fields: [
    {name: 'domain', label: 'Existing Route 53 domain or zone', type: 'select', required: true, placeholder: 'Choose an existing domain', options: rows.map((row) => ({value: row.name || row.domain, label: `${row.name || row.domain}${row.hosted ? ' · hosted zone' : ''}`}))},
    groupFields(ctx, false),
    {name: 'create_zone', label: 'Create a missing hosted zone', type: 'checkbox', help: 'Existing zones are always adopted in place.'},
  ], submitLabel: 'Adopt domain', onSubmit: async (values) => {
    const payload = {domain: values.domain, create_zone: values.create_zone};
    if (values.group) payload.group = values.group;
    await providerMutation(`domain:adopt:${values.domain}`,
      () => postOnce('/api/dnsman/registrar/adopt', payload),
      loadDomains, (observed) => (observed.some((row) => row.name === values.domain) ? 'applied' : 'unconfirmed'));
    close(); await reload();
  }});
  const close = openModal({title: 'Adopt from Route 53', subtitle: 'Discovery is read-only. Adoption preserves the existing zone and records.', content: form.render(), wide: true});
}

async function purchaseDomain(ctx, reload) {
  let quote = null; let token = null;
  const body = h('div', {class: 'wizard'});
  const scrub = () => {
    token = null;
    if (quote) quote.token = null;
    quote = null;
    body.querySelectorAll('input,textarea').forEach((input) => { input.value = ''; });
  };
  const close = openModal({title: 'Buy a domain', subtitle: 'Search, quote, typed confirmation, then one non-retried purchase attempt.', content: body, wide: true, onClose: scrub});
  function renderSearch() {
    const name = h('input', {type: 'text', placeholder: 'example.com', autocomplete: 'off'});
    const group = h('select', {}, h('option', {value: '', text: 'Choose a group'}), ...(ctx.groups || []).map((row) => h('option', {value: row.id, text: row.name})));
    const message = h('div', {class: 'form-message', role: 'alert'});
    const search = h('button', {class: 'button primary', onclick: () => runAction(search, async () => {
      message.textContent = '';
      const row = await api('/api/dnsman/registrar/search', {method: 'POST', body: JSON.stringify({domain: name.value})});
      renderResult(row, group.value);
    }, {
      // renderResult replaces the whole wizard body, this button included, so
      // there is nothing to restore on the path that succeeds.
      pendingLabel: 'Checking…', restoreOnSuccess: false,
      onError: (error) => { message.textContent = error.message; },
    })}, icon('search'), 'Check availability');
    body.replaceChildren(h('div', {class: 'wizard-steps'}, badge('1 Search', 'success'), badge('2 Quote'), badge('3 Confirm')),
      h('label', {class: 'field'}, h('span', {text: 'Group'}), group), h('label', {class: 'field'}, h('span', {text: 'Domain'}), name), message, search);
  }
  function renderResult(row, groupId) {
    const available = row.available === true; const price = row.price ?? '—';
    body.replaceChildren(h('div', {class: 'wizard-steps'}, badge('1 Search', 'success'), badge('2 Quote', 'success'), badge('3 Confirm')),
      h('div', {class: 'result-card'}, h('div', {}, h('strong', {text: row.name}), h('span', {text: row.reason || (available ? 'Available to register' : 'Not available')})), statusBadge(available ? 'active' : 'failed'), h('strong', {text: `${price} ${row.currency || 'USD'}`})),
      h('div', {class: 'form-actions'}, h('button', {class: 'button ghost', onclick: renderSearch}, 'Search again'),
        available && groupId ? h('button', {class: 'button primary', onclick: (event) => runAction(event.currentTarget, async () => {
          quote = await postOnce('/api/dnsman/registrar/quote', {group: groupId, domain: row.name, years: 1});
          token = quote.token; quote.token = null; renderConfirm(groupId);
        }, {
          pendingLabel: 'Getting a quote…', restoreOnSuccess: false,
          onError: (error) => { body.append(h('div', {class: 'form-message', text: error.message})); },
        })}, 'Get live quote') : null));
  }
  function renderConfirm(groupId) {
    const typedDomain = h('input', {autocomplete: 'off', placeholder: quote.name});
    const typedPrice = h('input', {autocomplete: 'off', inputmode: 'decimal', placeholder: String(quote.price)});
    const message = h('div', {class: 'form-message', role: 'alert'});
    // This spends money on a single non-retried attempt. The scrim is the
    // point: nothing else on the page is clickable — least of all this button
    // a second time — while the registrar is deciding.
    //
    // And the quote is single-use, so the control latches off the moment the
    // spend starts and never comes back. `runAction` restores an errored
    // control, which on its own would leave "Register domain" live directly
    // under the message saying the quote was spent; the server refuses that
    // retry, so the operator would just collect a second, more confusing
    // refusal. The latch is its own flag rather than a bare `disabled = true`
    // because validate() re-derives `disabled` on every keystroke.
    let spent = false;
    const buy = h('button', {class: 'button danger', disabled: true, onclick: () => runAction(buy, async () => {
      // Synchronous, before the first await: the guard and the latch agree.
      spent = true; buy.disabled = true;
      let purchaseToken = token; token = null;
      try {
        await postOnce('/api/dnsman/registrar/purchase', {group: groupId, purchase: quote.purchase, confirm_token: purchaseToken, confirm_domain: typedDomain.value, confirm_price: typedPrice.value});
        close(); await reload();
      } finally { purchaseToken = null; }
    }, {
      busy: {title: `Registering ${quote.name}…`, detail: 'This is a single attempt — it is not retried.'},
      restoreOnSuccess: false,
      onError: (error) => {
        if (quote) quote.token = null;
        message.textContent = `${error.message} The quote was spent; take a new quote before trying again.`;
      },
    })}, 'Register domain');
    const validate = () => { buy.disabled = spent || typedDomain.value.trim().toLowerCase() !== quote.name || typedPrice.value.trim() !== String(quote.price); };
    typedDomain.addEventListener('input', validate); typedPrice.addEventListener('input', validate);
    body.replaceChildren(h('div', {class: 'wizard-steps'}, badge('1 Search', 'success'), badge('2 Quote', 'success'), badge('3 Confirm', 'warning')),
      h('div', {class: 'callout warning'}, icon('alert'), h('p', {text: `This spends ${quote.price} ${quote.currency} to register ${quote.name}. The quote expires ${formatDate(quote.expires)}.`})),
      h('label', {class: 'field'}, h('span', {text: `Type ${quote.name}`}), typedDomain),
      h('label', {class: 'field'}, h('span', {text: `Type ${quote.price}`}), typedPrice), message, h('div', {class: 'form-actions'}, buy));
  }
  renderSearch();
}

// ---------------------------------------------------------------------------
// one domain
// ---------------------------------------------------------------------------

// Everything this domain is already doing, and — just as important — what
// putting an app live on it does NOT touch. The reads are the ones this portal
// already makes: the live provider zone, the vhost inventory, and the app list.
// Nothing here writes, and nothing here is derived from a guess about record
// shapes: an address belongs to this domain because its vhost says so.
function domainOverviewPanel(ctx, domain) {
  const panel = tablePanel('What’s on this domain',
    'Everything this domain is doing, and what putting an app live does not touch.');
  // Into a body node, not the panel: the panel carries the heading this
  // loading state would otherwise replace.
  const body = h('div', {}); panel.append(body);
  loadInto(body, async (current) => {
    // `mojo` is the provider name for "the customer keeps their own DNS", and a
    // domain that is not active has no zone here to ask about. Asking anyway is
    // a guaranteed provider error rendered as if the domain were broken, so the
    // zone read is skipped outright and the blocks say why.
    const keepsOwnDns = domain.provider === 'mojo' || domain.status !== 'active';
    // Each read is caught on its own: a zone the provider will not answer for
    // must still leave the addresses block standing, and vice versa.
    const [records, vhosts, apps] = await Promise.all([
      keepsOwnDns ? Promise.resolve(null)
        : api(`/api/dnsman/dns?domain=${encodeURIComponent(domain.id)}`)
          .then((payload) => payload.records || []).catch(() => null),
      loadVhosts().catch(() => []),
      api('/api/edge/webapp?graph=default&size=200').then(listData).catch(() => []),
    ]);
    if (!current()) return;

    const keptDnsCopy = 'This domain’s DNS lives at your own host — its records are managed there.';
    const unreadableCopy = 'This domain’s zone could not be read just now.';
    const zoneNote = keepsOwnDns ? keptDnsCopy : records === null ? unreadableCopy : null;

    // The wildcard is the whole reason a new app is instant. Matched by exact
    // name, trailing dot and all — providers return both spellings.
    const wildcardName = `*.${domain.name}`;
    const bare = (value) => String(value || '').replace(/\.$/, '');
    const wildcard = (records || []).find((row) =>
      String(row.type || '').toUpperCase() === 'CNAME' && bare(row.name) === wildcardName);
    const wildcardCard = zoneNote
      ? h('p', {class: 'muted small', text: zoneNote})
      : wildcard
        ? h('div', {class: 'result-card'}, h('div', {},
          h('strong', {text: `${wildcardName} → ${(wildcard.record_values || []).map(bare).join(', ')}`}),
          h('span', {text: 'Every app under this domain answers here. This is why a new app is instant — no record is added for it.'})))
        : h('div', {class: 'result-card'}, h('div', {},
          h('strong', {text: 'No wildcard record yet'}),
          h('span', {text: 'The first app created under this domain adds it, along with the certificate that covers every app on it.'})));

    // An EXACT-ID join, never a suffix match: `*.eu.example.com` is a different
    // domain row than `example.com`, and a name test would fold one into the
    // other. This catches primaries, extra addresses and apex rows alike.
    const onDomain = vhosts.filter((vhost) => vhost.domain?.id === domain.id);
    const appByVhost = new Map();
    apps.forEach((app) => { if (app.vhost?.id != null) appByVhost.set(app.vhost.id, app); });
    const addressRows = onDomain.map((vhost) => ({
      hostname: vhost.server_name, kind: vhost.kind,
      app: appByVhost.get(vhost.id) || null,
    }));
    const addresses = new TableView({rows: addressRows,
      empty: 'No apps are using this domain yet.', columns: [
        {label: 'Address', render: (row) => h('span', {class: 'mono', text: row.hostname})},
        // No app owns this vhost as its own address: it is one app's extra
        // address, or a vhost an admin made by hand. Said plainly, not hidden.
        {label: 'App', render: (row) => (row.app
          ? h('a', {href: routeHref('apps', {webapp: row.app.id})},
            row.app.display_name || row.app.slug)
          : h('span', {class: 'muted', text: ['site', 'site_api'].includes(row.kind)
            ? '— extra address' : `— ${row.kind}`}))},
      ]}).render();

    // Mail is the fear this panel exists to settle: nothing about putting an
    // app live reaches an MX or a verification TXT.
    const mail = (records || []).filter((row) =>
      ['MX', 'TXT'].includes(String(row.type || '').toUpperCase()));
    const mailBlock = zoneNote
      ? h('p', {class: 'muted small', text: zoneNote})
      : new TableView({rows: mail,
        empty: 'No mail or verification records in this zone.', columns: [
          {label: 'Type', render: (row) => badge(row.type, 'neutral')},
          {label: 'Name', render: (row) => h('span', {class: 'mono', text: row.name})},
          {label: 'Value', render: (row) => h('div', {class: 'record-values'},
            ...(row.record_values || []).map((value) => h('code', {text: value})))},
        ]}).render();

    body.replaceChildren(
      h('h3', {class: 'section-subhead', text: 'The wildcard record'}), wildcardCard,
      h('h3', {class: 'section-subhead', text: 'Addresses on this domain'}), addresses,
      h('h3', {class: 'section-subhead', text: 'Mail and verification records'}),
      h('p', {class: 'muted small', text: 'Yours to manage. Putting an app live never adds, changes, or removes anything here.'}),
      mailBlock);
  }, {message: 'Reading this domain…'});
  return panel;
}

// ---------------------------------------------------------------------------
// email identity
// ---------------------------------------------------------------------------

/**
 * Who this installation may send as.
 *
 * One read, the same `/api/aws/email/summary` Settings ▸ Email already makes,
 * and only its persisted verdict — no live SES re-check is triggered from here.
 * The panel states what the report says and hands the operator to the page that
 * owns the fix; it never offers a control of its own.
 *
 * A caller without the email block sees nothing at all. A read that fails is
 * reported as unreadable rather than as "no sender", because those are
 * different facts.
 */
function emailIdentityPanel(ctx) {
  if (ctx.features?.email?.enabled !== true) return null;
  const body = h('div', {});
  const panel = h('section', {class: 'panel domains-email'},
    h('div', {class: 'panel-head'}, h('div', {},
      h('h2', {text: 'Email identity'}),
      h('p', {text: 'Who this installation may send as. DNS lives on this page, so verification finishes here too.'}))),
    body);
  api(EMAIL_SUMMARY_URL).then((report) => {
    const domains = report.domains || [];
    const rows = domains.slice(0, 6).map((domain) => statusRow({
      tone: domain.can_send ? 'ok' : domain.status === 'pending' ? 'muted' : 'warn',
      name: domain.name, mono: true,
      value: domain.can_send && domain.can_recv ? 'Sending and receiving ready'
        : domain.can_send ? 'Ready to send'
          : domain.status === 'pending' ? 'Never checked' : 'Not ready',
      detail: domain.checked_at ? `last checked ${formatDate(domain.checked_at)}` : '',
    }));
    if (!rows.length) {
      rows.push(statusRow({tone: 'warn', name: 'No sender domain',
        value: 'Sending blocked',
        detail: 'invites, alerts and password resets stay blocked until a sender verifies'}));
    }
    body.replaceChildren(rowSection('SES domains', rows),
      h('p', {class: 'muted small'},
        h('a', {href: routeHref('settings-email')}, 'Manage in Settings ▸ Email')));
  }).catch(() => {
    body.replaceChildren(h('p', {class: 'muted small',
      text: 'Sender identity could not be read just now.'}),
    h('p', {class: 'muted small'},
      h('a', {href: routeHref('settings-email')}, 'Manage in Settings ▸ Email')));
  });
  return panel;
}

// ---------------------------------------------------------------------------
// the at-a-glance strip
// ---------------------------------------------------------------------------

function glanceTile(label, value, detail, tone = 'muted') {
  return h('div', {class: 'glance-tile', 'data-tone': tone},
    h('span', {class: 'glance-label', text: label}),
    h('strong', {class: 'glance-value', text: value}),
    h('span', {class: 'glance-detail', text: detail}));
}

// Counts, not judgements — and only for the reads that landed. A certificate
// read that failed contributes no tile rather than a zero that would read as
// "nothing is wrong here".
function glanceStrip(domains, certificates) {
  const tiles = [];
  const active = domains.filter((row) => row.status === 'active').length;
  tiles.push(glanceTile('Domains', String(domains.length),
    domains.length ? `${active} active` : 'none registered here',
    domains.length && active === domains.length ? 'ok' : domains.length ? 'warn' : 'muted'));
  if (certificates) {
    // Three states, three sentences. "issuing" is neither valid nor broken, and
    // folding it into either would be the page telling a story the inventory
    // does not support.
    const broken = certificates.filter((row) => ['failed', 'expired', 'revoked'].includes(row.status)).length;
    const issuing = certificates.filter((row) => ['pending', 'issuing'].includes(row.status)).length;
    const detail = broken ? `${broken} not valid`
      : issuing ? `${certificates.length - issuing} valid · ${issuing} still issuing`
        : certificates.length ? 'all valid' : 'none requested';
    tiles.push(glanceTile('Certificates', String(certificates.length), detail,
      broken ? 'danger' : issuing ? 'warn' : certificates.length ? 'ok' : 'muted'));
  }
  return h('div', {class: 'glance-strip'}, ...tiles);
}

// ---------------------------------------------------------------------------
// the tab
// ---------------------------------------------------------------------------

/**
 * One domain is a page of its own (?domain=<id>), never a drawer over the
 * list. The list and the detail are the same route, so a row click is a plain
 * navigation and the browser Back button behaves the way an operator expects.
 */
export async function domainsTab(ctx, actions) {
  const root = h('div', {class: 'domains-tab'});
  // Legacy ?inspector=<id> deep links predate the page and must still land on
  // the domain. replaceState fires no hashchange, so this render continues.
  const entry = decodeRouteState().state;
  if (!entry.domain && /^\d+$/.test(String(entry.inspector || ''))) {
    history.replaceState({}, '', routeHref('domains', {domain: entry.inspector}));
  }

  function renderDetail(domain) {
    actions.replaceChildren(...[
      ctx.capabilities.manage_network ? h('button', {class: 'button ghost', onclick: () => editDomain(domain, render)}, 'Edit registrar settings') : null,
    ].filter(Boolean));
    // Whether THIS is the domain new apps in its workspace go live under — the
    // single most load-bearing fact about a domain, and one the page never
    // said. Taken from the workspace's own onboarding options, so it is the
    // server's answer rather than a guess from record shapes. Starts empty and
    // fills in: a domain that is not the apps domain simply never gains a line.
    //
    // Gated on manage_webapps because the read belongs to WebApps: a DNS-only
    // operator does not make the call at all, and sees no badge.
    const role = h('p', {class: 'domain-role'});
    if (domain.group?.id && ctx.capabilities.manage_webapps) {
      api(`/api/edge/webapp/onboarding/options?group=${encodeURIComponent(domain.group.id)}`)
        .then((options) => {
          if (options?.apps_domain?.id !== domain.id) return;
          role.replaceChildren(badge('Apps domain', 'success'),
            h('span', {text: 'New apps in this workspace go live under this domain — one wildcard record and one certificate cover every one of them.'}));
        })
        .catch(() => { /* a fact that cannot be read is simply not stated */ });
    }
    root.replaceChildren(
      h('p', {class: 'back-link'}, h('a', {href: routeHref('domains')}, '← All domains')),
      h('h2', {class: 'domains-detail-name', text: domain.name}),
      role,
      domainOverviewPanel(ctx, domain),
      h('dl', {class: 'details'},
        h('div', {}, h('dt', {text: 'Provider'}), h('dd', {text: domain.provider})),
        h('div', {}, h('dt', {text: 'Status'}), h('dd', {text: domain.status})),
        h('div', {}, h('dt', {text: 'Owner'}), h('dd', {text: domain.group?.name || 'Platform'})),
        h('div', {}, h('dt', {text: 'Expires'}), h('dd', {text: formatDate(domain.expires)}))),
      domainCertificatesPanel(ctx, domain),
      h('div', {class: 'activity-links'},
        h('a', {class: 'related-record', href: routeHref('dns', {domain: domain.id, return: returnLocation()})}, h('strong', {text: 'Open DNS records'}), icon('chevron')),
        h('a', {class: 'related-record', href: activityHref('logs', {type: 'model', id: domain.id, model: 'Domain'}, {return: returnLocation()})}, h('strong', {text: 'Related logs'}), icon('chevron'))));
  }

  function renderList(rows, failure, certificates) {
    actions.replaceChildren(...[
      ctx.capabilities.manage_network ? h('button', {class: 'button ghost', onclick: () => registerExisting(ctx, render)}, icon('plus'), 'Register existing') : null,
      ctx.user.is_superuser ? h('button', {class: 'button ghost', onclick: () => adoptDomain(ctx, render)}, icon('globe'), 'Adopt') : null,
      ctx.capabilities.manage_network ? h('button', {class: 'button primary', onclick: () => purchaseDomain(ctx, render)}, icon('plus'), 'Buy domain') : null,
    ].filter(Boolean));
    const panel = tablePanel('Managed domains',
      'Every domain has an owner: a group, or the platform itself. Ownership decides which apps can use it.');
    // A read that failed gets no strip: counts derived from nothing are a
    // claim the page cannot make.
    root.replaceChildren(...[failure ? null : glanceStrip(rows, certificates), panel].filter(Boolean));
    if (failure) { panel.append(errorState(failure, render)); return; }
    panel.append(new TableView({rows, empty: 'No managed domains yet.',
      onSelect: (row) => { location.hash = routeHref('domains', {domain: row.id}); }, columns: [
        {label: 'Domain', render: (row) => h('div', {}, h('strong', {text: row.name}), h('small', {text: row.provider}))},
        {label: 'Status', render: (row) => statusBadge(row.status)},
        {label: 'Owner', render: (row) => row.group?.name || 'Platform'},
        {label: 'Expires', render: (row) => formatDate(row.expires)},
        {label: '', render: (row) => h('a', {class: 'button compact', href: routeHref('dns', {domain: row.id}), onclick: (event) => event.stopPropagation()}, icon('dns'), 'Records')},
      ]}).render());
    const identity = emailIdentityPanel(ctx);
    if (identity) root.append(identity);
    // The raw editor is a tab of this destination, but it is the advanced half
    // of it — named as such, with the one protection it carries stated up front.
    root.append(h('p', {class: 'domains-advanced'},
      h('a', {href: routeHref('dns')}, '▸ Advanced: the raw DNS record editor'),
      h('span', {class: 'muted', text: ' — apex NS and SOA are protected and cannot be deleted here.'})));
  }

  // The domain read IS this page: until it lands there is nothing to look at,
  // and on a re-render the previous list would otherwise sit there reading as
  // current. The loader paints the result itself, so loadInto only owns the
  // wait — the read's own failure still becomes the list's in-panel error.
  async function render() {
    await loadInto(root, async () => {
      let rows = []; let failure = null;
      try { rows = await loadDomains(); } catch (error) { failure = error; }
      const wanted = queryParam('domain');
      // An id the caller cannot see (or that no longer exists) is not an error —
      // it is simply the list.
      const selected = wanted ? rows.find((row) => String(row.id) === String(wanted)) : null;
      if (selected) { renderDetail(selected); return; }
      // Only the list carries the strip, and only when the extra read lands.
      const certificates = await loadCertificates().catch(() => null);
      renderList(rows, failure, certificates);
    }, {message: 'Loading domains…', retry: render});
  }
  await render();
  return root;
}
