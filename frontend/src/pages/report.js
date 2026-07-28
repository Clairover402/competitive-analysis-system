/* report.js — Analysis Report Viewer */

import { API } from '../js/api.js';
import { SFX } from '../js/sounds.js';

var _taskId = '';
var _reportId = '';

export function render(params) {
  _taskId = params.taskId || '';
  _reportId = params.reportId || '';

  return '' +
  '<div class="pixel-flex pixel-flex-col" style="min-height:100vh">' +

    '<!-- Top Nav -->' +
    '<div class="pixel-flex pixel-items-center pixel-gap-md" style="height:56px; padding:0 20px; background:var(--pixel-dark); border-bottom:2px solid var(--pixel-mid); flex-shrink:0">' +
      '<button id="rpt-btn-back" class="pixel-btn-secondary">&larr; \u8FD4\u56DE\u4EFB\u52A1</button>' +
      '<span id="rpt-title" class="pixel-font-display pixel-text-green pixel-flex-1" style="font-size:15px; letter-spacing:2px">\u52A0\u8F7D\u4E2D...</span>' +
      '<span id="rpt-version" class="pixel-badge pixel-badge-info" style="display:none">v1</span>' +
    '</div>' +

    '<!-- Content -->' +
    '<main style="flex:1; padding:20px; overflow-y:auto; max-width:900px; margin:0 auto; width:100%">' +

      '<!-- Score Area -->' +
      '<div id="rpt-score-area" class="pixel-box pixel-mb-lg" style="display:none">' +
        '<div class="pixel-score-ring pixel-mb-md">' +
          '<span id="rpt-score-value" class="pixel-score-value">-</span>' +
          '<span class="pixel-score-outof">/100</span>' +
        '</div>' +
        '<div id="rpt-metrics"></div>' +
      '</div>' +

      '<!-- Report Body -->' +
      '<div id="rpt-body" class="pixel-box" style="display:none"></div>' +

      '<!-- Empty / Error -->' +
      '<div id="rpt-empty" class="pixel-box pixel-text-center" style="display:none">' +
        '<p style="font-size:32px; margin-bottom:12px">&#x1f4c4;</p>' +
        '<p class="pixel-font-display pixel-text-light" style="font-size:14px; margin-bottom:8px">\u6682\u65E0\u62A5\u544A</p>' +
        '<p class="pixel-font-mono pixel-text-light" style="font-size:14px; margin-bottom:16px">\u62A5\u544A\u6B63\u5728\u751F\u6210\u4E2D\uFF0C\u8BF7\u7A0D\u540E\u518D\u6765</p>' +
        '<button id="rpt-btn-empty-back" class="pixel-btn-secondary">&larr; \u8FD4\u56DE\u4EFB\u52A1\u8BE6\u60C5</button>' +
      '</div>' +

      '<div id="rpt-error" class="pixel-box pixel-text-center" style="display:none">' +
        '<p style="font-size:32px; margin-bottom:12px">&#x26A0;</p>' +
        '<p id="rpt-error-msg" class="pixel-font-mono pixel-msg-error" style="font-size:14px; margin-bottom:16px"></p>' +
        '<button id="rpt-btn-err-back" class="pixel-btn-secondary">&larr; \u8FD4\u56DE\u4EEA\u8868\u76D8</button>' +
      '</div>' +

    '</main>' +

    '<!-- Bottom Buttons -->' +
    '<div id="rpt-footer" class="pixel-flex pixel-justify-between pixel-items-center" style="padding:16px 20px; background:var(--pixel-dark); border-top:2px solid var(--pixel-mid); flex-shrink:0; display:none">' +
      '<button id="rpt-btn-dashboard" class="pixel-btn-secondary">\u25C0 \u4EEA\u8868\u76D8</button>' +
      '<button id="rpt-btn-latest" class="pixel-btn-secondary" style="display:none">\u25B8 \u6700\u65B0\u7248\u672C</button>' +
    '</div>' +

  '</div>';
}

