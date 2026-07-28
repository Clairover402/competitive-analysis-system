/* monitor.js — Real-time System Monitor */

import { SFX } from '../js/sounds.js';

var _timerHealth = null;
var _timerMetrics = null;
var _lastMetrics = null;

var AGENT_COLORS = {
  collector:  'var(--pixel-yellow)',
  analyzer:   'var(--pixel-cyan)',
  writer:     'var(--pixel-green)',
  quality:    'var(--pixel-purple)',
  supervisor: 'var(--pixel-amber)',
};

var BLOCK_DESC = {
  whitelist:        '\u5DE5\u5177\u767D\u540D\u5355\u62E6\u622A',
  param_validation: '\u53C2\u6570\u6821\u9A8C\u4E0D\u901A\u8FC7',
  rate_limit:       'Agent \u7EA7\u9891\u63A7\u89E6\u53D1',
  pii:              '\u654F\u611F\u4FE1\u606F\u6CC4\u9732\u62E6\u622A',
};

export function render() {
  return '' +
  '<div class="pixel-flex pixel-flex-col" style="min-height:100vh">' +

    '<!-- Top Nav -->' +
    '<div class="pixel-flex pixel-items-center pixel-gap-md" style="height:56px; padding:0 20px; background:var(--pixel-dark); border-bottom:2px solid var(--pixel-mid); flex-shrink:0">' +
      '<button id="mon-btn-back" class="pixel-btn-secondary">&larr; \u4EEA\u8868\u76D8</button>' +
      '<span class="pixel-font-display pixel-text-green pixel-flex-1" style="font-size:15px; letter-spacing:2px">\u76D1\u63A7\u9762\u677F</span>' +
      '<span id="mon-sys-status" class="pixel-font-display" style="font-size:12px; letter-spacing:1px">\u52A0\u8F7D\u4E2D...</span>' +
    '</div>' +

    '<!-- Content -->' +
    '<main style="flex:1; padding:20px; overflow-y:auto">' +

      '<!-- Row 1: Status Cards -->' +
      '<div class="pixel-flex pixel-gap-md pixel-mb-md">' +
        renderStatusCard('system', '\u7CFB\u7EDF\u72B6\u6001', '<span class="pixel-spinner"></span>') +
        renderStatusCard('db', '\u6570\u636E\u5E93\u8FDE\u63A5', '<span class="pixel-spinner"></span>') +
        renderStatusCard('llm', 'LLM \u9650\u6D41', '<span class="pixel-spinner"></span>') +
      '</div>' +

      '<!-- Row 2: Metric Cards -->' +
      '<div class="pixel-flex pixel-gap-md pixel-mb-md">' +
        '<div class="pixel-box pixel-flex-1" id="mon-tasks">' +
          '<p class="pixel-card-title">\u4EFB\u52A1\u541E\u5410</p>' +
          '<div id="mon-tasks-body"><span class="pixel-spinner"></span></div>' +
        '</div>' +
        '<div class="pixel-box pixel-flex-1" id="mon-agents">' +
          '<p class="pixel-card-title">Agent \u8C03\u7528\u5206\u5E03</p>' +
          '<div id="mon-agents-body"><span class="pixel-spinner"></span></div>' +
        '</div>' +
      '</div>' +

      '<!-- Row 3: Harness Blocks -->' +
      '<div class="pixel-box pixel-mb-md" id="mon-blocks">' +
        '<p class="pixel-card-title">HARNESS BLOCKS</p>' +
        '<div id="mon-blocks-body"><span class="pixel-spinner"></span></div>' +
      '</div>' +

    '</main>' +

    '<!-- Bottom Bar -->' +
    '<div class="pixel-flex pixel-items-center pixel-justify-between" style="padding:10px 20px; background:var(--pixel-dark); border-top:2px solid var(--pixel-mid); flex-shrink:0">' +
      '<span class="pixel-font-mono pixel-text-light" style="font-size:13px">[ AUTO REFRESH \u00B7 10s ]</span>' +
      '<span id="mon-last-update" class="pixel-font-mono pixel-text-light" style="font-size:13px">[ LAST: --:--:-- ]</span>' +
    '</div>' +

  '</div>';
}

