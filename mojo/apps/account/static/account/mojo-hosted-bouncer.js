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
      cookies: 'Your browser didn’t return this site’s verification cookies. Restart the check, or request help below.',
      expired: 'This check expired. Reload the page to start a new check.',
      restart: 'This check has changed. Reload the page to continue.',
      operator: 'We couldn’t verify this visit. Please contact the site operator for help.',
      unavailable: 'Verification is temporarily unavailable. Please try again shortly.',
      limited: 'Too many checks. Please wait before trying again, or request help below.',
      invalid: 'We couldn’t read that response. Please try again.'
    };
    var text = messages[data.reason] || 'We couldn’t verify this visit. Please contact the site operator for help.';
    return text + (data.reference ? ' Reference: ' + data.reference : '');
  }

  function Client(config) { this.config = config; }
  Client.prototype.send = function (operation, fields, signals, context) {
    var controller = new AbortController();
    var timeout = setTimeout(function () { controller.abort(); }, 8000);
    var body = {hosted_gate: Object.assign({version: 1, descriptor: this.config.descriptor,
      operation: operation, request_id: requestId()}, fields || {}), signals: signals || {}};
    if (context && context.duid) body.duid = context.duid;
    return fetch('/api/account/bouncer/assess', {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body), signal: controller.signal})
      .then(function (response) {
        if (response.status === 429) return {next_action: 'error', reason: 'limited',
          retry_after: Math.max(1, Math.min(3600, Number(response.headers.get('Retry-After')) || 60))};
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
    return function (purpose, context) {
      return client.send('token', null, null, context).then(function (data) {
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
    var client = new Client(config), busy = false, navigated = false;
    var button = doc.getElementById('mbg-continue'), status = doc.getElementById('mbg-status');
    var pending = null, resetFallback = false, failures = 0, retryAt = 0, modality = 'unknown', reportedFailure = '';
    var help = doc.getElementById('mbg-help'), restart = doc.getElementById('mbg-restart');
    var form = doc.getElementById('mbg-review-form'), ticket = doc.getElementById('mbg-review-ticket');
    var behavior = {mouse_move_count: 0, touch_event_count: 0, keystroke_count: 0, activation_count: 0};
    var token = new URLSearchParams(root.location.search).get('token');
    if (token && token.indexOf('pr:') === 0) {
      try { root.sessionStorage.setItem('mat_reset_token', token); }
      catch (_) { resetFallback = true; }
    }
    doc.addEventListener('pointermove', function (event) {
      modality = event.pointerType || 'unknown';
      behavior[event.pointerType === 'touch' ? 'touch_event_count' : 'mouse_move_count'] += 1;
    }, {passive: true});
    doc.addEventListener('pointerdown', function (event) {
      modality = event.pointerType || 'unknown';
      if (modality === 'touch') behavior.touch_event_count += 1;
    }, {passive: true});
    doc.addEventListener('keydown', function () { modality = 'keyboard'; behavior.keystroke_count += 1; });

    function recovery(data) {
      reportedFailure = data.reason || 'network';
      if (help) help.open = true;
      if (ticket && data.review_ticket) ticket.value = data.review_ticket;
      if (form) form.hidden = !ticket.value;
      var reference = doc.getElementById('mbg-reference');
      if (reference && data.reference) reference.textContent = 'Reference: ' + data.reference;
    }

    function show(data) {
      // An in-flight page may contain the retired slider/cooldown config.
      var action = data.next_action;
      if (action === 'slider' || action === 'cooldown') action = 'check';
      button.hidden = false;
      button.disabled = busy;
      if (action === 'check') {
        button.textContent = 'Continue';
        status.textContent = 'Select Continue to proceed securely.';
      } else {
        if (action === 'decoy') data = Object.assign({}, data, {reason: 'operator'});
        var heading = doc.getElementById('mbg-heading');
        if (heading) heading.textContent = 'Let’s get you help';
        status.textContent = message(data);
        button.textContent = 'Retry';
        button.hidden = action === 'recovery' || action === 'decoy' || failures >= 3;
        if (data.reason === 'limited') {
          retryAt = Date.now() + data.retry_after * 1000;
          button.disabled = true;
          status.textContent += ' Wait ' + data.retry_after + ' seconds.';
          setTimeout(function () { if (!busy) button.disabled = false; }, data.retry_after * 1000);
        }
        if (restart) restart.hidden = false;
        recovery(data);
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

    function submit() {
      if (busy || navigated || Date.now() < retryAt || failures >= 3) return;
      behavior.activation_count += 1;
      busy = true; button.disabled = true;
      if (!pending) pending = {request_id: requestId()};
      status.textContent = 'Checking…';
      var signals = {behavior: behavior, gate_challenge: {
        honeypot_filled: !!doc.getElementById('mbg-hp-field').value,
        is_touch_device: modality === 'touch', had_activation: true,
        had_mouse_movement: behavior.mouse_move_count > 0, had_touch_events: behavior.touch_event_count > 0,
        input_modality: modality}};
      client.send('check', pending, signals).then(function (data) {
        pending = null;
        if (data.next_action !== 'check_cookie') return data;
        return client.send('confirm').then(function (confirmed) {
          if (confirmed.next_action === 'allow') navigate();
          return confirmed;
        });
      }).then(function (data) {
        busy = false;
        if (data.next_action === 'error') failures += 1;
        if (!navigated) show(data);
      }).catch(function () {
        busy = false;
        failures += 1;
        status.textContent = 'The connection was interrupted. Please try again.';
        button.textContent = 'Retry'; button.hidden = failures >= 3; button.disabled = false;
        if (failures >= 3) status.textContent = 'We couldn’t complete this check. You can restart or request help below.';
        if (restart) restart.hidden = false;
        recovery(config);
      });
    }
    button.addEventListener('click', submit);
    if (restart) restart.addEventListener('click', function () { root.location.reload(); });
    if (form) form.addEventListener('submit', function (event) {
      event.preventDefault();
      var submitButton = doc.getElementById('mbg-review-submit');
      var reviewStatus = doc.getElementById('mbg-review-status');
      if (submitButton.disabled) return;
      submitButton.disabled = true;
      reviewStatus.textContent = 'Sending your request…';
      var controller = new AbortController();
      var timeout = setTimeout(function () { controller.abort(); }, 10000);
      fetch(form.action, {method: 'POST', credentials: 'same-origin', signal: controller.signal,
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify({review_ticket: ticket.value,
          email: doc.getElementById('mbg-review-email').value, note: doc.getElementById('mbg-review-note').value,
          reported_failure: reportedFailure})})
        .then(function (response) {
          if (response.status === 429) throw new Error('Please wait a few minutes before sending another request.');
          return response.json().then(function (body) {
            if (!response.ok || body.status !== true) throw new Error(body.data && body.data.message || 'Your request could not be recorded. Please try again.');
            reviewStatus.textContent = body.data.message + ' Reference: ' + body.data.reference;
            reviewStatus.scrollIntoView({block: 'nearest'});
          });
        }).catch(function (error) {
          reviewStatus.textContent = error.name === 'AbortError' ? 'We couldn’t confirm receipt. You can safely try this request again.' : error.message;
          submitButton.disabled = false;
        }).finally(function () { clearTimeout(timeout); });
    });
    try { show(config); }
    catch (_) { show({next_action: 'error', reason: 'invalid'}); }
  }

  root.MojoHostedBouncer = {Client: Client, mount: mount, tokenProvider: tokenProvider, mountDecoy: mountDecoy};
}(typeof window !== 'undefined' ? window : globalThis));
