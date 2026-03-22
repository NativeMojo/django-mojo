/**
 * mojo-auth
 * Standalone, vanilla JS authentication library for MOJO backends.
 * No dependencies. No HTML. No CSS. You own the markup.
 *
 * Usage (script tag):
 *   <script src="mojo-auth.umd.js"></script>
 *   <script>
 *     MojoAuth.init({ baseURL: 'https://api.example.com' });
 *     await MojoAuth.login('alice', 'password');
 *   </script>
 *
 * Usage (ES module):
 *   import MojoAuth from './mojo-auth.es.js';
 *   MojoAuth.init({ baseURL: 'https://api.example.com' });
 *
 * localStorage keys (aligned with web-mojo TokenManager):
 *   access_token, refresh_token
 */

(function (root, factory) {
    if (typeof define === 'function' && define.amd) {
        define([], factory);
    } else if (typeof module !== 'undefined' && module.exports) {
        module.exports = factory();
    } else {
        root.MojoAuth = factory();
    }
}(typeof globalThis !== 'undefined' ? globalThis : typeof window !== 'undefined' ? window : this, function () {

    // -------------------------------------------------------------------------
    // Internal state
    // -------------------------------------------------------------------------

    var _baseURL = '';
    var _endpoints = {};

    var KEYS = {
        access:  'access_token',
        refresh: 'refresh_token'
    };

    var DEFAULT_ENDPOINTS = {
        login:              '/api/login',
        forgotPassword:     '/api/auth/forgot',
        resetWithCode:      '/api/auth/password/reset/code',
        resetWithToken:     '/api/auth/password/reset/token',
        magicSend:          '/api/auth/magic/send',
        magicLogin:         '/api/auth/magic/login',
        passkeyLoginBegin:  '/api/auth/passkeys/login/begin',
        passkeyLoginComplete: '/api/auth/passkeys/login/complete',
        oauthBegin:         '/api/auth/oauth/{provider}/begin',
        oauthComplete:      '/api/auth/oauth/{provider}/complete',
        refreshToken:       '/api/refresh_token'
    };

    // -------------------------------------------------------------------------
    // Helpers
    // -------------------------------------------------------------------------

    function _bouncerToken() {
        try { return (window.MojoBouncer && window.MojoBouncer.getToken()) || null; } catch (e) { return null; }
    }

    function _duid() {
        try {
            if (window.MojoBouncer && window.MojoBouncer.getDuid) return window.MojoBouncer.getDuid();
            var d = localStorage.getItem('mojo_device_uid')
                || localStorage.getItem('mojo_duid')
                || null;
            if (d) return d;
            var m = document.cookie.match(/(?:^|; )mojo_device_uid=([^;]+)/);
            return m ? m[1] : null;
        } catch (e) { return null; }
    }

    function _withDevice(payload) {
        var bt = _bouncerToken();
        var duid = _duid();
        if (bt) payload.bouncer_token = bt;
        if (duid) payload.duid = duid;
        return payload;
    }

    function ep(key, params) {
        var path = _endpoints[key] || DEFAULT_ENDPOINTS[key] || '';
        if (params) {
            Object.keys(params).forEach(function (k) {
                path = path.replace('{' + k + '}', params[k]);
            });
        }
        return _baseURL.replace(/\/$/, '') + path;
    }

    function post(url, body, headers) {
        var h = Object.assign({ 'Content-Type': 'application/json' }, headers || {});
        return fetch(url, {
            method: 'POST',
            headers: h,
            body: JSON.stringify(body || {})
        }).then(function (res) {
            return res.json().then(function (json) {
                if (!res.ok) throw json;
                return json;
            });
        });
    }

    function get(url, headers) {
        return fetch(url, {
            method: 'GET',
            headers: Object.assign({ 'Content-Type': 'application/json' }, headers || {})
        }).then(function (res) {
            return res.json().then(function (json) {
                if (!res.ok) throw json;
                return json;
            });
        });
    }

    function saveTokens(data) {
        // Response shape: { status, data: { access_token, refresh_token, user } }
        var d = (data && data.data) ? data.data : data;
        if (!d || !d.access_token) throw new Error('No access_token in response');
        localStorage.setItem(KEYS.access, d.access_token);
        if (d.refresh_token) localStorage.setItem(KEYS.refresh, d.refresh_token);
        return d;
    }

    function getError(err) {
        if (!err) return 'An error occurred';
        if (typeof err === 'string') return err;
        return err.message || err.error ||
            (Array.isArray(err.errors) && err.errors[0] && err.errors[0].message) ||
            'An error occurred';
    }

    // -------------------------------------------------------------------------
    // WebAuthn helpers
    // -------------------------------------------------------------------------

    function base64urlToBuffer(base64url) {
        var base64 = base64url.replace(/-/g, '+').replace(/_/g, '/');
        var binary = atob(base64);
        var bytes = new Uint8Array(binary.length);
        for (var i = 0; i < binary.length; i++) {
            bytes[i] = binary.charCodeAt(i);
        }
        return bytes.buffer;
    }

    function bufferToBase64url(buffer) {
        var bytes = new Uint8Array(buffer);
        var binary = '';
        for (var i = 0; i < bytes.byteLength; i++) {
            binary += String.fromCharCode(bytes[i]);
        }
        return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=/g, '');
    }

    // -------------------------------------------------------------------------
    // Public API
    // -------------------------------------------------------------------------

    var MojoAuth = {

        /**
         * Initialize the library. Must be called before any auth method.
         * @param {object} config
         * @param {string} config.baseURL  - API base URL e.g. 'https://api.example.com'
         * @param {object} [config.endpoints] - Override any default endpoint paths
         */
        init: function (config) {
            if (!config || !config.baseURL) throw new Error('MojoAuth.init: baseURL is required');
            _baseURL = config.baseURL;
            _endpoints = Object.assign({}, config.endpoints || {});
        },

        // -----------------------------------------------------------------------
        // JWT Login
        // -----------------------------------------------------------------------

        /**
         * Login with username/email and password.
         * Stores access_token and refresh_token in localStorage on success.
         * @param {string} username
         * @param {string} password
         * @returns {Promise<object>} response data ({ access_token, refresh_token, user })
         *
         * NOTE: If MFA is enabled, the response will contain { mfa_required: true, mfa_token, mfa_methods }
         * instead of tokens. Check result.mfa_required before assuming login is complete.
         */
        login: function (username, password) {
            return post(ep('login'), _withDevice({ username: username, password: password }))
                .then(function (resp) {
                    var d = resp.data || resp;
                    // MFA challenge — tokens not issued yet, return raw for caller to handle
                    if (d.mfa_required) return d;
                    return saveTokens(resp);
                });
        },

        // -----------------------------------------------------------------------
        // Password Reset — Code method
        // -----------------------------------------------------------------------

        /**
         * Request a 6-digit reset code sent to the user's email.
         * @param {string} email
         * @returns {Promise<object>}
         */
        forgotPasswordCode: function (email) {
            return post(ep('forgotPassword'), _withDevice({ email: email, method: 'code' }));
        },

        /**
         * Complete password reset using the emailed code.
         * Stores tokens on success (logs user in automatically).
         * @param {string} email
         * @param {string} code     - 6-digit code from email
         * @param {string} newPassword
         * @returns {Promise<object>}
         */
        resetWithCode: function (email, code, newPassword) {
            return post(ep('resetWithCode'), {
                email: email,
                code: code,
                new_password: newPassword
            }).then(saveTokens);
        },

        // -----------------------------------------------------------------------
        // Password Reset — Link / Token method
        // -----------------------------------------------------------------------

        /**
         * Request a password reset link (pr: token) sent to the user's email.
         * @param {string} email
         * @returns {Promise<object>}
         */
        forgotPasswordLink: function (email) {
            return post(ep('forgotPassword'), _withDevice({ email: email, method: 'link' }));
        },

        /**
         * Complete password reset using the token from the reset link email.
         * Stores tokens on success.
         * @param {string} token      - pr: prefixed token from URL
         * @param {string} newPassword
         * @returns {Promise<object>}
         */
        resetWithToken: function (token, newPassword) {
            return post(ep('resetWithToken'), {
                token: token,
                new_password: newPassword
            }).then(saveTokens);
        },

        // -----------------------------------------------------------------------
        // Magic Login (passwordless email link)
        // -----------------------------------------------------------------------

        /**
         * Send a magic login link (ml: token) to the user's email.
         * @param {string} email
         * @returns {Promise<object>}
         */
        sendMagicLink: function (email) {
            return post(ep('magicSend'), _withDevice({ email: email }));
        },

        /**
         * Complete login using the ml: token from the magic link URL.
         * Stores tokens on success.
         * @param {string} token  - ml: prefixed token from URL ?token=ml:...
         * @returns {Promise<object>}
         */
        loginWithMagicToken: function (token) {
            return post(ep('magicLogin'), _withDevice({ token: token })).then(saveTokens);
        },

        /**
         * Convenience: read ?token= from current URL and login if it's a magic link token.
         * Cleans the token from the URL after reading it.
         * @returns {Promise<object|null>} resolves with auth data or null if no token found
         */
        handleMagicTokenFromURL: function () {
            var params = new URLSearchParams(window.location.search);
            var token = params.get('token');
            if (!token || token.indexOf('ml:') !== 0) return Promise.resolve(null);

            // Clean token from URL
            params.delete('token');
            var newUrl = params.toString()
                ? window.location.pathname + '?' + params.toString()
                : window.location.pathname;
            window.history.replaceState({}, '', newUrl);

            return MojoAuth.loginWithMagicToken(token);
        },

        // -----------------------------------------------------------------------
        // Passkey Login (WebAuthn)
        // -----------------------------------------------------------------------

        /**
         * Check if WebAuthn passkeys are supported in this browser.
         * @returns {boolean}
         */
        isPasskeySupported: function () {
            return typeof window !== 'undefined' &&
                typeof window.PublicKeyCredential !== 'undefined' &&
                typeof navigator.credentials !== 'undefined' &&
                typeof navigator.credentials.get === 'function';
        },

        /**
         * Login with a passkey (WebAuthn) without requiring a username.
         * Uses discoverable credentials — browser shows all passkeys for this domain.
         * Handles the full begin → browser prompt → complete flow.
         * Stores tokens on success.
         * @returns {Promise<object>}
         */
        loginWithPasskeyDiscoverable: function () {
            if (!MojoAuth.isPasskeySupported()) {
                return Promise.reject(new Error('Passkeys are not supported in this browser'));
            }

            // Step 1: Begin with no username — server returns empty allowCredentials
            return post(ep('passkeyLoginBegin'), {})
                .then(function (resp) {
                    var d = resp.data || resp;
                    var challengeId = d.challenge_id;
                    var publicKey = d.publicKey;

                    publicKey.challenge = base64urlToBuffer(publicKey.challenge);
                    if (publicKey.allowCredentials) {
                        publicKey.allowCredentials = publicKey.allowCredentials.map(function (c) {
                            return Object.assign({}, c, { id: base64urlToBuffer(c.id) });
                        });
                    }

                    return navigator.credentials.get({ publicKey: publicKey })
                        .then(function (credential) {
                            if (!credential) throw new Error('No credential received from authenticator');

                            return post(ep('passkeyLoginComplete'), _withDevice({
                                challenge_id: challengeId,
                                credential: {
                                    id: credential.id,
                                    rawId: bufferToBase64url(credential.rawId),
                                    type: credential.type,
                                    response: {
                                        clientDataJSON:    bufferToBase64url(credential.response.clientDataJSON),
                                        authenticatorData: bufferToBase64url(credential.response.authenticatorData),
                                        signature:         bufferToBase64url(credential.response.signature),
                                        userHandle:        credential.response.userHandle
                                            ? bufferToBase64url(credential.response.userHandle)
                                            : null
                                    }
                                }
                            }));
                        });
                })
                .then(saveTokens);
        },

        /**
         * Login with a passkey (WebAuthn).
         * Handles the full begin → browser prompt → complete flow.
         * Stores tokens on success.
         * @param {string} username  - username or email
         * @returns {Promise<object>}
         */
        loginWithPasskey: function (username) {
            if (!MojoAuth.isPasskeySupported()) {
                return Promise.reject(new Error('Passkeys are not supported in this browser'));
            }

            // Step 1: Begin
            return post(ep('passkeyLoginBegin'), { username: username })
                .then(function (resp) {
                    var d = resp.data || resp;
                    var challengeId = d.challenge_id;
                    var publicKey = d.publicKey;

                    // Decode base64url fields the browser expects as ArrayBuffers
                    publicKey.challenge = base64urlToBuffer(publicKey.challenge);
                    if (publicKey.allowCredentials) {
                        publicKey.allowCredentials = publicKey.allowCredentials.map(function (c) {
                            return Object.assign({}, c, { id: base64urlToBuffer(c.id) });
                        });
                    }

                    // Step 2: Browser authenticator prompt
                    return navigator.credentials.get({ publicKey: publicKey })
                        .then(function (credential) {
                            if (!credential) throw new Error('No credential received from authenticator');

                            // Step 3: Complete
                            return post(ep('passkeyLoginComplete'), _withDevice({
                                challenge_id: challengeId,
                                credential: {
                                    id: credential.id,
                                    rawId: bufferToBase64url(credential.rawId),
                                    type: credential.type,
                                    response: {
                                        clientDataJSON:    bufferToBase64url(credential.response.clientDataJSON),
                                        authenticatorData: bufferToBase64url(credential.response.authenticatorData),
                                        signature:         bufferToBase64url(credential.response.signature),
                                        userHandle:        credential.response.userHandle
                                            ? bufferToBase64url(credential.response.userHandle)
                                            : null
                                    }
                                }
                            }));
                        });
                })
                .then(saveTokens);
        },

        // -----------------------------------------------------------------------
        // Google OAuth
        // -----------------------------------------------------------------------

        /**
         * Start Google OAuth login.
         * Fetches the authorization URL from the backend and redirects the browser.
         * @returns {Promise<void>}
         */
        /**
         * Start an OAuth login flow for any provider.
         * Stores the provider in sessionStorage so the callback page knows which provider to complete.
         * @param {string} provider     - e.g. 'google', 'apple'
         * @param {string} [callbackUrl] - URL Google/Apple should redirect back to.
         *                                 Defaults to the current page URL (strip query/hash).
         *                                 Must be registered in the provider's console AND
         *                                 allowed by the backend (ALLOWED_REDIRECT_URLS or per-group).
         * @returns {Promise<void>}
         */
        startOAuthLogin: function (provider, callbackUrl) {
            sessionStorage.setItem('oauth_provider', provider);
            var redirectUri = callbackUrl || (window.location.origin + window.location.pathname);
            var url = ep('oauthBegin', { provider: provider }) + '?redirect_uri=' + encodeURIComponent(redirectUri);
            return get(url)
                .then(function (resp) {
                    var d = resp.data || resp;
                    if (!d.auth_url) throw new Error('No auth_url in OAuth begin response');
                    window.location.href = d.auth_url;
                });
        },

        startGoogleLogin: function (callbackUrl) {
            return MojoAuth.startOAuthLogin('google', callbackUrl);
        },

        startAppleLogin: function (callbackUrl) {
            return MojoAuth.startOAuthLogin('apple', callbackUrl);
        },

        /**
         * Complete OAuth login for any provider — call this on your OAuth callback page.
         * Reads ?code and ?state from the current URL automatically.
         * Stores tokens on success.
         * @param {string} provider  - e.g. 'google'
         * @returns {Promise<object>}
         */
        completeOAuthLogin: function (provider) {
            var params = new URLSearchParams(window.location.search);
            var code  = params.get('code');
            var state = params.get('state');
            if (!code) return Promise.reject(new Error('No OAuth code in URL'));
            return post(ep('oauthComplete', { provider: provider }), _withDevice({
                code: code,
                state: state
            })).then(saveTokens);
        },

        /** @deprecated use completeOAuthLogin('google') */
        completeGoogleLogin: function () {
            return MojoAuth.completeOAuthLogin('google');
        },

        // -----------------------------------------------------------------------
        // Session helpers
        // -----------------------------------------------------------------------

        /**
         * Log out — clears tokens from localStorage.
         */
        logout: function () {
            localStorage.removeItem(KEYS.access);
            localStorage.removeItem(KEYS.refresh);
        },

        /**
         * Check if the user has a stored access token.
         * Does not validate expiry — use getTokenPayload() for that.
         * @returns {boolean}
         */
        isAuthenticated: function () {
            return !!localStorage.getItem(KEYS.access);
        },

        /**
         * Get the raw access token string.
         * @returns {string|null}
         */
        getToken: function () {
            return localStorage.getItem(KEYS.access);
        },

        /**
         * Get the raw refresh token string.
         * @returns {string|null}
         */
        getRefreshToken: function () {
            return localStorage.getItem(KEYS.refresh);
        },

        /**
         * Get the Authorization header value for use in API requests.
         * @returns {string|null}  e.g. "Bearer eyJhbGci..."
         */
        getAuthHeader: function () {
            var t = localStorage.getItem(KEYS.access);
            return t ? 'Bearer ' + t : null;
        },

        /**
         * Decode the JWT access token payload (client-side only, no signature verification).
         * @returns {object|null}
         */
        getTokenPayload: function () {
            var token = localStorage.getItem(KEYS.access);
            if (!token) return null;
            try {
                var parts = token.split('.');
                if (parts.length !== 3) return null;
                var base64 = parts[1].replace(/-/g, '+').replace(/_/g, '/');
                var padding = 4 - (base64.length % 4);
                if (padding !== 4) base64 += '===='.slice(0, padding);
                return JSON.parse(atob(base64));
            } catch (_) {
                return null;
            }
        },

        /**
         * Check if the stored access token is expired (based on JWT exp claim).
         * Returns true if expired or if token is missing/undecodable.
         * @returns {boolean}
         */
        isTokenExpired: function () {
            var payload = MojoAuth.getTokenPayload();
            if (!payload || !payload.exp) return true;
            return Math.floor(Date.now() / 1000) >= payload.exp;
        },

        /**
         * Refresh the access token using the stored refresh token.
         * Stores the new tokens on success.
         * @returns {Promise<object>}
         */
        refreshToken: function () {
            var refreshToken = localStorage.getItem(KEYS.refresh);
            if (!refreshToken) return Promise.reject(new Error('No refresh token stored'));
            return post(ep('refreshToken'), { refresh_token: refreshToken }).then(saveTokens);
        },

        // -----------------------------------------------------------------------
        // Error helper
        // -----------------------------------------------------------------------

        /**
         * Extract a human-readable error message from a caught error.
         * Works with the MOJO backend error shapes.
         * @param {any} err  - the caught error (object, string, etc.)
         * @returns {string}
         */
        getError: getError

    };

    return MojoAuth;

}));
