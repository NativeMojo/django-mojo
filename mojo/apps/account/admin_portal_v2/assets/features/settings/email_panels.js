// Email panels: the per-domain drill-in (live audit + "Check now"), the
// inline make-default confirm, and the test-send section. The drill-in is
// where AWS is actually contacted — the list itself never pays that cost.
//
// The drill-in is a centered modal: it is a read-only peek at evidence about a
// domain the page already lists, not a record with a life of its own.
//
// Ported from v1 unchanged: same endpoints, same audit summary, same confirm
// choreography, same test-send payload and the same plain-words failures.

import {api, h} from '../../core.js';
import {runAction} from '../../components/actions.js';
import {openModal} from '../../components/overlays.js';
import {loadingState} from '../../components/views.js';

const AUDIT_URL = (id) => `/api/aws/email/domain/${id}/audit`;
const TEST_URL = '/api/aws/email/test';
const DEFAULT_URL = '/api/aws/email/mailbox-default';

function field(label, control, hint) {
  return h('label', {class: 'field'}, h('span', {text: label}), control,
    hint ? h('small', {text: hint}) : null);
}

// The raw audit rows (AWS strings, ARNs) stay behind a disclosure: the panel
// leads with plain words, and a reader who needs the evidence still gets it.
function technicalDetails(data) {
  return h('details', {class: 'email-technical disclosure'},
    h('summary', {text: 'Technical details'}),
    h('pre', {class: 'evidence-json', text: JSON.stringify(data ?? {}, null, 2)}));
}

function auditSummary(data) {
  const checks = data.checks || {};
  const lines = [];
  lines.push(checks.ses_verified
    ? {tone: 'ok', text: 'The domain is verified with SES.'}
    : {tone: 'danger', text: 'The domain is not verified with SES yet.'});
  if (checks.ses_verified) {
    lines.push(checks.ses_production_access
      ? {tone: 'ok', text: 'The account has SES production access.'}
      : {tone: 'warn', text: 'The account is still in the SES sandbox — only verified recipients receive mail.'});
  }
  const recommendations = (data.recommendations || []).map((row) =>
    h('li', {},
      h('strong', {text: row.action || ''}),
      row.explanation ? h('p', {class: 'muted small', text: row.explanation}) : null));
  return h('div', {},
    h('p', {class: data.audit_pass ? 'email-note' : 'email-note warning',
      text: data.audit_pass
        ? 'Everything this domain needs is in place.'
        : 'The last check found things to fix.'}),
    ...lines.map((line) => h('p', {class: `email-check email-check-${line.tone}`, text: line.text})),
    recommendations.length ? h('ul', {class: 'email-recommendations'}, ...recommendations) : null,
    technicalDetails(data.items));
}

export function openDomainAudit(domain, reload) {
  const results = h('div', {class: 'email-audit', 'aria-live': 'polite'});
  const check = h('button', {class: 'button compact', type: 'button'}, 'Check now');
  let dirty = false;
  // The results block is a sibling of the button, so the pending state on
  // "Check now" and the answer it produces do not destroy each other.
  function runAudit() {
    return runAction(check, async () => {
      results.replaceChildren(loadingState('Checking with AWS…'));
      const data = await api(AUDIT_URL(domain.id));
      dirty = true; // the audit persisted fresh readiness fields
      results.replaceChildren(auditSummary(data));
    }, {
      pendingLabel: 'Checking…',
      onError: (error) => {
        results.replaceChildren(h('p', {class: 'email-note warning',
          text: `Could not check right now — ${error.message || 'AWS did not answer'}. Showing the last saved state.`}));
      },
    });
  }
  check.addEventListener('click', () => runAudit());
  const close = openModal({
    title: domain.name, wide: true,
    content: h('div', {class: 'email-domain-detail'},
      h('dl', {class: 'details'},
        h('div', {}, h('dt', {text: 'Status'}), h('dd', {text: domain.status})),
        h('div', {}, h('dt', {text: 'Region'}), h('dd', {class: 'mono', text: domain.region || ''})),
        h('div', {}, h('dt', {text: 'Sending'}),
          h('dd', {text: domain.can_send ? 'ready' : 'not ready'})),
        h('div', {}, h('dt', {text: 'Receiving'}),
          h('dd', {text: domain.receiving_enabled
            ? (domain.can_recv ? 'ready' : 'enabled, not ready') : 'not enabled'}))),
      h('div', {class: 'form-actions'}, check),
      results),
    onClose: () => { if (dirty && reload) reload(); },
  });
  runAudit();
  return close;
}

