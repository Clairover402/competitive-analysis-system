/* api.js — HTTP + SSE
   Auto-inject JWT token, unified error handling. */

const BASE_URL = '';

function _headers() {
  var h = { 'Content-Type': 'application/json' };
  var token = localStorage.getItem('token');
  if (token) h['Authorization'] = 'Bearer ' + token;
  return h;
}

async function _handle(res) {
  if (!res.ok) {
    var body;
    try { body = await res.json(); } catch(e) { body = {}; }
    var msg = body.detail || body.message || res.statusText || 'request failed';
    throw new Error(msg);
  }
  return res.json();
}

export const API = {
  async get(path) {
    var res = await fetch(BASE_URL + path, { headers: _headers() });
    return _handle(res);
  },

  async post(path, body) {
    var res = await fetch(BASE_URL + path, {
      method: 'POST',
      headers: _headers(),
      body: JSON.stringify(body),
    });
    return _handle(res);
  },

  /* SSE with named event support
   * handlers: { onProgress, onComplete, onError, onMessage, onDone } */
  sse(path, handlers) {
    var token = localStorage.getItem('token');
    var url = BASE_URL + path;
    if (token) url += (url.indexOf('?') >= 0 ? '&' : '?') + 'token=' + encodeURIComponent(token);

    var es = new EventSource(url);
    var closed = false;

    function closeOnce() {
      if (!closed) { closed = true; es.close(); }
    }

    es.addEventListener('progress', function(event) {
      try {
        var data = JSON.parse(event.data);
        if (handlers.onProgress) handlers.onProgress(data);
        if (handlers.onMessage) handlers.onMessage(data);
      } catch(e) {}
    });

    es.addEventListener('complete', function(event) {
      try {
        var data = JSON.parse(event.data);
        closeOnce();
        if (handlers.onComplete) handlers.onComplete(data);
        if (handlers.onDone) handlers.onDone(data);
      } catch(e) {}
    });

    es.addEventListener('error', function(event) {
      /* ⚠️ `error` 是 EventSource 的保留事件名，同时被两处使用：
         (1) 后端 SSE 发送 event: error（task 失败） → MessageEvent，有 data
         (2) 浏览器原生连接错误 → Event，无 data
         用 event.data 是否存在来区分。 */
      if (!event.data) return;
      try {
        var data = JSON.parse(event.data);
        closeOnce();
        if (handlers.onError) handlers.onError(data);
      } catch(e) {}
    });

    es.onmessage = function(event) {
      try {
        var data = JSON.parse(event.data);
        if (handlers.onMessage) handlers.onMessage(data);
        if (data.type === 'done') {
          closeOnce();
          if (handlers.onDone) handlers.onDone(data);
        }
        if (data.type === 'error') {
          closeOnce();
          if (handlers.onError) handlers.onError(data);
        }
      } catch(e) {}
    };

    es.onerror = function() {
      if (!closed && handlers.onError) {
        handlers.onError({ message: 'SSE connection error', connection_error: true });
      }
    };

    return es;
  },
};
