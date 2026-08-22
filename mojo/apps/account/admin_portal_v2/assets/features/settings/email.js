// Email — the SES domains and mailboxes behind every email this installation
// sends. The list reads only persisted state (the last SES audit's verdict);
// the live re-check lives one deliberate click behind each domain's Details,
// where "Check now" runs the real audit and refreshes the persisted fields.
//
// v1's Email page, ported whole: the same posture rows, the same domain and
// mailbox lists, the same make-default confirm, the same domain audit, and
// the same send-a-test-email section under the same `features.email.
// capabilities.manage` gate. The v2 change is the chrome — this is a sub-page
// of Settings now, so it wears the back pill and Settings' eyebrow.

import {api, formatDate, h} from '../../core.js';
import {backPill} from '../../app.js';
import {rowSection, statusRow} from '../../components/rows.js';
import {emptyState, errorState, loadingState} from '../../components/views.js';
import {makeDefaultControl, openDomainAudit, testSendSection} from './email_panels.js';

const SUMMARY_URL = '/api/aws/email/summary';

function domainTone(domain) {
  if (domain.can_send) return 'ok';
  if (domain.status === 'pending') return 'muted';
  return 'warn';
}

function domainValue(domain) {
  if (domain.can_send && domain.can_recv) return 'Sending and receiving ready';
  if (domain.can_send) return 'Ready to send';
  if (domain.status === 'pending') return 'Never checked';
  return 'Not ready';
}

function postureSection(report) {
  const posture = report.posture || {};
  const defaultBox = (report.mailboxes || []).find((row) => row.is_system_default);
  const rows = [];
  if (posture.default_sender_conflict) {
    rows.push(statusRow({tone: 'warn', name: 'Default sender',
      value: 'Conflict', detail: 'more than one mailbox claims the system default — set one below to resolve it', detailTone: 'warning'}));
  } else if (posture.default_sender_configured && defaultBox) {
    rows.push(statusRow({tone: 'ok', name: 'Default sender',
      value: defaultBox.email, mono: true,
      detail: 'system email sends as this mailbox'}));
  } else {
    rows.push(statusRow({tone: 'warn', name: 'Default sender',
      value: 'Not set', detail: 'pick a mailbox below so system email has a sender'}));
  }
  rows.push(posture.templates_installed
    ? statusRow({tone: 'ok', name: 'Email templates', value: 'Installed'})
    : statusRow({tone: 'warn', name: 'Email templates',
      value: `${posture.missing_template_count || 0} missing`,
      detail: 'shipped templates are installed by System Setup'}));
  return rowSection('Sending posture', rows);
}

function domainsSection(report, reload) {
  const rows = (report.domains || []).map((domain) => {
    const row = statusRow({
      tone: domainTone(domain), name: domain.name, mono: true,
      value: domainValue(domain),
      detail: `last checked ${formatDate(domain.checked_at)}`,
      action: {label: 'Details', href: '#'},
    });
    const link = row.querySelector('.row-link');
    if (link) {
      link.addEventListener('click', (event) => {
        event.preventDefault();
        openDomainAudit(domain, reload);
      });
    }
    return row;
  });
  if (report.domain_count > (report.domains || []).length) {
    rows.push(statusRow({tone: 'muted', name: '…',
      value: `${report.domain_count - report.domains.length} more domains not shown`}));
  }
  return rowSection('SES domains', rows);
}

function mailboxValue(box) {
  const parts = [];
  if (box.is_system_default) parts.push('System default');
  if (box.is_domain_default) parts.push('Domain default');
  if (!box.allow_outbound) parts.push('Outbound disabled');
  else if (!parts.length) parts.push('Can send');
  return parts.join(' · ');
}

function mailboxesSection(report, reload, canManage) {
  const rows = (report.mailboxes || []).map((box) => {
    const spec = {
      tone: box.is_system_default ? 'ok' : box.allow_outbound ? 'muted' : 'muted',
      name: box.email, mono: true, value: mailboxValue(box),
      detail: box.allow_inbound ? 'receives inbound mail' : '',
    };
    const row = statusRow(spec);
    if (canManage && box.allow_outbound && !box.is_system_default) {
      row.append(makeDefaultControl(box, reload));
    }
    return row;
  });
  if (report.mailbox_count > (report.mailboxes || []).length) {
    rows.push(statusRow({tone: 'muted', name: '…',
      value: `${report.mailbox_count - report.mailboxes.length} more mailboxes not shown`}));
  }
  return rowSection('Mailboxes', rows);
}

// System Setup has not been rebuilt in v2, so the one link out of the empty
// state opens the current Admin and says so rather than pointing at a v2 route
// that does not exist.
function setupHref(ctx) {
  return `${ctx.admin_path || '/admin/'}#/setup?focus=aws_email`;
}

export async function emailPage(ctx) {
  const body = h('div', {class: 'email-body'}, loadingState('Loading…'));
  const root = h('div', {class: 'page row-page email-page settings-sub-page'},
    backPill('Settings', 'settings'),
    h('header', {class: 'page-header'},
      h('div', {},
        h('div', {class: 'eyebrow', text: 'Settings · Integrations'}),
        h('h1', {text: 'Email', tabindex: '-1'}),
        h('p', {text: 'The SES domains and mailboxes behind every email this installation sends.'}))),
    body);
  async function load() {
    body.replaceChildren(loadingState('Loading…'));
    try {
      const report = await api(SUMMARY_URL);
      if (!(report.domains || []).length) {
        const empty = emptyState('No email domain is configured',
          'System Setup connects a verified SES domain and picks the default sender.');
        if (ctx.capabilities?.setup) {
          empty.append(h('p', {},
            h('a', {class: 'row-link', href: setupHref(ctx), text: 'Open System Setup'}),
            h('span', {class: 'email-note', text: ' · opens the current Admin'})));
        }
        body.replaceChildren(empty);
        return;
      }
      const canManage = ctx.features?.email?.capabilities?.manage === true;
      body.replaceChildren(...[
        postureSection(report),
        domainsSection(report, load),
        mailboxesSection(report, load, canManage),
        canManage ? testSendSection(report) : null,
      ].filter(Boolean));
    } catch (error) {
      body.replaceChildren(errorState(error, () => load()));
    }
  }
  await load();
  return root;
}
