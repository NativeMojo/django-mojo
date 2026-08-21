// The panel body: history, the live turn, and the composer.
//
// REST carries history; the WebSocket carries the turn. There is deliberately
// no REST fallback for a turn -- POST /api/assistant runs the entire multi-turn
// agent loop inside one request, and a second transport would be a second
// correlation owner.

import {api, apiEnvelope, formatDate, h, icon} from '../core.js';
import {announce, loadInto, runAction, toast} from '../components/actions.js';
import {confirmAction} from '../components/overlays.js';
import {degradedState, emptyState, loadingState} from '../components/views.js';
import {renderBlocks} from './blocks.js';
import {renderMarkdown} from './markdown.js';
import {applyPlanUpdate, planTracker} from './plan.js';
import {createTransport} from './transport.js';

const HISTORY_SIZE = 25;

function bubble(kind, ...children) {
  return h('div', {class: `assistant-message is-${kind}`},
    h('div', {class: 'assistant-bubble'}, ...children));
}

export function mountConversation({ctx, panel}) {
  const capabilities = panel.capabilities || {};
  const body = panel.body;
  let conversationId = null;
  let planNode = null;
  let busyRow = null;
  let disposed = false;
  let closeWhenIdle = false;

  const thread = h('div', {class: 'assistant-thread', 'aria-live': 'off'});
  const history = h('div', {class: 'assistant-history', hidden: true});
  const input = h('textarea', {rows: 2, 'aria-label': 'Message the Assistant',
    placeholder: 'Ask about this installation…'});
  const send = h('button', {class: 'button primary compact', type: 'button'}, 'Send');
  const composer = h('div', {class: 'assistant-composer'}, input, send);

  const transport = createTransport({
    onEvent: (event) => onEvent(event),
    onStatus: ({state, detail}) => {
      if (disposed) return;
      panel.status.classList.toggle('is-error', state === 'failed' || state === 'terminal');
      panel.status.textContent = state === 'online' ? '' : detail || '';
      if (state === 'terminal' && detail) revoke(detail);
    },
  });

  function revoke(message) {
    // Authority loss is not a degraded state: the panel goes away rather than
    // sitting there offering a composer that will be refused.
    if (disposed) return;
    disposed = true;
    toast(message, {tone: 'danger'});
    panel.revoke();
  }

  function scroll() {
    thread.scrollTop = thread.scrollHeight;
  }

  function appendAssistant(text, blocks) {
    const children = [];
    if (text) children.push(renderMarkdown(text));
    const node = bubble('assistant', ...children);
    (blocks && blocks.length ? renderBlocks(blocks, {conversationId}) : [])
      .forEach((block) => node.append(block));
    thread.append(node);
    scroll();
    return node;
  }

  function appendUser(text) {
    // A user's own message is their literal text; running it through the
    // markdown renderer would reformat what they actually typed.
    thread.append(bubble('user', h('span', {text})));
    scroll();
  }

  function appendError(message) {
    // In the thread, never a toast: a toast scrolls away and the operator is
    // left with a turn that simply stopped.
    thread.append(bubble('error', h('span', {text: message})));
    scroll();
  }

  function setBusy(on, label = 'Thinking') {
    if (!on) {
      busyRow?.remove();
      busyRow = null;
      input.disabled = false;
      send.removeAttribute('aria-disabled');
      return;
    }
    if (!busyRow) {
      busyRow = h('div', {class: 'assistant-busy'},
        h('span', {class: 'assistant-dots'}, h('i'), h('i'), h('i')),
        h('span', {class: 'assistant-busy-label', text: label}));
      thread.append(busyRow);
    } else {
      busyRow.querySelector('.assistant-busy-label').textContent = label;
    }
    input.disabled = true;
    send.setAttribute('aria-disabled', 'true');
    scroll();
  }

  function onEvent(event) {
    if (disposed) return;
    if (event.type === 'assistant_socket_ready') {
      if (closeWhenIdle) transport.stop();
      return;
    }
    if (event.conversation_id != null && !conversationId) {
      conversationId = event.conversation_id;
    }
    if (event.type === 'assistant_thinking') { setBusy(true); return; }
    if (event.type === 'assistant_tool_call') {
      setBusy(true, `Running ${String(event.tool || 'a tool')}`);
      return;
    }
    if (event.type === 'assistant_text') {
      appendAssistant(event.text, event.blocks);
      return;
    }
    if (event.type === 'assistant_plan') {
      planNode = planTracker(event.plan);
      if (planNode) { thread.append(planNode); scroll(); }
      return;
    }
    if (event.type === 'assistant_plan_update') {
      applyPlanUpdate(planNode, event);
      return;
    }
    if (event.type === 'assistant_response') {
      setBusy(false);
      appendAssistant(event.response, event.blocks);
      announce('The assistant answered.');
      if (closeWhenIdle) transport.stop();
      return;
    }
    if (event.type === 'assistant_error') {
      setBusy(false);
      appendError(String(event.error || 'The assistant could not answer.'));
      announce('The assistant reported an error.');
      if (closeWhenIdle) transport.stop();
    }
  }

  // --- history -------------------------------------------------------------

  async function loadHistoryList() {
    // The explicit owner filter is REQUIRED. Conversation.VIEW_PERMS accepts
    // view_admin outright and `owner` is only the fallback, so an unfiltered
    // list hands this operator every other operator's conversation titles.
    const page = await apiEnvelope(
      `/api/assistant/conversation?user=${encodeURIComponent(ctx.user.id)}`
      + `&size=${HISTORY_SIZE}&sort=-modified`);
    const rows = page.items || [];
    if (!rows.length) {
      history.replaceChildren(emptyState('No conversations yet'));
      return;
    }
    history.replaceChildren(...rows.map((row) => h('div', {class: 'assistant-history-row'},
      h('button', {class: 'assistant-history-open', type: 'button',
        onclick: (event) => runAction(event.currentTarget, () => openConversation(row.id),
          {announceLabel: 'Loading conversation…'})},
      h('strong', {text: row.title || `Conversation ${row.id}`}),
      h('span', {text: formatDate(row.modified)})),
      h('button', {class: 'icon-button', type: 'button', 'aria-label': 'Delete conversation',
        onclick: (event) => runAction(event.currentTarget, () => remove(row.id))},
      icon('trash')))));
  }

  async function remove(id) {
    const {confirmed} = await confirmAction({
      title: 'Delete this conversation?',
      copy: 'The conversation and its messages are removed. This cannot be undone.',
      confirmLabel: 'Delete', danger: true,
    });
    if (!confirmed) return;
    await api(`/api/assistant/conversation/${id}`, {method: 'DELETE'});
    if (conversationId === id) startNew();
    await loadHistoryList();
    toast('Conversation deleted.');
  }

  async function openConversation(id) {
    const detail = await api(`/api/assistant/conversation/${id}?graph=detail`);
    conversationId = id;
    planNode = null;
    const states = new Map();
    (detail.pending_actions || []).forEach((action) => {
      if (action && action.action_id) states.set(action.action_id, action);
    });
    const nodes = [];
    (detail.messages || []).forEach((message) => {
      // tool_use / tool_result rows carry raw payloads, not operator content.
      if (message.role === 'user') { nodes.push(bubble('user', h('span', {text: message.content || ''}))); return; }
      if (message.role !== 'assistant') return;
      if (!message.content && !(message.blocks || []).length) return;
      const node = bubble('assistant', message.content ? renderMarkdown(message.content) : null);
      // The block embedded in an old message is the card AS PROPOSED, so its
      // `state` is stale; the conversation's pending_actions is the truth.
      const blocks = (message.blocks || []).map((block) => (
        block && block.action_id && states.has(block.action_id)
          ? states.get(block.action_id) : block));
      renderBlocks(blocks, {conversationId: id}).forEach((block) => node.append(block));
      nodes.push(node);
    });
    thread.replaceChildren(...(nodes.length ? nodes
      : [emptyState('This conversation has no messages yet')]));
    history.hidden = true;
    scroll();
  }

  function startNew() {
    conversationId = null;
    planNode = null;
    busyRow = null;
    thread.replaceChildren(emptyState('New conversation',
      'Ask about users, incidents, jobs, metrics, or anything else this installation knows.'));
    input.focus?.({preventScroll: true});
  }

  // --- composer ------------------------------------------------------------

  function submit() {
    const message = input.value.trim();
    if (!message) return Promise.resolve();
    if (transport.isBusy()) {
      toast('The assistant is still answering.', {tone: 'info'});
      return Promise.resolve();
    }
    return runAction(send, async () => {
      if (!transport.isReady()) throw new Error('The Assistant connection is not ready yet.');
      transport.send(message, conversationId);
      input.value = '';
      appendUser(message);
      setBusy(true);
      announce('Message sent to the assistant.');
    }, {announceLabel: 'Sending…'});
  }

  send.addEventListener('click', () => { submit(); });
  input.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' || event.shiftKey) return;
    event.preventDefault();
    submit();
  });

  // --- mount ---------------------------------------------------------------

  function notConfigured() {
    const owner = capabilities.setup;
    return degradedState(
      'The Assistant is not configured',
      owner ? 'Turn it on, store an Anthropic key, and pick a model in Assistant setup.'
        : 'An account owner has to enable it and store a provider key before it can answer.',
      owner ? h('button', {class: 'button primary compact', type: 'button',
        onclick: () => panel.showSetup?.()}, 'Open Assistant setup') : null);
  }

  body.replaceChildren(history, thread, composer);
  panel.setControls?.({
    onNew: () => { startNew(); return Promise.resolve(); },
    onHistory: () => {
      const opening = history.hidden;
      history.hidden = !opening;
      if (!opening) return Promise.resolve();
      return loadInto(history, () => loadHistoryList(), {message: 'Loading conversations'});
    },
  });

  if (capabilities.ready === false) {
    composer.hidden = true;
    thread.replaceChildren(notConfigured());
  } else {
    thread.replaceChildren(loadingState('Loading Assistant'));
    transport.start();
    startNew();
    loadInto(history, () => loadHistoryList(), {message: 'Loading conversations'})
      .catch(() => { /* loadInto already paints its own failure */ });
  }

  return {
    dispose() {
      disposed = true;
      transport.dispose();
    },
    close() {
      // Closing the panel mid-turn keeps the socket until that turn resolves:
      // the server will publish exactly one terminal event and the thread has
      // to be able to receive it.
      closeWhenIdle = true;
      transport.stop();
    },
  };
}
