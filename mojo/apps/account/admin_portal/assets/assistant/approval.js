// The approval seam, in one file.
//
// NOTHING here is authority. The panel never decides that an operation is
// permitted, never remembers that one was granted, and re-asks the server on
// every attempt. The card is a rendering of a server-owned PendingAction; the
// two buttons submit a decision and nothing else.
//
// `args`, `description` and `preview` come from a language model and are
// rendered as TEXT, never as HTML.
//
// Two transports, one decision:
//   requires_fresh_auth false -> assistant_approval over the socket
//   requires_fresh_auth true  -> POST /api/assistant/action, because the
//                                socket authenticates once at connect and
//                                holds no per-message token. The WebSocket
//                                answers `reauth_required`; the panel raises
//                                the shell's own step-up and re-submits the
//                                SAME action_id over REST.

import {api, FreshAuthRequired, h} from '../core.js';
import {runAction} from '../components/actions.js';

const LIVE = 'pending';
const STATE_COPY = {
  pending: 'Waiting for a decision.',
  executing: 'Running…',
  completed: 'Completed.',
  failed: 'This operation ran and failed.',
  canceled: 'Canceled.',
  expired: 'This request expired before it was answered.',
  superseded: 'A newer request replaced this one.',
};

function text(value) {
  if (value === null || value === undefined) return '';
  if (typeof value === 'object') {
    try { return JSON.stringify(value); } catch (_) { return String(value); }
  }
  return String(value);
}

function expired(block) {
  const at = Date.parse(block?.expires_at || '');
  return Number.isFinite(at) ? at <= Date.now() : false;
}

function actionable(block) {
  return block?.state === LIVE && !expired(block);
}

function remaining(block) {
  const at = Date.parse(block?.expires_at || '');
  if (!Number.isFinite(at)) return '';
  const seconds = Math.max(0, Math.round((at - Date.now()) / 1000));
  if (!seconds) return 'Expired';
  if (seconds < 60) return `Expires in ${seconds}s`;
  const minutes = Math.round(seconds / 60);
  return minutes < 60 ? `Expires in ${minutes} min`
    : `Expires in ${Math.round(minutes / 60)}h`;
}

// The shell's existing step-up. core.js owns the 440 case on every REST call;
// this is the WebSocket's `reauth_required`, which no HTTP status carries.
function stepUp() {
  return new Promise((resolve, reject) => {
    const error = new FreshAuthRequired('/api/assistant/action');
    const detail = {error, resolve, reject, handled: false};
    window.dispatchEvent(new CustomEvent('mojo-admin:fresh-auth', {detail}));
    if (!detail.handled) reject(error);
  });
}

async function resolveOverRest(block, decision) {
  // core.js re-enters the step-up once on a 440 and retries the same body, so
  // the SAME action_id is re-submitted rather than a new card being requested.
  const data = await api('/api/assistant/action', {
    method: 'POST',
    body: JSON.stringify({
      action_id: block.action_id, decision,
      ...(block.conversation_id ? {conversation_id: block.conversation_id} : {}),
    }),
  });
  return data?.action || null;
}

async function currentState(block) {
  // After any refusal the server, not the browser, says what the card is now.
  try {
    const query = block.conversation_id
      ? `?conversation=${encodeURIComponent(block.conversation_id)}` : '';
    const data = await api(`/api/assistant/action${query}`);
    const rows = Array.isArray(data?.actions) ? data.actions : [];
    return rows.find((row) => row && row.action_id === block.action_id) || null;
  } catch (_) {
    return null;
  }
}

