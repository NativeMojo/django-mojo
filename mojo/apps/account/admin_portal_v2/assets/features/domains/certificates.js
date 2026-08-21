// Certificates — the TLS inventory, and the per-domain panel the domain page
// carries.
//
// Ported from v1's advanced/page.js. Every read, every confirm, every refusal
// and the bounded issuance poll are v1's. What changed is the chrome: the tab
// shell owns the header and the actions slot, and the eyebrow says Domains
// rather than "Network & hosting".
//
// The mockup frames a certificate as TRUST rather than as dates, so the panel
// copy says so — a certificate is fit when a normal browser would accept it.
import {FormView, formatDate, h, icon, openModal, TableView} from '../../core.js';
import {loadInto, runAction} from '../../components/actions.js';
import {routeHref} from '../../components/routes.js';
import {
  confirmNetwork, loadCertificates, loadDomainCertificates, loadDomains,
  loadRetireEligibility, loadVhosts, mutationFailed, normalizedValues, postOnce,
  statusBadge, tablePanel,
} from '../../components/network.js';

export function certificateExpiry(row) {
  return row.days_remaining == null ? formatDate(row.not_after) : `${row.days_remaining} days`;
}

export function certificateRequest(domains, reload) {
  const form = new FormView({fields: [
    {name: 'domain', label: 'Managed domain', type: 'select', required: true, placeholder: 'Choose a domain', options: domains.map((row) => ({value: row.id, label: row.name}))},
    {name: 'names', label: 'Certificate names', type: 'textarea', rows: 4, help: 'One hostname per line. Leave empty for the domain default.'},
  ], submitLabel: 'Request certificate', onSubmit: async (values) => {
    const payload = {domain: values.domain}; const names = normalizedValues(values.names.split('\n'));
    if (names.length) payload.names = names;
    await postOnce('/api/dnsman/certificate/request', payload);
    close(); await reload();
  }});
  const close = openModal({title: 'Request certificate', subtitle: 'Issuance runs asynchronously; this portal polls status metadata only.', content: form.render()});
}

export async function removeFailedCertificate(row, reload) {
  const confirmed = await confirmNetwork({title: `Remove failed attempt for ${row.common_name}?`, copy: 'This removes the obsolete certificate attempt from the inventory. The failure remains in the job and application logs.', confirmLabel: 'Remove failed attempt', danger: true});
  if (!confirmed) return;
  await runAction(null, async () => {
    await postOnce('/api/dnsman/certificate/remove-failed', {certificate: row.id});
    await reload();
  }, {
    key: `certificate-remove-failed:${row.id}`,
    busy: {title: `Removing the failed attempt for ${row.common_name}…`},
    onError: (error) => mutationFailed('The failed attempt was not removed', error),
  });
}

export async function retireCertificate(row, covering, addressCount, reload) {
  const confirmed = await confirmNetwork({
    title: `Retire ${row.common_name}?`,
    copy: `${covering?.common_name || 'The replacement certificate'} takes over ${addressCount} addresses now served by ${row.common_name}, then the retired certificate is removed from the inventory.`,
    confirmLabel: 'Retire certificate', danger: true});
  if (!confirmed) return;
  // The confirm was the human's part; the retirement moves live traffic onto
  // the covering certificate and rebuilds the row the button sat in.
  await runAction(null, async () => {
    await postOnce('/api/dnsman/certificate/retire', {certificate: row.id});
    await reload();
  }, {
    key: `certificate-retire:${row.id}`,
    busy: {title: `Retiring ${row.common_name}…`, detail: 'Moving its addresses onto the covering certificate.'},
    onError: (error) => mutationFailed('The certificate was not retired', error),
  });
}

