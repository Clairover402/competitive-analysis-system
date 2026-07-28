/* task-detail.js — SSE Real-time Task Monitor */

import { API } from '../js/api.js';
import { SFX } from '../js/sounds.js';

var _es = null;
var _autoScroll = true;
var _reconnectTimer = null;
var _taskId = '';

var STATUS_MAP = {
  'pending':     { label: '\u7B49\u5F85\u4E2D',   cls: 'pixel-badge-warning' },
  'collecting':  { label: '\u91C7\u96C6\u4E2D',   cls: 'pixel-badge-warning' },
  'analyzing':   { label: '\u5206\u6790\u4E2D',   cls: 'pixel-badge-warning' },
  'writing':     { label: '\u64B0\u5199\u4E2D',   cls: 'pixel-badge-warning' },
  'qa':          { label: '\u8D28\u68C0\u4E2D',   cls: 'pixel-badge-warning' },
  'completed':   { label: '\u5DF2\u5B8C\u6210',   cls: 'pixel-badge-success' },
  'failed':      { label: '\u5931\u8D25',         cls: 'pixel-badge-danger'  },
};

var AGENT_NAMES = {
  'collector': 'COLLECTOR',
  'analyzer':  'ANALYZER',
  'writer':    'WRITER',
  'quality':   'QUALITY',
  'system':    'SYSTEM',
};

function now() {
  var d = new Date();
  return ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2) + ':' + ('0' + d.getSeconds()).slice(-2);
}

export function render(params) {
  _taskId = params.id || '';

  return '' +
  '<div class="pixel-flex pixel-flex-col" style="min-height:100vh">' +

    '<!-- Top Nav -->' +
    '<div class="pixel-flex pixel-items-center pixel-gap-md" style="height:56px; padding:0 20px; background:var(--pixel-dark); border-bottom:2px solid var(--pixel-mid); flex-shrink:0">' +
      '<button id="btn-back" class="pixel-btn-secondary">&larr; \u8FD4\u56DE</button>' +
      '<span id="task-title" class="pixel-font-display pixel-text-green pixel-flex-1" style="font-size:15px; letter-spacing:2px">\u52A0\u8F7D\u4E2D...</span>' +
      '<span id="task-badge" class="pixel-badge pixel-badge-warning">-</span>' +
    '</div>' +

    '<!-- Progress Area -->' +
    '<div class="pixel-box" style="margin:20px 20px 0; flex-shrink:0">' +
      '<div class="pixel-flex pixel-items-center pixel-gap-md pixel-mb-sm">' +
        '<div class="pixel-progress pixel-flex-1">' +
          '<div id="task-progress-fill" class="pixel-progress-fill" style="width:0%"></div>' +
        '</div>' +
        '<span id="task-progress-pct" class="pixel-font-mono pixel-text-green" style="font-size:16px; min-width:48px; text-align:right">0%</span>' +
      '</div>' +
      '<p id="task-phase" class="pixel-font-mono pixel-text-light" style="font-size:14px">\u521D\u59CB\u5316\u4E2D...</p>' +
    '</div>' +

    '<!-- Terminal Area -->' +
    '<div class="pixel-flex-1" style="margin:20px; display:flex; flex-direction:column; min-height:0">' +
      '<div class="pixel-terminal" id="task-terminal" style="flex:1; max-height:none">' +
        '<div class="pixel-terminal-header">' +
          '<span class="pixel-terminal-title">\u25B8 SYSTEM LOG</span>' +
        '</div>' +
        '<div id="task-log-body">' +
          '<span class="pixel-log-line pixel-text-light">\u25B8 \u6B63\u5728\u8FDE\u63A5...</span>' +
        '</div>' +
      '</div>' +

      '<!-- Report button area (hidden initially) -->' +
      '<div id="task-report-area" class="pixel-hidden" style="margin-top:12px"></div>' +
    '</div>' +

  '</div>';
}