export function renderApprovalBlock(block, ctx = {}) {
  if (!block || typeof block !== 'object' || !block.action_id) return null;
  const host = h('div', {class: 'assistant-approval-host'});
  let timer = null;

  const submit = async (decision) => {
    let resolved = null;
    let failure = '';
    try {
      if (!block.requires_fresh_auth && ctx.transport?.isReady?.()) {
        const outcome = await ctx.transport.resolveApproval({
          conversationId: block.conversation_id ?? ctx.conversationId ?? null,
          actionId: block.action_id, decision,
        });
        if (outcome?.type === 'assistant_approval_result') {
          resolved = outcome.block || null;
        } else if (outcome?.code === 'reauth_required') {
          await stepUp();
          resolved = await resolveOverRest(block, decision);
        } else {
          failure = String(outcome?.error || 'This action is no longer available.');
        }
      } else {
        resolved = await resolveOverRest(block, decision);
      }
    } catch (error) {
      if (error?.code === 'fresh_auth_required') return;
      failure = String(error?.message || 'This action could not be resolved.');
    }
    if (!resolved) resolved = await currentState(block);
    // A tool that RAN and failed comes back 200 with state "failed" -- that is
    // a failure card, not a network error, and the operator has to be told the
    // mutation was attempted.
    paint(resolved || {...block, state: 'expired'}, failure);
    ctx.onResolved?.(resolved || null);
  };

  function paint(current, failure = '') {
    if (timer) { clearInterval(timer); timer = null; }
    const live = actionable(current);
    const args = current.args && typeof current.args === 'object' ? current.args : {};
    const rows = Object.entries(args).slice(0, 20);
    const approve = h('button', {class: 'button primary compact', type: 'button'}, 'Approve');
    const cancel = h('button', {class: 'button ghost compact', type: 'button'}, 'Cancel');
    // Double-submit protection only. The server re-checks everything and runs
    // the handler exactly once regardless of what the browser does.
    const guard = (decision, button) => runAction(button, () => {
      approve.setAttribute('aria-disabled', 'true');
      cancel.setAttribute('aria-disabled', 'true');
      return submit(decision);
    }, {key: `approval:${current.action_id}`, announceLabel: 'Submitting your decision'});
    approve.addEventListener('click', (event) => guard('approve', event.currentTarget));
    cancel.addEventListener('click', (event) => guard('cancel', event.currentTarget));

    const card = h('div', {class: `assistant-approval is-${text(current.state) || 'pending'}`},
      h('header', {},
        h('strong', {text: text(current.title) || text(current.tool) || 'Approval required'}),
        h('span', {text: `${text(current.tool)}${live ? ` · ${remaining(current)}` : ''}`})),
      current.description ? h('p', {text: text(current.description)}) : null,
      rows.length ? h('dl', {class: 'assistant-args'},
        ...rows.flatMap(([key, value]) => [h('dt', {text: text(key)}),
          h('dd', {text: text(value)})])) : null,
      current.preview && current.preview.summary
        ? h('p', {text: text(current.preview.summary)}) : null,
      current.requires_fresh_auth
        ? h('p', {text: 'You will be asked to confirm your identity before this runs.'}) : null,
      // A failure on a card the server still reports as pending (a dropped
      // socket, a bounded wait that ran out) has to be said out loud, or the
      // operator re-approves an action that may already have run.
      live && failure ? h('p', {text: failure}) : null,
      live ? h('footer', {}, cancel, approve)
        : h('div', {class: 'assistant-state',
          text: failure || STATE_COPY[current.state] || 'This request is closed.'}),
      live ? null : (current.failure_code
        ? h('p', {text: `Reason: ${text(current.failure_code)}`}) : null),
      !live && current.result && current.result.message
        ? h('p', {text: text(current.result.message)}) : null);
    host.replaceChildren(card);
    if (live) {
      timer = setInterval(() => {
        if (!host.isConnected) { clearInterval(timer); timer = null; return; }
        if (expired(current)) { paint({...current, state: 'expired'}); return; }
        card.querySelector('header span').textContent =
          `${text(current.tool)} · ${remaining(current)}`;
      }, 1000);
    }
  }

  paint(block);
  return host;
}

export function renderActionBlock(block, ctx = {}) {
  // The legacy quick-reply. Its buttons replay `value` as an ordinary chat
  // message; it carries no authority and never has.
  const actions = Array.isArray(block?.actions) ? block.actions.slice(0, 8) : [];
  if (!actions.length) return null;
  const usable = actions.filter((entry) => entry && typeof entry === 'object'
    && typeof entry.value === 'string' && entry.value);
  if (!usable.length) return null;
  return h('div', {class: 'assistant-block'},
    block.title ? h('h5', {text: text(block.title)}) : null,
    block.description ? h('p', {class: 'assistant-note', text: text(block.description)}) : null,
    h('div', {class: 'assistant-quickreply'}, ...usable.map((entry) => h('button', {
      class: 'button ghost compact', type: 'button',
      onclick: (event) => runAction(event.currentTarget, async () => {
        ctx.transport?.quickReply(entry.value,
          block.conversation_id ?? ctx.conversationId ?? null, block.action_id);
        ctx.onQuickReply?.(entry.value);
      }, {announceLabel: 'Sending…'}),
    }, text(entry.label) || text(entry.value)))));
}