function renderStatusCard(id, title, body) {
  return '' +
  '<div class="pixel-box pixel-flex-1" id="mon-' + id + '">' +
    '<p class="pixel-card-title">' + title + '</p>' +
    '<div id="mon-' + id + '-body" style="font-family:var(--pixel-font); font-size:18px; letter-spacing:2px">' + body + '</div>' +
  '</div>';
}

export function mount() {
  var btnBack = document.getElementById('mon-btn-back');
  if (btnBack) btnBack.addEventListener('click', function() {
    SFX.click();
    window.location.hash = '#/dashboard';
  });

  /* Initial fetch */
  fetchHealth();
  fetchMetrics();

  /* Polling */
  _timerHealth = setInterval(function() { SFX.blip(); fetchHealth(); }, 10000);
  _timerMetrics = setInterval(function() { fetchMetrics(); }, 10000);
}

/* —— Health —— */
function fetchHealth() {
  fetch('/health')
    .then(function(r) { return r.ok ? r.json() : Promise.reject(r); })
    .then(function(data) {
      updateSystemStatus(data.status);
      updateDbStatus(data.db_connected);
    })
    .catch(function() {
      updateSystemStatus('unreachable');
      updateDbStatus(null);
    });
}

function updateSystemStatus(status) {
  var el = document.getElementById('mon-sys-status');
  var body = document.getElementById('mon-system-body');
  if (!el || !body) return;

  if (status === 'ok') {
    el.innerHTML = '\uD83D\uDFE2 HEALTHY';
    el.style.color = 'var(--pixel-green)';
    body.innerHTML = '<span style="color:var(--pixel-green)">\u2705 OK</span>';
  } else if (status === 'degraded') {
    el.innerHTML = '\uD83D\uDFE1 DEGRADED';
    el.style.color = 'var(--pixel-yellow)';
    body.innerHTML = '<span style="color:var(--pixel-yellow)">\u26A0 DEGRADED</span>';
  } else {
    el.innerHTML = '\uD83D\uDD34 DOWN';
    el.style.color = 'var(--pixel-red)';
    body.innerHTML = '<span style="color:var(--pixel-red)">UNREACHABLE</span>';
  }
}

function updateDbStatus(connected) {
  var body = document.getElementById('mon-db-body');
  if (!body) return;

  if (connected === true) {
    body.innerHTML = '<span style="color:var(--pixel-green)">\u2705 CONNECTED</span>';
  } else if (connected === false) {
    body.innerHTML = '<span style="color:var(--pixel-red)">\u274C DISCONNECTED</span>';
  } else {
    body.innerHTML = '<span style="color:var(--pixel-light)">UNKNOWN</span>';
  }
}

/* —— Metrics —— */
function fetchMetrics() {
  fetch('/metrics')
    .then(function(r) { return r.ok ? r.text() : Promise.reject(r); })
    .then(function(text) {
      var data = parsePrometheus(text);
      _lastMetrics = data;
      updateLlmCard(data);
      updateTasksCard(data);
      updateAgentsCard(data);
      updateBlocksCard(data);
      updateLastTime();
    })
    .catch(function() {
      var lastEl = document.getElementById('mon-last-update');
      if (lastEl) lastEl.innerHTML = '<span style="color:var(--pixel-red)">[ FETCH \u00B7 FAILED ]</span>';
      if (_lastMetrics) {
        /* Keep showing last successful data */
      }
    });
}

function updateLastTime() {
  var el = document.getElementById('mon-last-update');
  if (!el) return;
  var d = new Date();
  var t = ('0'+d.getHours()).slice(-2) + ':' + ('0'+d.getMinutes()).slice(-2) + ':' + ('0'+d.getSeconds()).slice(-2);
  el.textContent = '[ LAST: ' + t + ' ]';
  el.style.color = 'var(--pixel-light)';
}

