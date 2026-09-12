/* Hosted recovery only. The public MojoBouncer SDK keeps its own contract. */
(function (root) {
  'use strict';
  var ACTIONS = ['check', 'slider', 'check_cookie', 'allow', 'token', 'cooldown', 'decoy', 'recovery', 'error'];

  function requestId() {
    var bytes = new Uint8Array(16);
    root.crypto.getRandomValues(bytes);
    return Array.from(bytes, function (b) { return b.toString(16).padStart(2, '0'); }).join('');
  }

  function message(data) {
    var messages = {
      cookies: 'This check needs site cookies. Allow cookies for this site, then reload the page.',
      expired: 'This check expired. Reload the page to start a new check.',
      restart: 'This check has changed. Reload the page to continue.',
      operator: 'We couldn’t verify this visit. Please contact the site operator for help.',
      unavailable: 'Verification is temporarily unavailable. Please try again shortly.',
      invalid: 'We couldn’t read that response. Please try again.'
    };
    return messages[data.reason] || 'We couldn’t verify this visit. Please contact the site operator for help.';
  }

  function Client(config) { this.config = config; }
  Client.prototype.send = function (operation, fields, signals) {
    var controller = new AbortController();
    var timeout = setTimeout(function () { controller.abort(); }, 8000);
    var body = {hosted_gate: Object.assign({version: 1, descriptor: this.config.descriptor,
      operation: operation, request_id: requestId()}, fields || {}), signals: signals || {}};
    return fetch('/api/account/bouncer/assess', {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body), signal: controller.signal})
      .then(function (response) {
        if (!(response.headers.get('Content-Type') || '').includes('application/json')) throw new Error('Invalid response');
        return response.json().then(function (body) {
          var data = body && body.data;
          if (!data || ACTIONS.indexOf(data.next_action) < 0) throw new Error('Invalid response');
          if ((!response.ok || body.status !== true) && ['error', 'recovery'].indexOf(data.next_action) < 0) throw new Error('Rejected response');
          if (['allow', 'token', 'check_cookie'].indexOf(data.next_action) >= 0 && data.decision !== 'allow') throw new Error('Rejected response');
          return data;
        });
      }).finally(function () { clearTimeout(timeout); });
  };

  function tokenProvider(config) {
    var client = new Client(config);
    return function () {
      return client.send('token').then(function (data) {
        if (data.next_action !== 'token' || typeof data.token !== 'string' || !data.token) throw new Error(message(data));
        return data.token;
      });
    };
  }

  function mountDecoy(doc) {
    var fields = doc.getElementById('mbg-decoy-fields');
    if (!fields || fields.dataset.ready) return;
    fields.dataset.ready = '1';
    fields.disabled = false;
    doc.getElementById('mbg-signin').addEventListener('click', function () {
      doc.getElementById('mbg-password').value = '';
      doc.getElementById('mbg-decoy-message').textContent = 'Invalid username or password.';
    });
  }

  function mount(doc, config) {
    var client = new Client(config), action = config.next_action, busy = false, navigated = false;
    var button = doc.getElementById('mbg-continue'), status = doc.getElementById('mbg-status');
    var slider = doc.getElementById('mbg-slider'), panel = doc.getElementById('mbg-slider-panel');
    var pending = null, countdown = null, pointerMoved = false, resetFallback = false;
    var behavior = {mouse_move_count: 0, touch_event_count: 0, keystroke_count: 0};
    var token = new URLSearchParams(root.location.search).get('token');
    if (token && token.indexOf('pr:') === 0) {
      try { root.sessionStorage.setItem('mat_reset_token', token); }
      catch (_) { resetFallback = true; }
    }
    doc.addEventListener('pointermove', function (event) {
      behavior[event.pointerType === 'touch' ? 'touch_event_count' : 'mouse_move_count'] += 1;
    }, {passive: true});
    doc.addEventListener('keydown', function () { behavior.keystroke_count += 1; });

    function show(data) {
      action = data.next_action;
      if (countdown) { clearInterval(countdown); countdown = null; }
      panel.hidden = action !== 'slider';
      button.hidden = false;
      button.disabled = busy;
      slider.disabled = busy || action !== 'slider';
      if (action === 'slider') {
        if (!Number.isFinite(data.target) || !Number.isFinite(data.tolerance)) throw new Error('Invalid target');
        var low = Math.max(0, data.target - data.tolerance), high = Math.min(100, data.target + data.tolerance);
        doc.getElementById('mbg-heading').textContent = 'A quick check';
        doc.getElementById('mbg-target-label').textContent = 'Move between ' + low + ' and ' + high + ', then release.';
        doc.getElementById('mbg-target').style.left = low + '%';
        doc.getElementById('mbg-target').style.width = (high - low) + '%';
        status.textContent = data.attempts_remaining < 3 ? 'Try the highlighted area. ' + data.attempts_remaining + ' attempts left.' : 'Move the slider into the highlighted area.';
        button.textContent = 'Confirm';
      } else if (action === 'check') {
        button.textContent = 'Continue';
        status.textContent = 'Select Continue to proceed securely.';
      } else if (action === 'cooldown') {
        var until = Date.now() + Math.max(1, Math.min(60, data.retry_after || 60)) * 1000;
        button.textContent = 'Retry'; button.disabled = true;
        function tick() {
          var left = Math.max(0, Math.ceil((until - Date.now()) / 1000));
          status.textContent = left ? 'Take a moment. You can try again in ' + left + ' seconds.' : 'You can try the check again now.';
          if (!left) { clearInterval(countdown); countdown = null; button.disabled = false; }
        }
        tick(); countdown = setInterval(tick, 1000);
      } else if (action === 'decoy') {
        doc.getElementById('mbg-check').hidden = true;
        doc.getElementById('mbg-decoy').hidden = false;
        mountDecoy(doc);
      } else {
        status.textContent = message(data);
        button.textContent = 'Retry';
        button.hidden = action === 'recovery';
        if (data.reason === 'cookies' && root.top !== root.self) doc.getElementById('mbg-top-level').hidden = false;
      }
    }

    function navigate() {
      if (navigated) return;
      navigated = true;
      status.textContent = 'Verified. Continuing…';
      if (resetFallback) root.location.reload();
      else root.location.replace(config.redirect_url);
    }

    function submit(operation) {
      if (busy || navigated) return;
      busy = true; button.disabled = slider.disabled = true;
      if (!pending) pending = {operation: operation, fields: {request_id: requestId(), answer: Number(slider.value)}};
      status.textContent = 'Checking…';
      var signals = {behavior: behavior, gate_challenge: {honeypot_filled: !!doc.getElementById('mbg-hp-field').value}};
      client.send(pending.operation, pending.fields, signals).then(function (data) {
        pending = null;
        if (data.next_action !== 'check_cookie') return data;
        return client.send('confirm').then(function (confirmed) {
          if (confirmed.next_action === 'allow') navigate();
          return confirmed;
        });
      }).then(function (data) {
        busy = false;
        if (!navigated) show(data);
      }).catch(function () {
        busy = false;
        action = 'error'; panel.hidden = true;
        status.textContent = 'The connection was interrupted. Please try again.';
        button.textContent = 'Retry'; button.hidden = false; button.disabled = false;
      });
    }
    button.addEventListener('click', function () { submit(action === 'slider' ? 'submit' : 'check'); });
    slider.addEventListener('input', function () {
      pointerMoved = true;
      doc.getElementById('mbg-value').textContent = 'Position: ' + slider.value;
    });
    slider.addEventListener('pointerdown', function () { pointerMoved = false; });
    slider.addEventListener('pointerup', function () { if (pointerMoved) submit('submit'); });
    try { show(config); }
    catch (_) { show({next_action: 'error', reason: 'invalid'}); }
  }

  root.MojoHostedBouncer = {Client: Client, mount: mount, tokenProvider: tokenProvider, mountDecoy: mountDecoy};
}(typeof window !== 'undefined' ? window : globalThis));
