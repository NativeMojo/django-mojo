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

  // Challenge page copy (bouncer_challenge.html). Every write is textContent.
  // Continue is a check, never proof of being human: no line here says so.
  var COPY = {
    ready: ['One quick check', 'Select Continue to proceed securely.'],
    checking: ['Checking…', 'This only takes a moment.'],
    verified: ['Verified', ''],
    cookies: ['We couldn’t confirm this visit', 'Your browser didn’t keep this site’s security cookie. This can happen in private browsing or inside another app’s browser.'],
    limited: ['Too many tries', 'Please wait a moment before trying again.'],
    expired: ['This check expired', 'It took too long to finish. Try again to start a new check.'],
    restart: ['This check changed', 'Try again to start a new check.'],
    unavailable: ['Checks are briefly unavailable', 'Something on our side isn’t responding. Please try again shortly.'],
    operator: ['We couldn’t confirm this visit', 'Ask for a review, or contact the site operator for help.'],
    invalid: ['Something went wrong', 'We couldn’t read the response. Start over to begin a new check.'],
    network: ['The connection was interrupted', 'Check your connection and try again.'],
    stopped: ['We couldn’t complete this check', 'Try again to start a new check, or ask for a review.'],
    sent: ['Request sent', '']
  };
  var REASONS = ['cookies', 'limited', 'expired', 'restart', 'unavailable', 'operator', 'invalid'];
  var VERIFIED = {login: 'Continuing to sign in…', registration: 'Continuing to create your account…',
    public_message: 'Continuing…'};

  function fmt(seconds) {
    var s = Math.max(0, Math.ceil(seconds));
    return Math.floor(s / 60) + ':' + ('0' + (s % 60)).slice(-2);
  }

  function mount(doc, config) {
    var client = new Client(config), busy = false, navigated = false;
    var button = doc.getElementById('mbg-continue'), status = doc.getElementById('mbg-status');
    var pending = null, resetFallback = false, failures = 0, retryAt = 0, modality = 'unknown', reportedFailure = '';
    var view = doc.getElementById('mbg-root'), restart = doc.getElementById('mbg-restart');
    var heading = doc.getElementById('mbg-heading'), buttonLabel = doc.getElementById('mbg-continue-label');
    var reviewOpen = doc.getElementById('mbg-review-open'), help = doc.getElementById('mbg-help');
    var reference = doc.getElementById('mbg-reference'), referenceRow = doc.getElementById('mbg-reference-row');
    var form = doc.getElementById('mbg-review-form'), ticket = doc.getElementById('mbg-review-ticket');
    var countdown = null, previous = 'ready', opener = null;
    var behavior = {mouse_move_count: 0, touch_event_count: 0, keystroke_count: 0, activation_count: 0};
    var token = new URLSearchParams(root.location.search).get('token');
    if (token && (token.indexOf('pr:') === 0 || token.indexOf('iv:') === 0)) {
      try { root.sessionStorage.setItem('mat_reset_token', token); }
      catch (_) { resetFallback = true; }
    }
    // Marks the page as script-driven: the no-script :target review reveal stops applying.
    if (view) view.setAttribute('data-js', '1');
    doc.addEventListener('pointermove', function (event) {
      modality = event.pointerType || 'unknown';
      behavior[event.pointerType === 'touch' ? 'touch_event_count' : 'mouse_move_count'] += 1;
    }, {passive: true});
    doc.addEventListener('pointerdown', function (event) {
      modality = event.pointerType || 'unknown';
      if (modality === 'touch') behavior.touch_event_count += 1;
    }, {passive: true});
    doc.addEventListener('keydown', function () { modality = 'keyboard'; behavior.keystroke_count += 1; });

    function setState(name) { if (view) view.setAttribute('data-state', name); }
    function currentState() { return view ? view.getAttribute('data-state') : ''; }
    function label(text) { if (buttonLabel) buttonLabel.textContent = text; else button.textContent = text; }
    function setCopy(key, sentence) {
      var copy = COPY[key] || COPY.operator;
      if (heading) heading.textContent = copy[0];
      status.textContent = sentence === undefined ? copy[1] : sentence;
    }
    function stopCountdown() { if (countdown) { clearInterval(countdown); countdown = null; } }
    function tick() {
      var left = (retryAt - Date.now()) / 1000;
      if (left <= 0) { stopCountdown(); label('Try again'); button.disabled = busy; return; }
      label('Try again in ' + fmt(left));
    }
    // A submit outcome can hide or disable the control that had focus; move
    // focus to the heading so the outcome is read and keyboard users keep their place.
    function refocus() {
      var active = doc.activeElement;
      if (!active || active === doc.body || active.hidden || active.disabled || !active.offsetParent) {
        if (heading) heading.focus();
      }
    }
    // Restart reloads the page. It stays reachable in every failed state: a quiet
    // "Start over" next to Try again, or the primary "Try again" when Try again is hidden.
    function placeRestart() {
      if (!restart) return;
      var promote = button.hidden;
      restart.hidden = false;
      restart.textContent = promote ? 'Try again' : 'Start over';
      restart.classList.toggle('is-primary', promote);
      var anchor = promote ? button : reviewOpen;
      if (anchor && anchor.parentNode === restart.parentNode && anchor.nextSibling !== restart) {
        restart.parentNode.insertBefore(restart, anchor.nextSibling);
      }
    }

    function recovery(data) {
      reportedFailure = data.reason || 'network';
      if (ticket && data.review_ticket) ticket.value = data.review_ticket;
      if (form) form.hidden = !ticket.value;
      if (reference && data.reference) reference.textContent = data.reference;
      if (referenceRow) referenceRow.hidden = !(reference && reference.textContent);
    }

    function show(data) {
      // An in-flight page may contain the retired slider/cooldown config.
      var action = data.next_action;
      if (action === 'slider' || action === 'cooldown') action = 'check';
      stopCountdown();
      button.hidden = false;
      button.disabled = busy;
      if (action === 'check') {
        label('Continue');
        setCopy('ready');
        setState('ready');
      } else {
        if (action === 'decoy') data = Object.assign({}, data, {reason: 'operator'});
        setCopy(failures >= 3 ? 'stopped' : (REASONS.indexOf(data.reason) >= 0 ? data.reason : 'operator'));
        setState('failed');
        label('Try again');
        button.hidden = action === 'recovery' || action === 'decoy' || failures >= 3;
        if (data.reason === 'limited') {
          retryAt = Date.now() + (Number(data.retry_after) || 60) * 1000;
          button.disabled = true;
          tick();
          countdown = setInterval(tick, 1000);
        }
        placeRestart();
        recovery(data);
        if (data.reason === 'cookies' && root.top !== root.self) doc.getElementById('mbg-top-level').hidden = false;
      }
    }

    function navigate() {
      if (navigated) return;
      navigated = true;
      setState('verified');
      setCopy('verified', VERIFIED[config.page_type] || VERIFIED.login);
      if (resetFallback) root.location.reload();
      else root.location.replace(config.redirect_url);
    }

    function submit() {
      if (busy || navigated || Date.now() < retryAt || failures >= 3) return;
      behavior.activation_count += 1;
      busy = true; button.disabled = true;
      if (!pending) pending = {request_id: requestId()};
      setState('checking');
      label('Checking');
      setCopy('checking');
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
        if (!navigated) { show(data); refocus(); }
      }).catch(function () {
        busy = false;
        failures += 1;
        setCopy(failures >= 3 ? 'stopped' : 'network');
        setState('failed');
        label('Try again'); button.hidden = failures >= 3; button.disabled = false;
        placeRestart();
        recovery(config);
        refocus();
      });
    }

    function openReview(from) {
      if (currentState() !== 'review') previous = currentState() || 'ready';
      opener = from;
      setState('review');
      var target = form && !form.hidden ? doc.getElementById('mbg-review-email') : doc.getElementById('mbg-review-title');
      if (target) target.focus();
    }

    button.addEventListener('click', submit);
    if (restart) restart.addEventListener('click', function () { root.location.reload(); });
    var sentRestart = doc.getElementById('mbg-sent-restart');
    if (sentRestart) sentRestart.addEventListener('click', function () { root.location.reload(); });
    if (help) help.addEventListener('click', function (event) { event.preventDefault(); openReview(help); });
    if (reviewOpen) reviewOpen.addEventListener('click', function () { openReview(reviewOpen); });
    var back = doc.getElementById('mbg-review-back');
    if (back) back.addEventListener('click', function () {
      setState(previous);
      if (opener && !opener.hidden) opener.focus();
      else if (heading) heading.focus();
    });
    var copy = doc.getElementById('mbg-copy'), copyStatus = doc.getElementById('mbg-copy-status');
    if (copy && reference) copy.addEventListener('click', function () {
      function selectIt() {
        try { root.getSelection().selectAllChildren(reference); } catch (_) {}
        if (copyStatus) copyStatus.textContent = 'Reference selected. Use your device’s copy command.';
      }
      var clipboard = root.navigator && root.navigator.clipboard;
      if (!clipboard || typeof clipboard.writeText !== 'function') return selectIt();
      try {
        clipboard.writeText(reference.textContent).then(function () {
          if (copyStatus) copyStatus.textContent = 'Reference copied.';
        }, selectIt);
      } catch (_) { selectIt(); }
    });
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
            // The receipt is the server's own message: recorded for review, no access granted.
            reviewStatus.textContent = body.data.message + ' Reference: ' + body.data.reference;
            if (reference && body.data.reference) reference.textContent = body.data.reference;
            if (referenceRow) referenceRow.hidden = !(reference && reference.textContent);
            stopCountdown();
            setCopy('sent', body.data.message);
            setState('sent');
            if (heading) {
              heading.setAttribute('aria-describedby', 'mbg-status mbg-reference-row');
              heading.focus();
            }
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