export function mount(params) {
  var taskId = params.taskId || _taskId;
  var reportId = params.reportId || _reportId;

  var scoreArea = document.getElementById('rpt-score-area');
  var scoreValue = document.getElementById('rpt-score-value');
  var metricsArea = document.getElementById('rpt-metrics');
  var bodyArea = document.getElementById('rpt-body');
  var emptyArea = document.getElementById('rpt-empty');
  var errorArea = document.getElementById('rpt-error');
  var errorMsg = document.getElementById('rpt-error-msg');
  var titleEl = document.getElementById('rpt-title');
  var versionEl = document.getElementById('rpt-version');
  var footer = document.getElementById('rpt-footer');

  function showError(msg) {
    if (errorArea) errorArea.style.display = '';
    if (errorMsg) errorMsg.textContent = msg;
  }

  function showEmpty() {
    if (emptyArea) emptyArea.style.display = '';
  }

  /* Button events */
  var btnBack = document.getElementById('rpt-btn-back');
  var btnEmptyBack = document.getElementById('rpt-btn-empty-back');
  var btnErrBack = document.getElementById('rpt-btn-err-back');
  var btnDashboard = document.getElementById('rpt-btn-dashboard');
  var btnLatest = document.getElementById('rpt-btn-latest');

  if (btnBack) btnBack.addEventListener('click', function() {
    SFX.click();
    window.location.hash = '#/task/' + taskId;
  });
  if (btnEmptyBack) btnEmptyBack.addEventListener('click', function() {
    SFX.click();
    window.location.hash = '#/task/' + taskId;
  });
  if (btnErrBack) btnErrBack.addEventListener('click', function() {
    SFX.click();
    window.location.hash = '#/dashboard';
  });
  if (btnDashboard) btnDashboard.addEventListener('click', function() {
    SFX.click();
    window.location.hash = '#/dashboard';
  });
  if (btnLatest) btnLatest.addEventListener('click', function() {
    SFX.click();
    window.location.reload();
  });

  /* Load data */
  Promise.all([
    API.get('/api/tasks/' + taskId).catch(function() { return null; }),
    API.get('/api/tasks/' + taskId + '/reports').catch(function() { return null; }),
  ]).then(function(results) {
    var task = results[0];
    var reports = results[1];

    if (!task) { showError('\u4EFB\u52A1\u4E0D\u5B58\u5728'); return; }
    if (titleEl) titleEl.textContent = task.title || '\u672A\u547D\u540D\u4EFB\u52A1';

    if (!reports || reports.length === 0) { showEmpty(); return; }

    /* Select report version */
    var report = null;
    if (reportId) {
      for (var i = 0; i < reports.length; i++) {
        if (reports[i].report_id === reportId) { report = reports[i]; break; }
      }
    }
    if (!report) {
      for (var i = 0; i < reports.length; i++) {
        if (reports[i].is_latest) { report = reports[i]; break; }
      }
    }
    if (!report) report = reports[reports.length - 1];

    if (versionEl && reports.length > 1) {
      versionEl.style.display = '';
      versionEl.textContent = 'v' + (reports.indexOf(report) + 1);
    }

    /* Render score */
    renderScore(report);

    /* Render content */
    if (report.content) {
      renderContent(report.content);
    } else {
      if (bodyArea) { bodyArea.style.display = ''; bodyArea.innerHTML = '<p class="pixel-font-mono pixel-text-light" style="font-size:15px; padding:20px; text-align:center">\u62A5\u544A\u751F\u6210\u4E2D...</p>'; }
    }

    /* Footer */
    if (footer) footer.style.display = '';
    if (btnLatest && reports.length > 1) btnLatest.style.display = '';

  }).catch(function(e) {
    showError(e.message || '\u52A0\u8F7D\u5931\u8D25');
  });
}

function renderScore(report) {
  var scoreArea = document.getElementById('rpt-score-area');
  var scoreValue = document.getElementById('rpt-score-value');
  var metricsArea = document.getElementById('rpt-metrics');

  if (!scoreArea) return;
  scoreArea.style.display = '';

  var score = report.quality_score;
  if (score !== null && score !== undefined) {
    if (scoreValue) scoreValue.textContent = score;
  } else {
    if (scoreValue) scoreValue.textContent = '?';
  }

  var details = report.quality_details;
  if (!details || !metricsArea) return;

  var metrics = [
    { key: 'completeness',    label: '\u5B8C\u6574\u6027' },
    { key: 'accuracy',        label: '\u51C6\u786E\u6027' },
    { key: 'readability',     label: '\u53EF\u8BFB\u6027' },
    { key: 'professionalism', label: '\u4E13\u4E1A\u6027' },
  ];

  var html = '';
  for (var i = 0; i < metrics.length; i++) {
    var m = metrics[i];
    var val = details[m.key];
    if (val === undefined || val === null) continue;
    html += '' +
    '<div class="pixel-score-metric">' +
      '<div class="pixel-score-label">' +
        '<span>' + m.label + '</span>' +
        '<span class="pixel-text-green">' + val + '</span>' +
      '</div>' +
      '<div class="pixel-score-bar">' +
        '<div class="pixel-score-fill" style="width:' + val + '%"></div>' +
      '</div>' +
    '</div>';
  }
  metricsArea.innerHTML = html;
}

