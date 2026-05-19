/**
 * mojo-bouncer.js — embeddable bot-detection gate for arbitrary pages.
 *
 * Loaded on any page (same-origin or cross-origin) that needs a one-shot
 * bot screen before a sensitive action. Collects environment + behavior
 * signals, runs an interactive challenge, calls /api/account/bouncer/assess
 * on the configured host, and issues a bouncer_token the page can attach to
 * its next sensitive request.
 *
 * v2.0.0 changes vs the legacy mojoverify version:
 *   - Endpoints point at the django-mojo bouncer paths (/api/account/bouncer/*).
 *   - The two-stage submit handshake is gone — the assess token + the
 *     @md.requires_bouncer_token decorator on the server are the validation.
 *   - The `Authorization: apikey ...` header is dropped — django-mojo bouncer
 *     endpoints are @md.public_endpoint, rate-limited, no apikey concept.
 *   - All fetch calls include credentials: 'include' so the HttpOnly mbp
 *     pass cookie is set cross-origin.
 *   - `data-api-base` is the supported config for cross-origin embedding;
 *     `data-api-key` is no longer recognized.
 *
 * Public API (unchanged from v1 where applicable):
 *   bouncer.reportEvent(category, level, title, details, metadata)
 *   bouncer.getToken()
 *   bouncer.getDuid()
 *   bouncer.getSessionId()
 *
 * Rules:
 *   - Never throws (every method try/catch wrapped)
 *   - Fails open (any error allows the user through)
 *   - Token in memory only (never localStorage, never cookie)
 *   - Single global: window.MojoBouncer
 *
 * @version 2.0.0
 */