// Making a mailbox the system default changes what every system email sends
// as, so the control confirms inline — on the row, in sight of the address.
export function makeDefaultControl(box, reload) {
  const host = h('span', {class: 'email-default-control'});
  const message = h('span', {class: 'email-note warning'});
  function offer() {
    const start = h('button', {class: 'button ghost compact', type: 'button'}, 'Make default');
    start.addEventListener('click', () => confirm());
    host.replaceChildren(start);
  }
  function confirm() {
    const yes = h('button', {class: 'button compact', type: 'button'}, `Send as ${box.email}`);
    const no = h('button', {class: 'button ghost compact', type: 'button'}, 'Cancel');
    // The confirm is already on screen, so this click is past the human-input
    // step: the pending state belongs on the confirm button itself. Success
    // reloads the list out from under it, and a failure rebuilds the offer, so
    // neither path restores it.
    yes.addEventListener('click', () => runAction(yes, async () => {
      no.setAttribute('aria-disabled', 'true');
      await api(DEFAULT_URL, {method: 'POST', body: JSON.stringify(
        {mailbox: box.id, scope: 'system'})});
      if (reload) await reload();
    }, {
      pendingLabel: 'Applying…', restoreOnSuccess: false,
      onError: (error) => {
        message.textContent = error.message || 'The change was refused.';
        offer();
        host.append(message);
      },
    }));
    no.addEventListener('click', offer);
    host.replaceChildren(yes, no);
  }
  offer();
  return host;
}

function resultLine(result) {
  if (result.sent === false) {
    return h('p', {class: 'email-result email-result-danger', role: 'status',
      text: result.error || 'The send failed.'});
  }
  if (result.status === 'failed') {
    return h('p', {class: 'email-result email-result-danger', role: 'status',
      text: `SES refused the message — ${result.status_reason || 'no reason given'}`});
  }
  return h('p', {class: 'email-result email-result-ok', role: 'status',
    text: `Handed to SES${result.message_id ? ` — message ${result.message_id}` : ''}`});
}

export function testSendSection(report) {
  const senders = (report.mailboxes || []).filter((row) => row.allow_outbound);
  if (!senders.length) return null;
  const fromSelect = h('select', {},
    ...senders.map((row) => h('option', {value: row.email, text: row.email})));
  const preferred = senders.find((row) => row.is_system_default);
  if (preferred) fromSelect.value = preferred.email;
  const toInput = h('input', {autocomplete: 'off', placeholder: 'you@example.com'});
  const subjectInput = h('input', {autocomplete: 'off', value: 'Test email from the Admin portal'});
  const results = h('div', {class: 'email-results', 'aria-live': 'polite'});
  const send = h('button', {class: 'button', type: 'button'}, 'Send test email');
  send.addEventListener('click', () => {
    if (!toInput.value.trim()) {
      results.replaceChildren(resultLine({sent: false, error: 'Enter a recipient address first.'}));
      return undefined;
    }
    return runAction(send, async () => {
      const result = await api(TEST_URL, {method: 'POST', body: JSON.stringify({
        from_email: fromSelect.value, to: toInput.value.trim(),
        subject: subjectInput.value.trim(),
        body_text: 'This is a test email sent from the built-in Admin portal.'})});
      results.replaceChildren(resultLine(result));
    }, {
      pendingLabel: 'Sending…',
      onError: (error) => { results.replaceChildren(resultLine({sent: false, error: error.message})); },
    });
  });
  return h('div', {class: 'email-test'},
    h('h2', {text: 'Send a test email'}),
    h('p', {class: 'email-note', text: 'Failures come back in plain words — a sandbox account, an unverified domain, or a disabled mailbox each say so instead of erroring.'}),
    field('From', fromSelect),
    field('To', toInput),
    field('Subject', subjectInput),
    results,
    h('div', {class: 'form-actions'}, send));
}
