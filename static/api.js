/**
 * Agent Router — Shared API Client
 *
 * Unified fetch wrapper used across the dashboards.
 * Configure auth strategy, base URL, and error handling via PicoAPI.configure().
 *
 * Usage:
 *   <script src="/api/static/api.js"></script>
 *   <script>
 *     PicoAPI.configure({
 *       baseUrl: 'http://localhost:8002',
 *       getToken: () => localStorage.getItem('pcAuthToken'),
 *       authHeader: 'Authorization',        // default
 *       authPrefix: 'Bearer ',              // default
 *       onAuthError: () => redirectToLogin(),
 *     });
 *     const users = await PicoAPI.get('/users');
 *   </script>
 */

var PicoAPI = (function() {
  'use strict';

  var _config = {
    baseUrl: '',
    getToken: function() { return ''; },
    authHeader: 'Authorization',
    authPrefix: 'Bearer ',
    onAuthError: null,
    retryOn401: false,
    defaultHeaders: {},
    requestIdHeader: 'X-Request-ID',
    timeout: 30000,
  };

  var _requestCount = 0;

  function configure(opts) {
    Object.keys(opts).forEach(function(k) {
      if (k in _config) _config[k] = opts[k];
    });
  }

  function _generateRequestId() {
    return 'web-' + Date.now().toString(36) + '-' + (++_requestCount).toString(36);
  }

  function _buildHeaders(extra) {
    var headers = {};

    // Default headers
    Object.keys(_config.defaultHeaders).forEach(function(k) {
      headers[k] = _config.defaultHeaders[k];
    });

    // Auth
    var token = _config.getToken();
    if (token) {
      headers[_config.authHeader] = _config.authPrefix + token;
    }

    // Content-Type for JSON bodies
    headers['Content-Type'] = 'application/json';

    // Request tracing
    if (_config.requestIdHeader) {
      headers[_config.requestIdHeader] = _generateRequestId();
    }

    // Merge extra headers (caller can override)
    if (extra) {
      Object.keys(extra).forEach(function(k) {
        headers[k] = extra[k];
      });
    }

    return headers;
  }

  function _buildUrl(path) {
    if (path.startsWith('http://') || path.startsWith('https://')) return path;
    return _config.baseUrl + path;
  }

  async function _request(method, path, opts) {
    opts = opts || {};
    var url = _buildUrl(path);
    var headers = _buildHeaders(opts.headers);

    var fetchOpts = {
      method: method,
      headers: headers,
    };

    if (opts.body !== undefined) {
      fetchOpts.body = typeof opts.body === 'string' ? opts.body : JSON.stringify(opts.body);
    }

    // Timeout via AbortController
    var controller = null;
    var timeoutId = null;
    var timeout = opts.timeout || _config.timeout;
    if (timeout > 0 && typeof AbortController !== 'undefined') {
      controller = new AbortController();
      fetchOpts.signal = controller.signal;
      timeoutId = setTimeout(function() { controller.abort(); }, timeout);
    }

    try {
      var response = await fetch(url, fetchOpts);

      if (timeoutId) clearTimeout(timeoutId);

      // 401 handling
      if (response.status === 401) {
        if (_config.retryOn401 && _config.onAuthError) {
          var retried = await _config.onAuthError(response);
          if (retried !== false) {
            // Retry once with fresh token
            return _request(method, path, Object.assign({}, opts, { _retried: true }));
          }
        } else if (_config.onAuthError && !opts._retried) {
          _config.onAuthError(response);
        }
      }

      if (!response.ok) {
        var errBody = null;
        try { errBody = await response.clone().json(); } catch(e) {}
        var err = new Error(errBody && errBody.message ? errBody.message : response.status + ' ' + response.statusText);
        err.status = response.status;
        err.response = response;
        err.body = errBody;
        err.requestId = headers[_config.requestIdHeader] || '';
        throw err;
      }

      // Return parsed JSON (or raw response if opts.raw)
      if (opts.raw) return response;
      var ct = response.headers.get('content-type') || '';
      if (ct.includes('application/json')) return response.json();
      return response.text();

    } catch(e) {
      if (timeoutId) clearTimeout(timeoutId);
      if (e.name === 'AbortError') {
        var timeoutErr = new Error('Request timed out after ' + timeout + 'ms');
        timeoutErr.status = 0;
        timeoutErr.timeout = true;
        throw timeoutErr;
      }
      throw e;
    }
  }

  // Public API methods
  function get(path, opts) { return _request('GET', path, opts); }
  function post(path, body, opts) { return _request('POST', path, Object.assign({ body: body }, opts)); }
  function put(path, body, opts) { return _request('PUT', path, Object.assign({ body: body }, opts)); }
  function patch(path, body, opts) { return _request('PATCH', path, Object.assign({ body: body }, opts)); }
  function del(path, opts) { return _request('DELETE', path, opts); }

  // Raw fetch with auth headers (for non-JSON endpoints)
  function raw(path, opts) { return _request('GET', path, Object.assign({ raw: true }, opts)); }

  // Expose configuration and methods
  return {
    configure: configure,
    get: get,
    post: post,
    put: put,
    patch: patch,
    del: del,
    raw: raw,
    request: _request,
    getConfig: function() { return Object.assign({}, _config); },
  };
})();