/* —— Helper: extract a single label value from a Prometheus label set —— */
function _extractLabel(line, labelName) {
  var re = new RegExp('\\b' + labelName + '="([^"]*)"');
  var m = line.match(re);
  return m ? m[1] : null;
}

/* —— Prometheus Text Parser ——
 *
 * 关键设计 — 通用标签匹配：
 * 后端指标含多个标签，如 ca_agent_calls_total{agent="collector",action="..",status=".."}
 * 解析器不用写死完整标签列表 → 用 \{[^}]*\} 通配所有标签，再用 _extractLabel 提取关心的。
 */
function parsePrometheus(text) {
  var result = { tasks: {}, agents: {}, rateLimit: 0, blocks: {} };
  var lines = text.split('\n');

  for (var i = 0; i < lines.length; i++) {
    var line = lines[i].trim();
    if (!line || line[0] === '#') continue;

    /* ca_tasks_total{status="pending"} 5.0  (single label, exact match is fine) */
    var taskMatch = line.match(/^ca_tasks_total\{status="(\w+)"\}\s+([\d.]+)/);
    if (taskMatch) {
      result.tasks[taskMatch[1]] = parseFloat(taskMatch[2]);
      continue;
    }

    /* ca_agent_calls_total{agent="collector",action="web_search",status="success"} 12.0
     *   → 按 agent 聚合（同一 agent 的多条行求和） */
    var agentMatch = line.match(/^ca_agent_calls_total\{([^}]*)\}\s+([\d.]+)/);
    if (agentMatch) {
      var agent = _extractLabel(line, 'agent');
      if (agent) {
        result.agents[agent] = (result.agents[agent] || 0) + parseFloat(agentMatch[2]);
      }
      continue;
    }

    /* ca_rate_limit_hits{layer="llm",agent="analyzer"} 1.0
     *   → 各 layer 求和 */
    var rlMatch = line.match(/^ca_rate_limit_hits\{([^}]*)\}\s+([\d.]+)/);
    if (rlMatch) {
      result.rateLimit += parseFloat(rlMatch[2]);
      continue;
    }

    /* ca_harness_blocks{check_type="whitelist"} 5.0  (single label) */
    var blockMatch = line.match(/^ca_harness_blocks\{check_type="(\w+)"\}\s+([\d.]+)/);
    if (blockMatch) {
      result.blocks[blockMatch[1]] = parseFloat(blockMatch[2]);
      continue;
    }
  }

  return result;
}

/* —— LLM Rate Limit Card —— */
function updateLlmCard(data) {
  var body = document.getElementById('mon-llm-body');
  if (!body) return;
  var hits = data.rateLimit || 0;
  var color = hits > 0 ? 'var(--pixel-red)' : 'var(--pixel-green)';
  body.innerHTML = '' +
    '<div><span style="font-size:24px; color:' + color + '">' + hits + '</span>' +
    '<span style="font-size:13px; color:var(--pixel-light); margin-left:6px">\u6B21\u89E6\u53D1</span></div>' +
    '<div style="font-family:var(--pixel-mono); font-size:14px; color:var(--pixel-light); margin-top:6px">60 RPM \u9650\u6D41</div>';
}