(function () {
    'use strict';

    // ── DuidManager ─────────────────────────────────────────────
    // Shares the localStorage key `mojo_device_uid` with mojo-auth.js,
    // mojo-sentinel.js, and the bouncer challenge inline JS — one device
    // identity across the whole mojo client surface.
    class DuidManager {
        static STORAGE_KEY = 'mojo_device_uid';
        static COOKIE_NAME = 'mojo_device_uid';

        static load() {
            try {
                var duid = localStorage.getItem(this.STORAGE_KEY);
                if (duid) return duid;
                duid = this._readCookie();
                if (duid) { this._persist(duid); return duid; }
                duid = this._generate();
                this._persist(duid);
                return duid;
            } catch (e) {
                return this._generate();
            }
        }

        static _generate() {
            return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
                var r = Math.random() * 16 | 0;
                return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
            });
        }

        static _persist(duid) {
            try { localStorage.setItem(this.STORAGE_KEY, duid); } catch (e) {}
            try {
                document.cookie = this.COOKIE_NAME + '=' + duid +
                    ';path=/;max-age=31536000;SameSite=Lax';
            } catch (e) {}
        }

        static _readCookie() {
            try {
                var match = document.cookie.match(new RegExp('(?:^|; )' + this.COOKIE_NAME + '=([^;]+)'));
                return match ? match[1] : null;
            } catch (e) { return null; }
        }
    }

    // ── EnvironmentScanner ──────────────────────────────────────
    class EnvironmentScanner {
        static scan() {
            var signals = {};
            try { signals.webdriver_flag = !!navigator.webdriver; } catch (e) { signals.webdriver_flag = false; }
            try { signals.phantom_globals = !!(window.callPhantom || window._phantom); } catch (e) { signals.phantom_globals = false; }
            try { signals.nightmare_global = !!window.__nightmare; } catch (e) { signals.nightmare_global = false; }
            try { signals.selenium_artifacts = !!(window._selenium || window.__webdriverFunc || document.documentElement.getAttribute('webdriver')); } catch (e) { signals.selenium_artifacts = false; }
            try { signals.chrome_runtime_missing = /Chrome/.test(navigator.userAgent) && !window.chrome; } catch (e) { signals.chrome_runtime_missing = false; }
            try { signals.languages_empty = !navigator.languages || navigator.languages.length === 0; } catch (e) { signals.languages_empty = false; }
            try { signals.screen_zero = screen.width === 0 || screen.height === 0; } catch (e) { signals.screen_zero = false; }
            try {
                var c = document.createElement('canvas');
                signals.webgl_missing = !c.getContext('webgl') && !c.getContext('experimental-webgl');
            } catch (e) { signals.webgl_missing = false; }
            try { signals.plugins_zero = navigator.plugins && navigator.plugins.length === 0; } catch (e) { signals.plugins_zero = false; }
            try { signals.mobile_touch_mismatch = /Mobile|Android|iPhone/i.test(navigator.userAgent) && navigator.maxTouchPoints === 0; } catch (e) { signals.mobile_touch_mismatch = false; }
            try { signals.notification_missing = !('Notification' in window); } catch (e) { signals.notification_missing = false; }
            try { signals.eval_modified = eval.toString().length !== 33; } catch (e) { signals.eval_modified = false; }
            try {
                var fn = Function.prototype.toString;
                signals.native_fn_spoofed = fn.call(fn).indexOf('[native code]') === -1;
            } catch (e) { signals.native_fn_spoofed = false; }
            return signals;
        }
    }

    // ── BehaviorWatcher ─────────────────────────────────────────
    class BehaviorWatcher {
        constructor() {
            this._data = {
                mouse_move_count: 0, scroll_event_count: 0,
                keystroke_count: 0, touch_event_count: 0,
                first_interaction_ms: null, rapid_click: false,
                page_hidden_on_load: false, click_times: [],
            };
            this._startTime = Date.now();
            this._bound = {};
        }

        start() {
            try { this._data.page_hidden_on_load = document.visibilityState === 'hidden'; } catch (e) {}
            var self = this;
            this._bind('mousemove', function () { self._data.mouse_move_count++; self._markFirst(); });
            this._bind('scroll', function () { self._data.scroll_event_count++; self._markFirst(); });
            this._bind('keydown', function () { self._data.keystroke_count++; self._markFirst(); });
            this._bind('touchstart', function () { self._data.touch_event_count++; self._markFirst(); });
            this._bind('click', function () {
                var now = Date.now();
                self._data.click_times.push(now);
                if (self._data.click_times.length >= 2) {
                    var last = self._data.click_times[self._data.click_times.length - 2];
                    if (now - last < 50) self._data.rapid_click = true;
                }
                self._markFirst();
            });
        }

        stop() {
            for (var evt in this._bound) {
                try { document.removeEventListener(evt, this._bound[evt]); } catch (e) {}
            }
            this._bound = {};
        }

        getSignals() {
            var d = Object.assign({}, this._data);
            delete d.click_times;
            return d;
        }

        _markFirst() {
            if (this._data.first_interaction_ms === null) {
                this._data.first_interaction_ms = Date.now() - this._startTime;
            }
        }

        _bind(evt, handler) {
            try {
                this._bound[evt] = handler;
                document.addEventListener(evt, handler, { passive: true });
            } catch (e) {}
        }
    }

    // ── MouseAnalyzer ───────────────────────────────────────────
    class MouseAnalyzer {
        constructor() { this._points = []; }

        record(x, y) {
            if (this._points.length < 50) {
                this._points.push({ x: x, y: y, t: Date.now() });
            }
        }

        analyze() {
            var pts = this._points;
            if (pts.length < 3) return { straightness_score: 0, avg_speed: 0, acceleration_variance: 0 };

            var directDist = Math.hypot(pts[pts.length - 1].x - pts[0].x, pts[pts.length - 1].y - pts[0].y);
            var pathDist = 0;
            for (var i = 1; i < pts.length; i++) {
                pathDist += Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y);
            }
            var straightness = pathDist > 0 ? directDist / pathDist : 0;

            var totalTime = (pts[pts.length - 1].t - pts[0].t) / 1000;
            var avgSpeed = totalTime > 0 ? pathDist / totalTime : 0;

            var accels = [];
            for (var j = 2; j < pts.length; j++) {
                var dt1 = (pts[j].t - pts[j - 1].t) / 1000 || 0.001;
                var dt0 = (pts[j - 1].t - pts[j - 2].t) / 1000 || 0.001;
                var v1 = Math.hypot(pts[j].x - pts[j - 1].x, pts[j].y - pts[j - 1].y) / dt1;
                var v0 = Math.hypot(pts[j - 1].x - pts[j - 2].x, pts[j - 1].y - pts[j - 2].y) / dt0;
                accels.push((v1 - v0) / dt1);
            }
            var mean = accels.reduce(function (s, v) { return s + v; }, 0) / (accels.length || 1);
            var variance = accels.reduce(function (s, v) { return s + (v - mean) * (v - mean); }, 0) / (accels.length || 1);

            return {
                straightness_score: Math.round(straightness * 1000) / 1000,
                avg_speed: Math.round(avgSpeed),
                acceleration_variance: Math.round(variance * 1000) / 1000,
            };
        }
    }

    // ── FingerprintCollector ────────────────────────────────────
    class FingerprintCollector {
        static async collect() {
            var components = [];
            try { components.push('ua:' + navigator.userAgent); } catch (e) {}
            try { components.push('lang:' + navigator.language); } catch (e) {}
            try { components.push('screen:' + screen.width + 'x' + screen.height + 'x' + screen.colorDepth); } catch (e) {}
            try { components.push('tz:' + Intl.DateTimeFormat().resolvedOptions().timeZone); } catch (e) {}
            try { components.push('cores:' + navigator.hardwareConcurrency); } catch (e) {}
            try { components.push('mem:' + navigator.deviceMemory); } catch (e) {}
            try { components.push('touch:' + navigator.maxTouchPoints); } catch (e) {}
            try { components.push('plt:' + navigator.platform); } catch (e) {}

            try {
                var c = document.createElement('canvas');
                c.width = 200; c.height = 50;
                var ctx = c.getContext('2d');
                ctx.textBaseline = 'top';
                ctx.font = '14px Arial';
                ctx.fillStyle = '#f60';
                ctx.fillRect(0, 0, 200, 50);
                ctx.fillStyle = '#069';
                ctx.fillText('MojoBouncer', 2, 15);
                components.push('canvas:' + c.toDataURL().slice(-50));
            } catch (e) {}

            try {
                var gl = document.createElement('canvas').getContext('webgl');
                if (gl) {
                    var ext = gl.getExtension('WEBGL_debug_renderer_info');
                    if (ext) components.push('webgl:' + gl.getParameter(ext.UNMASKED_RENDERER_WEBGL));
                }
            } catch (e) {}

            var raw = components.join('|');
            try {
                var buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(raw));
                var arr = Array.from(new Uint8Array(buf));
                return arr.map(function (b) { return b.toString(16).padStart(2, '0'); }).join('');
            } catch (e) {
                var hash = 0;
                for (var i = 0; i < raw.length; i++) {
                    hash = ((hash << 5) - hash) + raw.charCodeAt(i);
                    hash |= 0;
                }
                return 'fallback-' + Math.abs(hash).toString(16);
            }
        }
    }

    // ── ApiClient ───────────────────────────────────────────────
    // credentials: 'include' is required for the HttpOnly mbp cookie to be
    // set by the server cross-origin. Without it, returning visitors would
    // never skip the gate on subsequent loads.
    class ApiClient {
        static async post(url, payload) {
            try {
                var controller = new AbortController();
                var timer = setTimeout(function () { controller.abort(); }, 5000);
                var resp = await fetch(url, {
                    method: 'POST',
                    credentials: 'include',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                    signal: controller.signal,
                });
                clearTimeout(timer);
                return await resp.json();
            } catch (e) {
                return null;
            }
        }
    }

    // ── Overlay styles (kept minimal — mojo-bouncer.css is the canonical) ──
    var _stylesInjected = false;
    var _CSS = [
        '.mbg{position:fixed;top:0;left:0;width:100%;height:100%;z-index:99999;display:flex;flex-direction:column;align-items:center;justify-content:center;',
        'background:linear-gradient(145deg,#0a0e1a 0%,#121830 40%,#1a1040 100%);',
        'opacity:0;transition:opacity .4s ease;pointer-events:all;overflow:hidden}',
        '.mbg--active{opacity:1}',
        '.mbg--exit{opacity:0;pointer-events:none}',
        '.mbg--none{display:none}',
        '.mbg-logo-wrap{position:relative;margin-bottom:28px}',
        '.mbg-logo{width:72px;height:72px;position:relative;z-index:1;filter:drop-shadow(0 0 20px rgba(99,132,255,.3))}',
        '.mbg-ring{position:absolute;top:50%;left:50%;width:96px;height:96px;margin:-48px 0 0 -48px;border-radius:50%;',
        'border:1.5px solid rgba(99,132,255,.25);animation:mbg-pulse 2s ease-in-out infinite}',
        '.mbg-ring2{animation-delay:.7s;width:116px;height:116px;margin:-58px 0 0 -58px;border-color:rgba(99,132,255,.12)}',
        '@keyframes mbg-pulse{0%,100%{transform:scale(1);opacity:1}50%{transform:scale(1.08);opacity:.4}}',
        '.mbg-wordmark{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;font-size:13px;',
        'font-weight:600;letter-spacing:3.5px;text-transform:uppercase;color:rgba(255,255,255,.7);margin-bottom:32px}',
        '.mbg-progress{width:180px;height:2px;background:rgba(255,255,255,.08);border-radius:1px;overflow:hidden;margin-bottom:16px}',
        '.mbg-bar{height:100%;width:0%;background:linear-gradient(90deg,#6384ff,#a78bfa);border-radius:1px;transition:width .15s linear}',
        '.mbg-status{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;font-size:11px;',
        'color:rgba(255,255,255,.35);letter-spacing:.5px}',
        '.mbg-scan{position:absolute;top:0;left:0;width:100%;height:1px;',
        'background:linear-gradient(90deg,transparent 0%,rgba(99,132,255,.08) 40%,rgba(99,132,255,.15) 50%,rgba(99,132,255,.08) 60%,transparent 100%);',
        'animation:mbg-scanline 3s ease-in-out infinite}',
        '@keyframes mbg-scanline{0%{top:0;opacity:0}10%{opacity:1}90%{opacity:1}100%{top:100%;opacity:0}}',
        '.mbg-block{text-align:center;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;',
        'font-size:14px;color:rgba(255,255,255,.5);padding:40px 20px;max-width:400px}',
        '.mbg-content-hidden{opacity:0!important;pointer-events:none!important}',
        '.mbg-content-reveal{transition:opacity .5s ease}',
        '.mbg-challenge{display:flex;flex-direction:column;align-items:center;animation:mbg-fadein .4s ease}',
        '@keyframes mbg-fadein{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}',
        '.mbg-hp-wrap{position:absolute;left:-9999px;top:-9999px;width:1px;height:1px;overflow:hidden;opacity:0}',
        '.mbg-challenge-text{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;font-size:12px;',
        'color:rgba(255,255,255,.4);letter-spacing:.5px;margin-bottom:20px}',
        '.mbg-verify-btn{display:inline-flex;align-items:center;gap:8px;padding:10px 28px;',
        'border:1.5px solid rgba(99,132,255,.35);border-radius:24px;background:rgba(99,132,255,.08);',
        'color:rgba(255,255,255,.85);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;',
        'font-size:13px;font-weight:500;letter-spacing:.5px;cursor:pointer;transition:all .25s ease;',
        'outline:none;position:relative;overflow:hidden}',
        '.mbg-verify-btn:hover{background:rgba(99,132,255,.18);border-color:rgba(99,132,255,.55);transform:scale(1.03)}',
        '.mbg-verify-btn:active{transform:scale(0.97)}',
    ].join('');

    function _injectStyles() {
        if (_stylesInjected) return;
        try {
            var style = document.createElement('style');
            style.textContent = _CSS;
            (document.head || document.documentElement).appendChild(style);
            _stylesInjected = true;
        } catch (e) {}
    }

    class OverlayController {
        constructor(style, config) {
            this._style = style || 'fullscreen';
            this._logoUrl = (config && config.logoUrl) || '';
            this._brand = (config && config.brand) || 'VERIFY';
            this._gateDurationMs = (config && config.gateDurationMs) || 2000;
            this._el = null;
            this._bar = null;
            this._statusEl = null;
            this._progressTimer = null;
            this._hiddenEls = [];
        }

        show() {
            if (this._style === 'none') return;
            try {
                _injectStyles();
                var children = document.body.children;
                for (var i = 0; i < children.length; i++) {
                    var child = children[i];
                    if (child.tagName === 'SCRIPT' || child.tagName === 'STYLE' || child.tagName === 'LINK') continue;
                    child.classList.add('mbg-content-hidden');
                    this._hiddenEls.push(child);
                }

                this._el = document.createElement('div');
                this._el.className = 'mbg';
                var logo = this._logoUrl
                    ? '<img class="mbg-logo" src="' + this._logoUrl + '" alt="" />'
                    : '';
                this._el.innerHTML =
                    '<div class="mbg-scan"></div>' +
                    '<div class="mbg-logo-wrap">' +
                        '<div class="mbg-ring"></div>' +
                        '<div class="mbg-ring mbg-ring2"></div>' +
                        logo +
                    '</div>' +
                    '<div class="mbg-wordmark">' + this._brand + '</div>' +
                    '<div class="mbg-progress"><div class="mbg-bar"></div></div>' +
                    '<div class="mbg-status">Verifying your connection</div>';
                document.body.appendChild(this._el);
                this._bar = this._el.querySelector('.mbg-bar');
                this._statusEl = this._el.querySelector('.mbg-status');
                this._el.offsetHeight;
                this._el.classList.add('mbg--active');
                this._animateProgress();
            } catch (e) {}
        }

        hide() {
            if (!this._el) return;
            try {
                if (this._progressTimer) clearInterval(this._progressTimer);
                this._el.classList.remove('mbg--active');
                this._el.classList.add('mbg--exit');
                var hiddenEls = this._hiddenEls;
                for (var i = 0; i < hiddenEls.length; i++) {
                    hiddenEls[i].classList.add('mbg-content-reveal');
                    hiddenEls[i].classList.remove('mbg-content-hidden');
                }
                var el = this._el;
                setTimeout(function () { try { el.remove(); } catch (e) {} }, 500);
                this._el = null;
                this._hiddenEls = [];
            } catch (e) {}
        }

        showBlock(message) {
            if (!this._el) return;
            try {
                if (this._progressTimer) clearInterval(this._progressTimer);
                var logo = this._logoUrl
                    ? '<img class="mbg-logo" src="' + this._logoUrl + '" alt="" />'
                    : '';
                this._el.innerHTML = '<div class="mbg-scan"></div>' +
                    '<div class="mbg-logo-wrap">' +
                        '<div class="mbg-ring"></div>' +
                        '<div class="mbg-ring mbg-ring2"></div>' +
                        logo +
                    '</div>' +
                    '<div class="mbg-block">' +
                    (message || 'Unable to verify your connection. Please try again.') +
                    '</div>';
            } catch (e) {}
        }

        _animateProgress() {
            var self = this;
            var progress = 0;
            var steps = [
                { at: 0, text: 'Verifying your connection' },
                { at: 25, text: 'Analyzing environment' },
                { at: 55, text: 'Checking signals' },
                { at: 80, text: 'Finalizing' },
            ];
            var stepIdx = 0;
            var duration = this._gateDurationMs;
            var interval = 50;
            var increment = (100 / (duration / interval)) * 0.92;
            this._progressTimer = setInterval(function () {
                progress = Math.min(progress + increment, 92);
                if (self._bar) self._bar.style.width = progress + '%';
                if (stepIdx < steps.length - 1 && progress >= steps[stepIdx + 1].at) {
                    stepIdx++;
                    if (self._statusEl) self._statusEl.textContent = steps[stepIdx].text;
                }
            }, interval);
        }

        completeProgress() {
            try {
                if (this._progressTimer) clearInterval(this._progressTimer);
                if (this._bar) this._bar.style.width = '100%';
                if (this._statusEl) this._statusEl.textContent = 'Verified';
            } catch (e) {}
        }

        showChallenge() {
            if (!this._el) return;
            try {
                if (this._progressTimer) clearInterval(this._progressTimer);
                var progress = this._el.querySelector('.mbg-progress');
                var status = this._el.querySelector('.mbg-status');
                if (progress) progress.remove();
                if (status) status.remove();
                var challengeHtml =
                    '<div class="mbg-challenge">' +
                        '<div class="mbg-hp-wrap" aria-hidden="true" tabindex="-1">' +
                            '<input type="text" name="mbg_contact_info" class="mbg-hp" autocomplete="off" tabindex="-1" />' +
                        '</div>' +
                        '<div class="mbg-challenge-text">Tap below to continue</div>' +
                        '<button type="button" class="mbg-verify-btn">' +
                            '<svg width="20" height="20" viewBox="0 0 20 20" fill="none">' +
                                '<path d="M4 10l4 4 8-8" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>' +
                            '</svg>' +
                            '<span>I\'m here</span>' +
                        '</button>' +
                    '</div>';
                this._el.insertAdjacentHTML('beforeend', challengeHtml);
            } catch (e) {}
        }

        resetToProgress() {
            if (!this._el) return;
            try {
                if (this._progressTimer) clearInterval(this._progressTimer);
                var challenge = this._el.querySelector('.mbg-challenge');
                if (challenge) challenge.remove();
                var html = '<div class="mbg-progress"><div class="mbg-bar"></div></div>' +
                    '<div class="mbg-status">Verifying your connection</div>';
                this._el.insertAdjacentHTML('beforeend', html);
                this._bar = this._el.querySelector('.mbg-bar');
                this._statusEl = this._el.querySelector('.mbg-status');
                this._animateProgress();
            } catch (e) {}
        }
    }

    // ── EventReporter ───────────────────────────────────────────
    class EventReporter {
        constructor(bouncer) {
            this._bouncer = bouncer;
            this._queue = [];
            this._sending = false;
            this._installed = false;
        }

        install() {
            if (this._installed) return;
            this._installed = true;
            var self = this;
            var prevOnError = window.onerror;
            window.onerror = function (message, source, lineno, colno, error) {
                self._onError(message, source, lineno, colno, error);
                if (typeof prevOnError === 'function') {
                    return prevOnError.call(window, message, source, lineno, colno, error);
                }
                return false;
            };
            var prevUnhandled = window.onunhandledrejection;
            window.onunhandledrejection = function (event) {
                var reason = event && event.reason;
                var message = (reason && (reason.message || String(reason))) || 'Unhandled promise rejection';
                var stack = (reason && reason.stack) || '';
                self.report('js:unhandled_rejection', 4, message, stack, { url: window.location.href });
                if (typeof prevUnhandled === 'function') {
                    return prevUnhandled.call(window, event);
                }
            };
        }

        report(category, level, title, details, metadata) {
            var payload = {
                duid: this._bouncer._duid,
                session_id: this._bouncer._sessionId,
                page_type: this._bouncer.pageType,
                event_type: category || 'client:event',
                data: {
                    level: level || 3,
                    title: String(title || '').substring(0, 500),
                    details: String(details || '').substring(0, 2000),
                    metadata: metadata || {},
                },
            };
            this._queue.push(payload);
            this._flush();
        }

        _onError(message, source, lineno, colno, error) {
            try {
                var stack = (error && error.stack) || '';
                this.report('js:error', 4, String(message || 'Unknown error').substring(0, 500), stack, {
                    source: source || '', line: lineno || 0, col: colno || 0, url: window.location.href,
                });
            } catch (e) {}
        }

        async _flush() {
            if (this._sending || this._queue.length === 0) return;
            this._sending = true;
            try {
                while (this._queue.length > 0) {
                    var payload = this._queue.shift();
                    await ApiClient.post(this._bouncer.eventUrl, payload);
                }
            } catch (e) {}
            this._sending = false;
        }
    }

    // ── MojoBouncer ─────────────────────────────────────────────
    class MojoBouncer {
        constructor(config) {
            config = config || {};
            this.pageType = config.pageType || 'login';
            // Endpoints point at django-mojo bouncer paths. No /submit anymore.
            this.gateUrl = config.gateUrl || '/api/account/bouncer/assess';
            this.eventUrl = config.eventUrl || '/api/account/bouncer/event';
            this.gateDurationMs = config.gateDurationMs || 2000;
            this.overlayStyle = config.overlayStyle || 'fullscreen';
            this.logoUrl = config.logoUrl || '';
            this.brand = config.brand || 'VERIFY';
            this.blockBehavior = config.blockBehavior || 'message';
            this.blockMessage = config.blockMessage || 'Unable to verify your connection. Please try again.';
            this.blockRedirectUrl = config.blockRedirectUrl || null;
            this.debug = config.debug || false;
            this.gateChallengeMinTimeMs = config.gateChallengeMinTimeMs || 800;
            this.gateChallengeEnabled = config.gateChallengeEnabled !== false;
            this.onDecision = config.onDecision || function () {};
            this.onError = config.onError || function () {};

            this._token = null;
            this._sessionId = DuidManager._generate();
            this._duid = DuidManager.load();
            this._challengeShownAt = null;
            this._challengeSignals = null;
            this._behaviorWatcher = new BehaviorWatcher();
            this._mouseAnalyzer = new MouseAnalyzer();
            this._eventReporter = new EventReporter(this);
            this._overlay = new OverlayController(this.overlayStyle, {
                logoUrl: this.logoUrl,
                brand: this.brand,
                gateDurationMs: this.gateDurationMs,
            });
        }

        async init() {
            try {
                this._eventReporter.install();
                this._overlay.show();
                this._behaviorWatcher.start();
                var self = this;
                this._mouseMoveHandler = function (e) { self._mouseAnalyzer.record(e.clientX, e.clientY); };
                document.addEventListener('mousemove', this._mouseMoveHandler, { passive: true });

                var fingerprintPromise = FingerprintCollector.collect();

                await this._sleep(this.gateDurationMs);

                if (this.gateChallengeEnabled) {
                    var attempt = 0;
                    var maxAttempts = 5;
                    var challengePassed = false;
                    while (attempt < maxAttempts) {
                        attempt++;
                        this._overlay.showChallenge();
                        this._challengeShownAt = Date.now();
                        await this._waitForChallengeClick();
                        if (this._validateChallenge()) {
                            this._challengeSignals = this._buildChallengeSignals(attempt);
                            challengePassed = true;
                            break;
                        }
                        this._log('Challenge failed attempt ' + attempt);
                        if (attempt < maxAttempts) {
                            this._overlay.resetToProgress();
                            await this._sleep(this.gateDurationMs);
                        }
                    }
                    if (!challengePassed) {
                        this._log('Max challenge attempts — failing open');
                        this._challengeSignals = this._buildChallengeSignals(attempt);
                    }
                }

                this._behaviorWatcher.stop();
                try { document.removeEventListener('mousemove', this._mouseMoveHandler); } catch (e) {}

                var fingerprintId = await fingerprintPromise;
                var payload = {
                    duid: this._duid,
                    fingerprint_id: fingerprintId,
                    session_id: this._sessionId,
                    page_type: this.pageType,
                    signals: {
                        environment: EnvironmentScanner.scan(),
                        behavior: this._behaviorWatcher.getSignals(),
                        mouse: this._mouseAnalyzer.analyze(),
                        gate_challenge: this._challengeSignals || {},
                        timing: {
                            page_load_epoch: Math.floor(Date.now() / 1000),
                            time_on_page_ms: this._challengeShownAt
                                ? (Date.now() - this._challengeShownAt + this.gateDurationMs)
                                : this.gateDurationMs,
                        },
                    },
                };

                var response = await ApiClient.post(this.gateUrl, payload);

                if (!response || !response.data) {
                    this._log('No response — failing open');
                    this._allowThrough();
                    return;
                }

                var decision = response.data.decision;
                this._token = response.data.token || null;
                this._log('Decision: ' + decision + ' score: ' + response.data.risk_score);
                this.onDecision({ decision: decision, score: response.data.risk_score, token: this._token });

                if (decision === 'block') {
                    this._handleBlock();
                } else {
                    this._allowThrough();
                }
            } catch (e) {
                this._log('Init error — failing open: ' + e.message);
                this.onError(e);
                this._allowThrough();
            }
        }

        getToken() { return this._token; }
        getDuid() { return this._duid; }
        getSessionId() { return this._sessionId; }

        reportEvent(category, level, title, details, metadata) {
            try {
                this._eventReporter.report(category, level, title, details, metadata);
            } catch (e) {}
        }

        _allowThrough() {
            var self = this;
            this._overlay.completeProgress();
            setTimeout(function () { self._overlay.hide(); }, 350);
        }

        _handleBlock() {
            if (this.blockBehavior === 'redirect' && this.blockRedirectUrl) {
                try { window.location.href = this.blockRedirectUrl; } catch (e) {}
            } else {
                this._overlay.showBlock(this.blockMessage);
            }
        }

        _isTouchDevice() {
            try {
                return navigator.maxTouchPoints > 0 || ('ontouchstart' in window);
            } catch (e) { return false; }
        }

        _waitForChallengeClick() {
            var self = this;
            return new Promise(function (resolve) {
                try {
                    var btn = self._overlay._el ? self._overlay._el.querySelector('.mbg-verify-btn') : null;
                    if (!btn) { resolve(); return; }
                    var timeout = setTimeout(function () { resolve(); }, 30000);
                    btn.addEventListener('click', function handler() {
                        clearTimeout(timeout);
                        btn.removeEventListener('click', handler);
                        resolve();
                    });
                } catch (e) { resolve(); }
            });
        }

        _validateChallenge() {
            try {
                var hp = this._overlay._el ? this._overlay._el.querySelector('.mbg-hp') : null;
                if (hp && hp.value.length > 0) return false;
                var elapsed = Date.now() - this._challengeShownAt;
                if (elapsed < this.gateChallengeMinTimeMs) return false;
                var bh = this._behaviorWatcher.getSignals();
                var isTouch = this._isTouchDevice();
                if (isTouch) {
                    if (bh.touch_event_count === 0 && bh.scroll_event_count === 0) return false;
                } else {
                    if (bh.mouse_move_count === 0) return false;
                }
                return true;
            } catch (e) { return true; }
        }

        _buildChallengeSignals(attempt) {
            try {
                var bh = this._behaviorWatcher.getSignals();
                var hp = this._overlay._el ? this._overlay._el.querySelector('.mbg-hp') : null;
                var clickedAt = Date.now();
                return {
                    honeypot_filled: hp ? hp.value.length > 0 : false,
                    time_to_click_ms: clickedAt - (this._challengeShownAt || clickedAt),
                    attempt_number: attempt,
                    had_mouse_movement: bh.mouse_move_count > 0,
                    had_touch_events: bh.touch_event_count > 0,
                    had_scroll_events: bh.scroll_event_count > 0,
                    had_keyboard_events: bh.keystroke_count > 0,
                    is_touch_device: this._isTouchDevice(),
                    challenge_shown_at: this._challengeShownAt || 0,
                    challenge_clicked_at: clickedAt,
                };
            } catch (e) {
                return { attempt_number: attempt };
            }
        }

        _sleep(ms) {
            return new Promise(function (resolve) { setTimeout(resolve, ms); });
        }

        _log(msg) {
            if (this.debug) {
                try { console.log('[MojoBouncer]', msg); } catch (e) {}
            }
        }
    }

    // ── Auto-init ───────────────────────────────────────────────
    // Embeds in any page (same- or cross-origin). Set data-api-base for
    // cross-origin embedding; falls back to same-origin otherwise.
    //
    // <script src="https://auth.example.com/account/static/mojo-bouncer.js"
    //         data-api-base="https://auth.example.com"
    //         data-page-type="login"
    //         data-logo-url="/logo.svg"
    //         defer></script>

    function _detectApiBase(script) {
        var explicit = script.getAttribute('data-api-base');
        if (explicit) return explicit.replace(/\/+$/, '');
        try {
            var src = script.src;
            if (src) {
                var url = new URL(src);
                return url.origin;
            }
        } catch (e) {}
        return window.location.origin;
    }

    function _detectPageType() {
        var path = window.location.pathname.toLowerCase();
        if (/\/(login|signin|auth)/.test(path)) return 'login';
        if (/\/(register|signup)/.test(path)) return 'registration';
        if (/\/(forgot|reset|password)/.test(path)) return 'password_reset';
        return 'embed';
    }

    function autoInit() {
        try {
            var script = document.querySelector('script[data-page-type]') ||
                         document.querySelector('script[data-api-base]') ||
                         document.querySelector('script[src*="mojo-bouncer.js"]');
            if (!script) return;

            var apiBase = _detectApiBase(script);
            var config = {
                pageType: script.getAttribute('data-page-type') || _detectPageType(),
                gateUrl: script.getAttribute('data-gate-url') || (apiBase + '/api/account/bouncer/assess'),
                eventUrl: script.getAttribute('data-event-url') || (apiBase + '/api/account/bouncer/event'),
                gateDurationMs: parseInt(script.getAttribute('data-gate-duration') || '2000', 10),
                overlayStyle: script.getAttribute('data-overlay-style') || 'fullscreen',
                logoUrl: script.getAttribute('data-logo-url') || '',
                brand: script.getAttribute('data-brand') || 'VERIFY',
                blockBehavior: script.getAttribute('data-block-behavior') || 'message',
                gateChallengeMinTimeMs: parseInt(script.getAttribute('data-gate-min-time') || '800', 10),
                gateChallengeEnabled: !script.hasAttribute('data-gate-no-challenge'),
                debug: script.hasAttribute('data-debug'),
            };

            var bouncer = new MojoBouncer(config);
            bouncer.init();
            window._mojoBouncerInstance = bouncer;
        } catch (e) {}
    }

    window.MojoBouncer = MojoBouncer;

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', autoInit);
    } else {
        autoInit();
    }

})();
