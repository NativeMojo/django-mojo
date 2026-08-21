// The panel's one WebSocket owner. No DOM, no rendering, no authority.
//
// Four rules shape this file:
//
//  * ONE correlation owner. send_event_to_user fans an event out to EVERY
//    socket the user holds, so a second Admin tab's turn arrives here too.
//    Every inbound assistant_* event whose request_id this transport did not
//    mint is dropped.
//  * ONE terminal outcome per turn: assistant_response or assistant_error.
//    Nothing else resolves a turn.
//  * A keep-alive is mandatory, not an optimisation. The server closes an
//    authenticated socket after 30s of CLIENT silence (AUTH_IDLE_TIMEOUT_SECONDS
//    in mojo/apps/realtime/handler.py; last_activity is stamped on inbound
//    messages only, so server->client events do not count). Every turn longer
//    than 30 seconds would otherwise lose its socket mid-answer.
//  * No stop control. The server exposes no way to abort a turn and the agent
//    runs on a detached daemon thread, so no button here may claim otherwise:
//    the composer is re-enabled by a terminal outcome and by nothing else.

const PING_MS = 12000;
const MISSED_PONG_LIMIT = 2;
// The ceiling on a silent-but-alive turn. Every event of any kind resets it;
// the keep-alive above is what actually detects a dead socket.
const TURN_WATCHDOG_MS = 240000;
const BACKOFF_MS = [1000, 2000, 4000, 8000, 16000, 30000];
const RATE_LIMITED_CODE = 4429;
const MAX_FAILURES = 8;
const OWNED_IDS = 24;

function socketUrl() {
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${scheme}//${location.host}/ws/realtime/`;
}