/* —— Task Throughput Card —— */
function updateTasksCard(data) {
  var body = document.getElementById('mon-tasks-body');
  if (!body) return;
  var tasks = data.tasks;
  var statuses = [
    { key: 'pending',   label: '\u7B49\u5F85\u4E2D', color: 'var(--pixel-light)' },
    { key: 'running',   label: '\u8FD0\u884C\u4E2D', color: 'var(--pixel-yellow)' },
    { key: 'completed', label: '\u5DF2\u5B8C\u6210', color: 'var(--pixel-green)' },
    { key: 'failed',    label: '\u5931\u8D25',     color: 'var(--pixel-red)' },
  ];

  var max = 0;
  statuses.forEach(function(s) { var v = (tasks[s.key] || 0); if (v > max) max = v; });
  if (max === 0) max = 1;

  var html = '';
  statuses.forEach(function(s) {
    var val = tasks[s.key] || 0;
    var pct = Math.round((val / max) * 100);
    html += '' +
    '<div class="pixel-flex pixel-items-center pixel-gap-sm" style="margin-bottom:8px">' +
      '<span class="pixel-font-mono" style="font-size:13px; width:60px; color:' + s.color + '">' + s.label + '</span>' +
      '<span class="pixel-font-mono" style="font-size:14px; width:36px; text-align:right; color:var(--pixel-white)">' + val + '</span>' +
      '<div class="pixel-flex-1" style="height:10px; background:var(--pixel-black); box-shadow:0 0 0 2px var(--pixel-mid); overflow:hidden">' +
        '<div style="height:100%; width:' + pct + '%; background:' + s.color + '; transition:width 0.4s ease"></div>' +
      '</div>' +
    '</div>';
  });
  body.innerHTML = html;
}

/* —— Agent Call Distribution Card —— */
function updateAgentsCard(data) {
  var body = document.getElementById('mon-agents-body');
  if (!body) return;
  var agents = data.agents;
  var agentList = ['collector', 'analyzer', 'writer', 'quality', 'supervisor'];

  var max = 0;
  agentList.forEach(function(a) { var v = agents[a] || 0; if (v > max) max = v; });
  if (max === 0) max = 1;

  var html = '';
  agentList.forEach(function(a) {
    var val = agents[a] || 0;
    var pct = Math.round((val / max) * 100);
    var color = AGENT_COLORS[a] || 'var(--pixel-light)';
    html += '' +
    '<div class="pixel-flex pixel-items-center pixel-gap-sm" style="margin-bottom:8px">' +
      '<span class="pixel-font-display" style="font-size:10px; width:80px; letter-spacing:1px; color:' + color + '">' + a.toUpperCase() + '</span>' +
      '<span class="pixel-font-mono" style="font-size:14px; width:36px; text-align:right; color:var(--pixel-white)">' + val + '</span>' +
      '<div class="pixel-flex-1" style="height:10px; background:var(--pixel-black); box-shadow:0 0 0 2px var(--pixel-mid); overflow:hidden">' +
        '<div style="height:100%; width:' + pct + '%; background:' + color + '; transition:width 0.4s ease"></div>' +
      '</div>' +
    '</div>';
  });
  body.innerHTML = html;
}

/* —— Harness Blocks Card —— */
function updateBlocksCard(data) {
  var body = document.getElementById('mon-blocks-body');
  if (!body) return;
  var blocks = data.blocks;
  var types = ['whitelist', 'param_validation', 'rate_limit', 'pii'];

  var html = '';
  types.forEach(function(t) {
    var count = blocks[t] || 0;
    var statusIcon = count > 0
      ? '<span style="color:var(--pixel-green)">[NOW]</span>'
      : '<span style="color:var(--pixel-light)">[ - ]</span>';
    var desc = BLOCK_DESC[t] || t;

    html += '' +
    '<div class="pixel-flex pixel-items-center pixel-gap-sm" style="margin-bottom:6px">' +
      '<span style="font-family:var(--pixel-font); font-size:10px; min-width:50px">' + statusIcon + '</span>' +
      '<span style="font-family:var(--pixel-font); font-size:10px; color:var(--pixel-green); letter-spacing:1px; width:130px">' + t.toUpperCase() + '</span>' +
      '<span style="font-family:var(--pixel-mono); font-size:15px; color:var(--pixel-white); width:50px; text-align:right">' + count + ' \u6B21</span>' +
      '<span style="font-family:var(--pixel-mono); font-size:13px; color:var(--pixel-light)">&gt; ' + desc + '</span>' +
    '</div>';
  });
  body.innerHTML = html;
}

export function unmount() {
  if (_timerHealth) { clearInterval(_timerHealth); _timerHealth = null; }
  if (_timerMetrics) { clearInterval(_timerMetrics); _timerMetrics = null; }
}
