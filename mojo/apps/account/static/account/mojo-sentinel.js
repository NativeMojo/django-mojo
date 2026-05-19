/**
 * mojo-sentinel.js — lightweight in-session bot-detection telemetry.
 *
 * Continuous monitoring while a user is on the page. No UI, no fingerprinting,
 * no gate logic — pure background telemetry. Pairs with mojo-bouncer.js (gate)
 * by sharing the same `mojo_device_uid` localStorage key for identity continuity.
 *
 * Auto-collected passive signals (no app integration required):
 *   visibility_transitions, focus_blur_count, paste_events,
 *   click_count, click_coord_buckets, inter_action_interval_ms,
 *   page_lifetime_ms, idle_gaps_count
 *
 * Public API:
 *   MojoSentinel.observe(category, payload)   — push a custom event
 *   MojoSentinel.flush()                      — force immediate flush
 *   MojoSentinel.getDuid()                    — read the shared duid
 *
 * Rules:
 *   - Never throws; failures are silent
 *   - No effect on the host page if the bouncer endpoint is unreachable
 *   - Batched flushes minimize network noise (default 15s or 25 events)
 *   - Final flush on pagehide via navigator.sendBeacon
 *
 * @version 1.0.0
 */
(function () {
    'use strict';

    var DUID_KEY = 'mojo_device_uid';
    var IDLE_THRESHOLD_MS = 60000;

    function _readDuid() {
        try {
            var d = localStorage.getItem(DUID_KEY);
            if (d) return d;
        } catch (e) {}
        try {
            var m = document.cookie.match(/(?:^|; )mojo_device_uid=([^;]+)/);
            if (m) return m[1];
        } catch (e) {}
        return _generateDuid();
    }

    function _generateDuid() {
        try {
            return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
                var r = Math.random() * 16 | 0;
                return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
            });
        } catch (e) {
            return 'fallback-' + Date.now();
        }
    }

    function _persistDuid(duid) {
        try { localStorage.setItem(DUID_KEY, duid); } catch (e) {}
    }

    function _sessionId() {
        return _generateDuid();
    }

    function Sentinel(config) {
        this._duid = _readDuid();
        _persistDuid(this._duid);
        this._sessionId = _sessionId();
        this._apiBase = (config.apiBase || '').replace(/\/+$/, '');
        this._endpoint = this._apiBase + '/api/account/bouncer/event';
        this._pageType = config.pageType || 'embed';
        this._context = config.context || '';
        this._flushIntervalMs = config.flushIntervalMs || 15000;
        this._flushSize = config.flushSize || 25;

        // Passive auto-collected counters
        this._counters = {
            visibility_transitions: 0,
            focus_blur_count: 0,
            paste_events: 0,
            click_count: 0,
        };
        this._coordBuckets = {};
        this._intervals = [];
        this._idleGaps = 0;
        this._lastActionMs = 0;
        this._bootMs = Date.now();

        this._buffer = [];
        this._timer = null;
        this._installed = false;
    }

    Sentinel.prototype.install = function () {
        if (this._installed) return;
        this._installed = true;
        var self = this;

        try {
            document.addEventListener('visibilitychange', function () {
                self._counters.visibility_transitions++;
            });
        } catch (e) {}

        try {
            window.addEventListener('focus', function () { self._counters.focus_blur_count++; });
            window.addEventListener('blur', function () { self._counters.focus_blur_count++; });
        } catch (e) {}

        try {
            document.addEventListener('paste', function (e) {
                self._counters.paste_events++;
                var target = e && e.target ? self._describeTarget(e.target) : '';
                self._buffer.push(self._buildPayload({
                    event_type: 'paste_event',
                    data: { target_tag: target },
                }));
                self._maybeFlush();
            }, true);
        } catch (e) {}

        try {
            document.addEventListener('click', function (e) {
                self._recordAction();
                self._counters.click_count++;
                if (typeof e.clientX === 'number' && typeof e.clientY === 'number') {
                    var key = Math.round(e.clientX / 8) + ',' + Math.round(e.clientY / 8);
                    self._coordBuckets[key] = true;
                }
            }, true);
        } catch (e) {}

        ['mousemove', 'keydown', 'touchstart', 'scroll'].forEach(function (evt) {
            try {
                document.addEventListener(evt, function () { self._recordAction(); }, { passive: true });
            } catch (e) {}
        });

        try {
            window.addEventListener('pagehide', function () { self._flushBeacon(); });
        } catch (e) {}

        this._timer = setInterval(function () { self._tick(); }, this._flushIntervalMs);
    };

    Sentinel.prototype._recordAction = function () {
        var now = Date.now();
        if (this._lastActionMs > 0) {
            var gap = now - this._lastActionMs;
            if (this._intervals.length < 500) {
                this._intervals.push(gap);
            }
            if (gap >= IDLE_THRESHOLD_MS) {
                this._idleGaps++;
            }
        }
        this._lastActionMs = now;
    };

    Sentinel.prototype._describeTarget = function (el) {
        try {
            var tag = (el.tagName || '').toLowerCase();
            var type = el.type ? '[type=' + el.type + ']' : '';
            return tag + type;
        } catch (e) { return ''; }
    };

    Sentinel.prototype._buildPayload = function (event) {
        return {
            event_type: event.event_type || event.category || 'client_event',
            data: event.data || {},
            context: this._context,
        };
    };

    Sentinel.prototype._snapshotEvent = function () {
        // Periodic snapshot: passive counters + buckets + intervals + lifetime.
        // This is what the universal stream analyzers read from.
        var data = {
            visibility_transitions: this._counters.visibility_transitions,
            focus_blur_count: this._counters.focus_blur_count,
            paste_events: this._counters.paste_events,
            click_count: this._counters.click_count,
            click_coord_buckets: Object.keys(this._coordBuckets),
            inter_action_interval_ms: this._intervals.slice(),
            idle_gaps_count: this._idleGaps,
            page_lifetime_ms: Date.now() - this._bootMs,
        };
        // Reset rolling-window fields so next snapshot starts fresh on intervals
        // and coord buckets — total counters (visibility_transitions, idle_gaps,
        // page_lifetime_ms) keep accumulating.
        this._coordBuckets = {};
        this._intervals = [];
        return this._buildPayload({
            event_type: 'sentinel_snapshot',
            data: data,
        });
    };

    Sentinel.prototype.observe = function (category, payload) {
        try {
            this._buffer.push(this._buildPayload({
                event_type: category || 'observe',
                data: payload || {},
            }));
            this._maybeFlush();
        } catch (e) {}
    };

    Sentinel.prototype._maybeFlush = function () {
        if (this._buffer.length >= this._flushSize) this.flush();
    };

    Sentinel.prototype._tick = function () {
        try {
            this._buffer.push(this._snapshotEvent());
            this.flush();
        } catch (e) {}
    };

    Sentinel.prototype.flush = function () {
        if (this._buffer.length === 0) return;
        var events = this._buffer.splice(0);
        var body = {
            duid: this._duid,
            session_id: this._sessionId,
            page_type: this._pageType,
            events: events,
        };
        try {
            fetch(this._endpoint, {
                method: 'POST',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
                keepalive: true,
            }).catch(function () {});
        } catch (e) {}
    };

    Sentinel.prototype._flushBeacon = function () {
        if (this._buffer.length === 0) return;
        try {
            // Include one final snapshot so the page-lifetime-end counters land.
            this._buffer.push(this._snapshotEvent());
        } catch (e) {}
        var events = this._buffer.splice(0);
        var body = JSON.stringify({
            duid: this._duid,
            session_id: this._sessionId,
            page_type: this._pageType,
            events: events,
        });
        try {
            if (navigator.sendBeacon) {
                var blob = new Blob([body], { type: 'application/json' });
                navigator.sendBeacon(this._endpoint, blob);
                return;
            }
        } catch (e) {}
        try {
            fetch(this._endpoint, {
                method: 'POST', credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: body, keepalive: true,
            }).catch(function () {});
        } catch (e) {}
    };

    Sentinel.prototype.getDuid = function () { return this._duid; };

    // ── Auto-init ───────────────────────────────────────────────
    function _detectApiBase(script) {
        var explicit = script.getAttribute('data-api-base');
        if (explicit) return explicit.replace(/\/+$/, '');
        try {
            var src = script.src;
            if (src) return new URL(src).origin;
        } catch (e) {}
        return window.location.origin;
    }

    function autoInit() {
        try {
            var script = document.querySelector('script[data-api-base][src*="mojo-sentinel"]') ||
                         document.querySelector('script[src*="mojo-sentinel.js"]');
            if (!script) return;
            var instance = new Sentinel({
                apiBase: _detectApiBase(script),
                pageType: script.getAttribute('data-page-type') || 'embed',
                context: script.getAttribute('data-context') || '',
                flushIntervalMs: parseInt(script.getAttribute('data-flush-interval-ms') || '15000', 10),
                flushSize: parseInt(script.getAttribute('data-flush-size') || '25', 10),
            });
            instance.install();
            window.MojoSentinel = instance;
        } catch (e) {}
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', autoInit);
    } else {
        autoInit();
    }

    // Constructor exported for manual init by host apps that need it.
    if (!window.MojoSentinel) window.MojoSentinel = Sentinel;
})();