export function mount(params) {
  var id = params.id || _taskId;
  _taskId = id;

  var titleEl = document.getElementById('task-title');
  var badgeEl = document.getElementById('task-badge');
  var progressFill = document.getElementById('task-progress-fill');
  var progressPct = document.getElementById('task-progress-pct');
  var phaseEl = document.getElementById('task-phase');
  var logBody = document.getElementById('task-log-body');
  var terminal = document.getElementById('task-terminal');
  var reportArea = document.getElementById('task-report-area');
  var btnBack = document.getElementById('btn-back');

  var completed = false;
  var reportId = null;
  var qualityScore = null;

  if (btnBack) {
    btnBack.addEventListener('click', function() {
      SFX.click();
      window.location.hash = '#/dashboard';
    });
  }

  /* Auto-scroll */
  if (terminal) {
    terminal.addEventListener('scroll', function() {
      var diff = terminal.scrollHeight - terminal.scrollTop - terminal.clientHeight;
      _autoScroll = diff < 50;
    });
  }

  function scrollBottom() {
    if (_autoScroll && terminal) {
      terminal.scrollTop = terminal.scrollHeight;
    }
  }

  function addLog(agent, message, isError) {
    if (!logBody) return;
    var agentName = AGENT_NAMES[agent] || agent.toUpperCase();
    var agentCls = 'pixel-log-agent-' + agent;
    if (isError) agentCls = 'pixel-log-agent-error';

    var line = document.createElement('span');
    line.className = 'pixel-log-line';
    line.innerHTML = '<span class="pixel-log-time">[' + now() + ']</span> ' +
      '<span class="' + agentCls + '">' + agentName + ' ::</span> ' +
      '<span class="pixel-text-white">' + escHtml(message) + '</span>';
    logBody.appendChild(line);
    scrollBottom();
  }

  function escHtml(s) {
    var d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }

  function updateProgress(pct, phase) {
    if (progressFill) progressFill.style.width = Math.min(100, Math.max(0, Math.round(pct))) + '%';
    if (progressPct) progressPct.textContent = Math.round(pct) + '%';
    if (phase && phaseEl) phaseEl.textContent = phase;
  }

  function updateStatus(status) {
    var s = STATUS_MAP[status] || { label: status, cls: 'pixel-badge-info' };
    if (badgeEl) {
      badgeEl.textContent = s.label;
      badgeEl.className = 'pixel-badge ' + s.cls;
    }
  }

  function showReportButton() {
    if (!reportArea || !reportId) return;
    reportArea.classList.remove('pixel-hidden');
    reportArea.innerHTML = '' +
    '<div class="pixel-card" style="cursor:pointer; text-align:center"' +
        ' onclick="window.location.hash=' + "'#/report/" + _taskId + '/' + reportId + "'" + '">' +
      '<span class="pixel-font-display pixel-text-green" style="font-size:14px; letter-spacing:2px">' +
        '\u25B8 \u67E5\u770B\u5206\u6790\u62A5\u544A' +
      '</span>' +
      (qualityScore !== null
        ? ' <span class="pixel-font-mono pixel-text-amber" style="font-size:14px">[' + qualityScore + '/100]</span>'
        : '') +
    '</div>';
  }

  function connectSSE() {
    if (completed) return;

    addLog('system', '\u5EFA\u7ACB\u8FDE\u63A5...');

    _es = API.sse('/api/tasks/' + id + '/stream', {
      onProgress: function(data) {
        var agent = data.agent || 'system';
        var msg = data.message || '';
        var pct = data.progress_pct;
        var action = data.action || '';

        if (msg) addLog(agent, msg);
        if (pct !== undefined && pct !== null) {
          updateProgress(pct * 100, (action || msg));
        }
        if (data.status) updateStatus(data.status);
      },

      onComplete: function(data) {
        completed = true;
        reportId = data.report_id || '';
        qualityScore = data.quality_score;
        var elapsed = data.elapsed_ms ? (data.elapsed_ms / 1000).toFixed(0) + 's' : '';

        updateProgress(100, '\u4EFB\u52A1\u5B8C\u6210');
        updateStatus(data.status || 'completed');

        addLog('system', '\u4EFB\u52A1\u5B8C\u6210' + (elapsed ? ' (\u8017\u65F6 ' + elapsed + ')' : ''));
        if (qualityScore !== null && qualityScore !== undefined) {
          addLog('system', '\u8D28\u91CF\u8BC4\u5206: ' + qualityScore + '/100');
        }

        SFX.confirm();
        if (reportId) showReportButton();
      },

      onError: function(data) {
        if (data.connection_error) {
          addLog('system', '\u8FDE\u63A5\u4E2D\u65AD\uFF0C3 \u79D2\u540E\u91CD\u8FDE...', true);
          clearReconnect();
          _reconnectTimer = setTimeout(function() { connectSSE(); }, 3000);
        } else {
          completed = true;
          addLog('error', data.message || '\u4EFB\u52A1\u6267\u884C\u5931\u8D25', true);
          updateStatus('failed');
          SFX.error();
        }
      },
    });
  }

  function clearReconnect() {
    if (_reconnectTimer) { clearTimeout(_reconnectTimer); _reconnectTimer = null; }
  }

  /* Load task info */
  API.get('/api/tasks/' + id).then(function(task) {
    if (titleEl) titleEl.textContent = task.title || '\u672A\u547D\u540D\u4EFB\u52A1';
    updateStatus(task.status);

    /* If already completed, show report button */
    if (task.status === 'completed' || task.status === 'failed') {
      completed = true;
      updateProgress(100, task.status === 'completed' ? '\u4EFB\u52A1\u5DF2\u5B8C\u6210' : '\u4EFB\u52A1\u5DF2\u5931\u8D25');
      addLog('system', task.status === 'completed' ? '\u4EFB\u52A1\u5DF2\u5B8C\u6210' : '\u4EFB\u52A1\u5DF2\u5931\u8D25');

      /* Load reports if completed */
      if (task.status === 'completed') {
        API.get('/api/tasks/' + id + '/reports').then(function(reports) {
          if (reports && reports.length > 0) {
            var latest = null;
            for (var i = 0; i < reports.length; i++) {
              if (reports[i].is_latest) { latest = reports[i]; break; }
            }
            if (!latest) latest = reports[0];
            reportId = latest.report_id;
            qualityScore = latest.quality_score;
            showReportButton();
          }
        }).catch(function() {});
      }
      return;
    }

    connectSSE();
  }).catch(function(e) {
    if (titleEl) titleEl.textContent = '\u52A0\u8F7D\u5931\u8D25';
    addLog('error', '\u4EFB\u52A1\u4E0D\u5B58\u5728\u6216\u52A0\u8F7D\u5931\u8D25: ' + e.message, true);
  });
}

export function unmount() {
  if (_es) { _es.close(); _es = null; }
  if (_reconnectTimer) { clearTimeout(_reconnectTimer); _reconnectTimer = null; }
  _autoScroll = true;
}