function renderContent(md) {
  var bodyArea = document.getElementById('rpt-body');
  if (!bodyArea) return;
  bodyArea.style.display = '';
  bodyArea.innerHTML = parseMarkdown(md);
}

/* Markdown -> HTML parser */

function parseMarkdown(md) {
  var bt = String.fromCharCode(96);
  var triple = bt + bt + bt;
  var lines = md.split('\n');
  var html = '';
  var inCodeBlock = false;
  var codeContent = '';
  var inTable = false;
  var tableHtml = '';
  var inList = false;

  for (var i = 0; i < lines.length; i++) {
    var line = lines[i];

    /* Code block */
    if (line.trim().substring(0, 3) === triple) {
      if (inCodeBlock) {
        html += '<pre class="pixel-report-block">' + escMd(codeContent) + '</pre>';
        codeContent = '';
        inCodeBlock = false;
      } else {
        inCodeBlock = true;
      }
      continue;
    }

    if (inCodeBlock) {
      codeContent += (codeContent ? '\n' : '') + line;
      continue;
    }

    /* Table */
    var isTableLine = line.trim().indexOf('|') === 0;
    if (isTableLine && !inTable) {
      inTable = true;
      tableHtml = '<table class="pixel-report-table">';
    }

    if (inTable) {
      if (isTableLine) {
        if (/^\|?[\s:-]+\|[\s|:-]+$/.test(line.trim())) continue;
        var cells = line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|');
        var tag = tableHtml.indexOf('<tbody>') >= 0 ? 'td' : 'th';
        if (tag === 'th' && tableHtml.indexOf('<thead>') < 0) tableHtml += '<thead>';
        if (tag === 'td' && tableHtml.indexOf('<tbody>') < 0) {
          if (tableHtml.indexOf('<thead>') >= 0) tableHtml += '</thead>';
          tableHtml += '<tbody>';
        }
        tableHtml += '<tr>';
        for (var c = 0; c < cells.length; c++) {
          tableHtml += '<' + tag + '>' + parseInline(cells[c].trim()) + '</' + tag + '>';
        }
        tableHtml += '</tr>';
        continue;
      } else {
        if (tableHtml.indexOf('<tbody>') >= 0) tableHtml += '</tbody>';
        tableHtml += '</table>';
        html += tableHtml;
        tableHtml = '';
        inTable = false;
      }
    }

    /* List */
    var listMatch = line.match(/^(\s*)([-*])\s+(.+)$/);
    if (listMatch) {
      if (!inList) { html += '<ul class="pixel-report-list">'; inList = true; }
      html += '<li>' + parseInline(listMatch[3]) + '</li>';
      continue;
    } else if (inList) {
      html += '</ul>';
      inList = false;
    }

    /* Headings */
    if (line.substring(0, 4) === '### ') {
      html += '<h3 class="pixel-report-h3">' + parseInline(line.substring(4)) + '</h3>';
      continue;
    }
    if (line.substring(0, 3) === '## ') {
      html += '<h2 class="pixel-report-h2">' + parseInline(line.substring(3)) + '</h2>';
      continue;
    }
    if (line.substring(0, 2) === '# ') {
      html += '<h1 class="pixel-report-h1">' + parseInline(line.substring(2)) + '</h1>';
      continue;
    }

    /* Empty line */
    if (line.trim() === '') { html += '<br>'; continue; }

    /* Paragraph */
    html += '<p class="pixel-report-p">' + parseInline(line) + '</p>';
  }

  if (inList) html += '</ul>';
  if (inCodeBlock) html += '<pre class="pixel-report-block">' + escMd(codeContent) + '</pre>';

  return html;
}

function parseInline(text) {
  text = text.replace(/\*\*(.+?)\*\*/g, '<strong style="color:var(--pixel-cream)">$1</strong>');
  text = text.replace(/\*(.+?)\*/g, '<em style="color:var(--pixel-cyan)">$1</em>');
  var bt = String.fromCharCode(96);
  var re = new RegExp(bt + '(.+?)' + bt, 'g');
  text = text.replace(re, '<code class="pixel-report-code">$1</code>');
  return text;
}

function escMd(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

export function unmount() {}