export function createTransport({onEvent, onStatus = () => {}} = {}) {
  let socket = null;
  let authenticated = false;
  let running = false;
  let failures = 0;
  let reconnectTimer = null;
  let pingTimer = null;
  let missedPongs = 0;
  let watchdog = null;
  let pendingTurn = null;
  let terminal = null;
  const owned = [];
  const approvals = new Map();

  const status = (state, detail = '') => onStatus({state, detail});

  function mint() {
    const id = crypto.randomUUID();
    owned.push(id);
    if (owned.length > OWNED_IDS) owned.shift();
    return id;
  }

  function isOwned(id) { return typeof id === 'string' && owned.includes(id); }

  function clearWatchdog() {
    if (watchdog) { clearTimeout(watchdog); watchdog = null; }
  }

  function armWatchdog() {
    clearWatchdog();
    if (!pendingTurn) return;
    watchdog = setTimeout(() => {
      if (!pendingTurn) return;
      const turn = pendingTurn;
      pendingTurn = null;
      clearWatchdog();
      // Not "it failed" -- the server may well still be working. The panel
      // offers a reload rather than inventing an outcome.
      onEvent?.({type: 'assistant_error', request_id: turn.requestId,
        conversation_id: turn.conversationId, timeout: true,
        error: 'The assistant has not answered for four minutes. It may still be '
          + 'running -- reload the conversation to see where it got to.'});
    }, TURN_WATCHDOG_MS);
  }

  function stopPing() {
    if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
    missedPongs = 0;
  }

  function startPing() {
    stopPing();
    pingTimer = setInterval(() => {
      if (!socket || socket.readyState !== WebSocket.OPEN) return;
      if (missedPongs >= MISSED_PONG_LIMIT) { dropSocket(); return; }
      missedPongs += 1;
      try { socket.send(JSON.stringify({action: 'ping'})); } catch (_) { dropSocket(); }
    }, PING_MS);
  }

  function dropSocket() {
    stopPing();
    authenticated = false;
    if (socket) {
      socket.onclose = null;
      socket.onmessage = null;
      socket.onerror = null;
      socket.onopen = null;
      try { socket.close(); } catch (_) { /* already gone */ }
      socket = null;
    }
    scheduleReconnect();
  }

  function scheduleReconnect(code = 0) {
    if (!running || terminal) return;
    if (reconnectTimer) return;
    if (failures >= MAX_FAILURES) {
      status('failed', 'The Assistant connection could not be re-established.');
      return;
    }
    // 4429 is a deliberate pre-accept rejection, not a network blip: start at
    // the top of the ladder rather than hammering the gate.
    const index = code === RATE_LIMITED_CODE ? BACKOFF_MS.length - 1
      : Math.min(failures, BACKOFF_MS.length - 1);
    failures += 1;
    status('offline', 'Reconnecting…');
    reconnectTimer = setTimeout(() => { reconnectTimer = null; connect(); }, BACKOFF_MS[index]);
  }

  function handleAssistantEvent(payload) {
    const requestId = payload.request_id;
    const approval = approvals.get(requestId);
    if (approval) {
      if (payload.type === 'assistant_approval_ack') return;
      approvals.delete(requestId);
      approval(payload);
      return;
    }
    if (!isOwned(requestId)) return;
    if (pendingTurn && pendingTurn.requestId === requestId) {
      if (payload.type === 'assistant_thinking' && payload.conversation_id != null) {
        pendingTurn.conversationId = payload.conversation_id;
      }
      if (payload.type === 'assistant_response' || payload.type === 'assistant_error') {
        pendingTurn = null;
        clearWatchdog();
      } else {
        armWatchdog();
      }
    }
    onEvent?.(payload);
  }

  function connect() {
    if (!running || socket) return;
    status('connecting', 'Connecting…');
    let opened;
    try {
      opened = new WebSocket(socketUrl());
    } catch (_) {
      scheduleReconnect();
      return;
    }
    socket = opened;
    socket.onmessage = (message) => {
      let payload;
      try { payload = JSON.parse(message.data); } catch (_) { return; }
      if (!payload || typeof payload !== 'object') return;
      if (payload.type === 'auth_required') {
        const token = window.MojoAuth?.getToken?.();
        if (!token) { terminate('Assistant access is no longer available.'); return; }
        socket.send(JSON.stringify({type: 'authenticate', token, prefix: 'bearer'}));
        return;
      }
      if (payload.type === 'auth_success') {
        authenticated = true;
        failures = 0;
        missedPongs = 0;
        startPing();
        status('online', '');
        onEvent?.({type: 'assistant_socket_ready'});
        return;
      }
      if (payload.type === 'pong') { missedPongs = 0; return; }
      if (payload.type === 'error' && !authenticated) {
        // An authentication failure is terminal. Retrying with the same token
        // would loop forever against a boundary that already answered.
        terminate('Assistant access is no longer available.');
        return;
      }
      if (typeof payload.type === 'string' && payload.type.startsWith('assistant_')) {
        handleAssistantEvent(payload);
      }
    };
    socket.onclose = (event) => {
      stopPing();
      authenticated = false;
      socket = null;
      if (!running || terminal) return;
      scheduleReconnect(event?.code);
    };
    socket.onerror = () => { /* onclose always follows; nothing to add here */ };
  }

  function terminate(detail) {
    terminal = detail;
    running = false;
    stopPing();
    clearWatchdog();
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    if (socket) {
      socket.onclose = null;
      try { socket.close(); } catch (_) { /* already gone */ }
      socket = null;
    }
    status('terminal', detail);
  }

  return {
    start() {
      if (terminal) return;
      running = true;
      failures = 0;
      connect();
    },
    stop() {
      // A turn in flight keeps the socket: closing here would throw away the
      // one terminal event the panel is waiting for.
      if (pendingTurn) return false;
      running = false;
      stopPing();
      clearWatchdog();
      if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
      if (socket) {
        socket.onclose = null;
        try { socket.close(); } catch (_) { /* already gone */ }
        socket = null;
      }
      authenticated = false;
      return true;
    },
    isReady() { return Boolean(authenticated && socket && socket.readyState === WebSocket.OPEN); },
    isBusy() { return Boolean(pendingTurn); },
    terminalReason() { return terminal; },
    send(message, conversationId) {
      if (pendingTurn) throw new Error('The assistant is still answering.');
      if (!this.isReady()) throw new Error('The Assistant connection is not ready.');
      const requestId = mint();
      pendingTurn = {requestId, conversationId: conversationId ?? null, startedAt: Date.now()};
      socket.send(JSON.stringify({
        type: 'assistant_message', message, request_id: requestId,
        ...(conversationId ? {conversation_id: conversationId} : {}),
      }));
      armWatchdog();
      return requestId;
    },
    quickReply(value, conversationId, actionId) {
      // The legacy `action` quick-reply, byte-identical to before: its value is
      // replayed as an ordinary message and it carries no authority.
      if (pendingTurn) throw new Error('The assistant is still answering.');
      if (!this.isReady()) throw new Error('The Assistant connection is not ready.');
      const requestId = mint();
      pendingTurn = {requestId, conversationId: conversationId ?? null, startedAt: Date.now()};
      socket.send(JSON.stringify({
        type: 'assistant_action', value, action_id: actionId, request_id: requestId,
        ...(conversationId ? {conversation_id: conversationId} : {}),
      }));
      armWatchdog();
      return requestId;
    },
    resolveApproval({conversationId, actionId, decision}) {
      if (!this.isReady()) throw new Error('The Assistant connection is not ready.');
      const requestId = mint();
      return new Promise((resolve) => {
        approvals.set(requestId, resolve);
        socket.send(JSON.stringify({
          type: 'assistant_approval', conversation_id: conversationId ?? null,
          action_id: actionId, decision, request_id: requestId,
        }));
      });
    },
    dispose() {
      // Unconditional, unlike stop(): the panel is going away, so a turn still
      // in flight has nowhere to be delivered.
      pendingTurn = null;
      terminate(terminal || 'The Assistant panel was closed.');
      approvals.clear();
    },
  };
}