/** The certificates block on one domain's page. */
export function domainCertificatesPanel(ctx, domain) {
  const panel = h('section', {class: 'panel'});
  // The heading is painted synchronously and the reads land underneath it, so
  // the loading state goes in `body` — replacing the panel would take the
  // heading and the Request certificate button with it.
  const body = h('div', {});
  async function render() {
    panel.replaceChildren(h('div', {class: 'panel-head'}, h('div', {},
      h('h2', {text: 'Certificates'}),
      h('p', {text: 'TLS coverage for this domain. Private key material is never shown.'})),
    ctx.capabilities.manage_network ? h('button', {class: 'button compact', onclick: () => certificateRequest([domain], render)}, icon('certificate'), 'Request certificate') : null),
    body);
    await loadInto(body, async (current) => {
      const [rows, eligibility, vhosts] = await Promise.all([
        loadDomainCertificates(domain.id),
        loadRetireEligibility(domain.id).catch(() => ({})),
        loadVhosts().catch(() => []),
      ]);
      if (!current()) return;
      body.replaceChildren();
      const wildcardName = `*.${domain.name}`;
      const headline = rows.find((row) => row.status === 'active' && [row.common_name, ...(row.sans || [])].includes(wildcardName)) || null;
      const addressCount = (row) => vhosts.filter((vhost) => Number(vhost.certificate?.id || vhost.certificate) === Number(row.id)).length;
      const retireButton = (row) => {
        const coveringId = eligibility[String(row.id)];
        if (!coveringId || !ctx.capabilities.manage_network) return null;
        const covering = rows.find((item) => Number(item.id) === Number(coveringId));
        return h('button', {class: 'button compact', onclick: (event) => {
          event.stopPropagation();
          return retireCertificate(row, covering, addressCount(row), render);
        }}, 'Retire — use wildcard');
      };
      if (headline) {
        body.append(h('div', {class: 'result-card cert-headline'},
          h('div', {},
            h('strong', {text: [headline.common_name, ...(headline.sans || []).filter((name) => name !== headline.common_name)].join(', ')}),
            h('span', {text: 'Covers every app on this domain — current and future.'})),
          statusBadge(headline.status),
          h('strong', {text: headline.renew_after ? `Renews ${formatDate(headline.renew_after)}` : `Expires ${certificateExpiry(headline)}`})));
      }
      const others = rows.filter((row) => row !== headline);
      body.append(new TableView({rows: others, empty: headline ? 'No other certificates on this domain.' : 'No certificates have been requested for this domain.', columns: [
        {label: 'Certificate', render: (row) => h('div', {}, h('strong', {text: row.common_name}), h('small', {text: (row.sans || []).join(', ')}))},
        {label: 'Status', render: (row) => statusBadge(row.status)},
        {label: 'Expires', render: (row) => certificateExpiry(row)},
        {label: '', render: (row) => h('div', {class: 'form-actions'},
          retireButton(row),
          ctx.capabilities.manage_network && row.status === 'failed' ? h('button', {class: 'icon-button danger-text', 'aria-label': `Remove failed certificate attempt for ${row.common_name}`, onclick: (event) => { event.stopPropagation(); return removeFailedCertificate(row, render); }}, icon('trash')) : null)},
      ]}).render());
    }, {message: 'Loading certificates…', retry: render});
  }
  render();
  return panel;
}

/**
 * The Certificates tab: the whole inventory.
 *
 * `actions` is the tab shell's header slot — the Request certificate button
 * has to survive this body's own reloads, so it lives up there exactly like
 * Infrastructure's Refresh.
 */
export async function certificatesTab(ctx, actions) {
  const root = h('div', {class: 'domains-tab'}); let pollTicks = 0; let pollTimer = null;
  async function render() {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    const domains = await loadDomains(); const rows = await loadCertificates();
    actions.replaceChildren(...[
      ctx.capabilities.manage_network ? h('button', {class: 'button primary', onclick: () => certificateRequest(domains.filter((row) => row.status === 'active'), render)}, icon('certificate'), 'Request certificate') : null,
    ].filter(Boolean));
    const panel = h('section', {class: 'panel'}, h('div', {class: 'panel-head'}, h('div', {},
      h('h2', {text: 'TLS certificates'}),
      h('p', {}, 'Fitness means a normal browser would trust it — issuer and environment count, not just dates. ',
        h('a', {href: routeHref('domains'), text: 'Manage certificates per-domain from the domain page.'})))));
    root.replaceChildren(panel);
    panel.append(new TableView({rows, empty: 'No certificates have been requested.', columns: [
      {label: 'Certificate', render: (row) => h('div', {}, h('strong', {text: row.common_name}), h('small', {text: (row.sans || []).join(', ')}))},
      {label: 'Status', render: (row) => statusBadge(row.status)},
      {label: 'Domain', render: (row) => row.domain?.name || '—'},
      {label: 'Issuer', render: (row) => row.issuer || 'Pending'},
      {label: 'Expires', render: (row) => certificateExpiry(row)},
      {label: '', render: (row) => ctx.capabilities.manage_network && row.status === 'failed' ? h('button', {class: 'icon-button danger-text', 'aria-label': `Remove failed certificate attempt for ${row.common_name}`, onclick: (event) => { event.stopPropagation(); return removeFailedCertificate(row, render); }}, icon('trash')) : null},
    ]}).render());
    if (rows.some((row) => ['pending', 'issuing'].includes(row.status)) && pollTicks < 36 && location.hash.startsWith('#/certificates')) {
      pollTicks += 1; pollTimer = setTimeout(() => render().catch(() => {}), 10000);
    }
  }
  // Two reads before anything paints. The poll keeps calling render() directly —
  // a background tick must never blank the table the operator is reading.
  async function load() {
    await loadInto(root, () => render(), {message: 'Loading certificates…', retry: load});
  }
  await load();
  root.dispose = () => { if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; } };
  return root;
}
