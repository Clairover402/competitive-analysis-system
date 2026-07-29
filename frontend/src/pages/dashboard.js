/* dashboard.js — Pixel CRT Terminal Dashboard */

import { Auth } from '../js/auth.js';
import { API } from '../js/api.js';
import { SFX } from '../js/sounds.js';

var mode = 'quick';

export function render() {
  var username = Auth.getUsername();

  return '' +
  '<div class="pixel-flex" style="min-height:100vh">' +

    '<nav class="pixel-sidebar">' +

      '<div class="pixel-sidebar-brand">' +
        '<span class="pixel-sidebar-brand-icon">&#x1f50d;</span>' +
        '<span class="pixel-sidebar-brand-text">\u7ADE\u54C1\u5206\u6790</span>' +
      '</div>' +

      '<div class="pixel-divider"></div>' +

      '<div class="pixel-sidebar-nav">' +
        '<button class="pixel-nav-item active" id="nav-dashboard">' +
          '<span class="pixel-nav-cursor"></span> \u4EEA\u8868\u76D8' +
        '</button>' +
        '<button class="pixel-nav-item" id="nav-monitor" ' +
                'onclick="window.location.hash=' + "'#/monitor'" + '">' +
          '<span class="pixel-nav-cursor"></span> \u76D1\u63A7\u9762\u677F' +
        '</button>' +
      '</div>' +

      '<div class="pixel-sidebar-footer">' +
        '<div class="pixel-sidebar-user">' +
          '<span>&#x1f464;</span> <span id="sidebar-user">' + username + '</span>' +
        '</div>' +
        '<button id="btn-logout" class="pixel-btn-secondary pixel-w-full">\u9000\u51FA\u767B\u5F55</button>' +
      '</div>' +

    '</nav>' +

    '<main style="flex:1; padding:28px; overflow-y:auto">' +

      '<div class="pixel-box pixel-mb-lg">' +

        '<div class="pixel-tabs">' +
          '<button id="tab-quick" class="pixel-tab active">\u5FEB\u901F\u6A21\u5F0F</button>' +
          '<button id="tab-advanced" class="pixel-tab">\u9AD8\u7EA7\u6A21\u5F0F</button>' +
        '</div>' +

        '<div id="panel-quick">' +
          '<textarea id="quick-query" class="pixel-textarea pixel-mb-md" rows="4"' +
                    ' placeholder="> 例如：对比王者荣耀和英雄联盟在玩法设计、美术风格、电竞生态、商业化方面的表现"></textarea>' +
          '<button id="btn-submit-quick" class="pixel-btn">' +
            '&blacktriangleright; \u5F00\u59CB\u5206\u6790' +
          '</button>' +
        '</div>' +

        '<div id="panel-advanced" class="pixel-hidden">' +
          '<label class="pixel-input-label" for="adv-title">[ \u6807\u9898 ]</label>' +
          '<input id="adv-title" class="pixel-input pixel-mb-md" ' +
                 ' placeholder="> 例如：MOBA 头部游戏竞品分析" maxlength="200">' +
          '<label class="pixel-input-label" for="adv-competitors">[ \u7ADE\u54C1 ]</label>' +
          '<input id="adv-competitors" class="pixel-input pixel-mb-md" ' +
                 ' placeholder="> 例如：王者荣耀, 英雄联盟, 英雄联盟手游, 决战平安京" maxlength="2000">' +
          '<label class="pixel-input-label" for="adv-dimensions">[ \u7EF4\u5EA6 ]</label>' +
          '<input id="adv-dimensions" class="pixel-input pixel-mb-md" ' +
                 ' placeholder="> 例如：玩法设计, 美术风格, 电竞生态, 商业化, 社交系统" maxlength="2000">' +
          '<button id="btn-submit-adv" class="pixel-btn">' +
            '&blacktriangleright; \u5F00\u59CB\u5206\u6790' +
          '</button>' +
        '</div>' +

        '<div id="submit-msg" class="pixel-msg pixel-mt-sm" style="min-height:20px"></div>' +

      '</div>' +

      '<div class="pixel-box">' +
        '<div class="pixel-flex pixel-items-center pixel-justify-between pixel-mb-md">' +
          '<span class="pixel-font-mono pixel-text-light" style="font-size:16px">&blacktriangleright; \u4EFB\u52A1\u961F\u5217</span>' +
          '<button id="btn-refresh-tasks" class="pixel-btn-secondary">\u5237\u65B0</button>' +
        '</div>' +
        '<div id="task-list" class="pixel-flex pixel-flex-col pixel-gap-sm" style="min-height:120px">' +
          '<div class="pixel-text-center pixel-text-light" style="padding:40px 0; font-size:16px">' +
            '<span class="pixel-spinner" style="margin-right:8px; vertical-align:middle"></span>\u52A0\u8F7D\u4E2D...' +
          '</div>' +
        '</div>' +
      '</div>' +

    '</main>' +

  '</div>';
}

export function mount() {
  var tabQuick = document.getElementById('tab-quick');
  var tabAdvanced = document.getElementById('tab-advanced');
  var panelQuick = document.getElementById('panel-quick');
  var panelAdvanced = document.getElementById('panel-advanced');
  var msgEl = document.getElementById('submit-msg');
  var quickQuery = document.getElementById('quick-query');
  var advTitle = document.getElementById('adv-title');
  var advCompetitors = document.getElementById('adv-competitors');
  var advDimensions = document.getElementById('adv-dimensions');
  var btnSubmitQuick = document.getElementById('btn-submit-quick');
  var btnSubmitAdv = document.getElementById('btn-submit-adv');
  var btnRefresh = document.getElementById('btn-refresh-tasks');
  var btnLogout = document.getElementById('btn-logout');

  function showMsg(text, type) {
    if (!msgEl) return;
    msgEl.textContent = text;
    msgEl.className = 'pixel-msg pixel-mt-sm pixel-animate-fade-in';
    if (type === 'error') msgEl.classList.add('pixel-msg-error');
    else if (type === 'success') msgEl.classList.add('pixel-msg-success');
    else msgEl.classList.add('pixel-msg-info');

    if (type === 'error') {
      setTimeout(function() {
        if (msgEl.textContent === text) {
          msgEl.style.opacity = '0';
          setTimeout(function() { msgEl.textContent = ''; msgEl.style.opacity = '1'; }, 200);
        }
      }, 5000);
    }
  }

  if (tabQuick && tabAdvanced && panelQuick && panelAdvanced) {
    tabQuick.addEventListener('click', function() {
      mode = 'quick';
      tabQuick.classList.add('active');
      tabAdvanced.classList.remove('active');
      panelQuick.classList.remove('pixel-hidden');
      panelAdvanced.classList.add('pixel-hidden');
      SFX.blip();
      quickQuery.focus();
    });

    tabAdvanced.addEventListener('click', function() {
      mode = 'advanced';
      tabAdvanced.classList.add('active');
      tabQuick.classList.remove('active');
      panelAdvanced.classList.remove('pixel-hidden');
      panelQuick.classList.add('pixel-hidden');
      SFX.blip();
    });
  }

  async function submitQuick() {
    var query = quickQuery.value.trim();
    if (!query) {
      showMsg('> \u8BF7\u8F93\u5165\u5206\u6790\u5185\u5BB9', 'error');
      SFX.error();
      return;
    }

    btnSubmitQuick.disabled = true;
    btnSubmitQuick.textContent = '\u5904\u7406\u4E2D...';

    try {
      var data = await API.post('/api/tasks', { title: query.substring(0, 80), query: query });
      showMsg('> \u4EFB\u52A1\u5DF2\u521B\u5EFA :: ' + data.task_id, 'success');
      SFX.confirm();
      setTimeout(function() { window.location.hash = '#/task/' + data.task_id; }, 600);
    } catch (e) {
      showMsg('> \u9519\u8BEF: ' + e.message, 'error');
      SFX.error();
      btnSubmitQuick.disabled = false;
      btnSubmitQuick.textContent = '\u25B6 \u5F00\u59CB\u5206\u6790';
    }
  }

  async function submitAdvanced() {
    var title = advTitle.value.trim();
    var competitors = advCompetitors.value.trim();
    var dimensions = advDimensions.value.trim();

    if (!title && !competitors) {
      showMsg('> \u6807\u9898\u6216\u7ADE\u54C1\u81F3\u5C11\u586B\u5199\u4E00\u9879', 'error');
      SFX.error();
      return;
    }

    btnSubmitAdv.disabled = true;
    btnSubmitAdv.textContent = '\u5904\u7406\u4E2D...';

    try {
      var body = { title: title || '\u672A\u547D\u540D' };
      if (competitors) body.competitors = competitors.split(/[,，]/).map(function(s) { return s.trim(); }).filter(Boolean);
      if (dimensions) body.dimensions = dimensions.split(/[,，]/).map(function(s) { return s.trim(); }).filter(Boolean);
      var data = await API.post('/api/tasks', body);
      showMsg('> \u4EFB\u52A1\u5DF2\u521B\u5EFA :: ' + data.task_id, 'success');
      SFX.confirm();
      setTimeout(function() { window.location.hash = '#/task/' + data.task_id; }, 600);
    } catch (e) {
      showMsg('> \u9519\u8BEF: ' + e.message, 'error');
      SFX.error();
      btnSubmitAdv.disabled = false;
      btnSubmitAdv.textContent = '\u25B6 \u5F00\u59CB\u5206\u6790';
    }
  }

  if (quickQuery) {
    quickQuery.addEventListener('keydown', function(e) {
      if (e.key === 'Enter' && e.ctrlKey) {
        e.preventDefault();
        submitQuick();
      }
    });
  }
  if (advDimensions) {
    advDimensions.addEventListener('keydown', function(e) {
      if (e.key === 'Enter') submitAdvanced();
    });
  }

  if (btnSubmitQuick) btnSubmitQuick.addEventListener('click', submitQuick);
  if (btnSubmitAdv) btnSubmitAdv.addEventListener('click', submitAdvanced);
  if (btnRefresh) btnRefresh.addEventListener('click', function() { SFX.click(); loadTasks(); });
  if (btnLogout) btnLogout.addEventListener('click', function() { SFX.click(); Auth.logout(); });

  if (quickQuery) quickQuery.focus();

  loadTasks();
}

var STATUS_MAP = {
  'pending':     { label: '\u7B49\u5F85\u4E2D',   cls: 'pixel-badge-warning' },
  'collecting':  { label: '\u91C7\u96C6\u4E2D',   cls: 'pixel-badge-warning' },
  'analyzing':   { label: '\u5206\u6790\u4E2D',   cls: 'pixel-badge-warning' },
  'writing':     { label: '\u64B0\u5199\u4E2D',   cls: 'pixel-badge-warning' },
  'qa':          { label: '\u8D28\u68C0\u4E2D',   cls: 'pixel-badge-warning' },
  'completed':   { label: '\u5DF2\u5B8C\u6210',   cls: 'pixel-badge-success' },
  'failed':      { label: '\u5931\u8D25',         cls: 'pixel-badge-danger'  },
};

async function loadTasks() {
  var el = document.getElementById('task-list');
  if (!el) return;

  try {
    var taskIds = getLocalTaskIds();

    if (taskIds.length === 0) {
      el.innerHTML = renderEmptyState();
      return;
    }

    var recent = taskIds.slice(-20).reverse();
    var tasks = [];
    for (var i = 0; i < recent.length; i++) {
      try {
        var t = await API.get('/api/tasks/' + recent[i]);
        tasks.push(t);
      } catch(e) {}
    }

    if (tasks.length === 0) {
      el.innerHTML = renderEmptyState();
      return;
    }

    el.innerHTML = '<div class="pixel-flex pixel-flex-wrap pixel-gap-md">' +
      tasks.map(function(t) { return renderTaskCard(t); }).join('') +
      '</div>';

  } catch(e) {
    el.innerHTML = renderEmptyState();
  }
}

function getLocalTaskIds() {
  try {
    return JSON.parse(localStorage.getItem('_task_ids') || '[]');
  } catch(e) {
    return [];
  }
}

function addLocalTaskId(taskId) {
  var ids = getLocalTaskIds();
  ids.push(taskId);
  ids = ids.filter(function(v, i, a) { return a.indexOf(v) === i; }).slice(-100);
  localStorage.setItem('_task_ids', JSON.stringify(ids));
}

function renderEmptyState() {
  return '<div class="pixel-text-center pixel-text-light" style="padding:40px 0; font-size:16px">' +
    '<p style="font-size:24px; margin-bottom:8px">&#x1f4ad;</p>' +
    '<p>\u6682\u65E0\u5206\u6790\u4EFB\u52A1</p>' +
    '<p style="font-size:14px; margin-top:6px">\u5728\u4E0A\u65B9\u521B\u5EFA\u7B2C\u4E00\u4E2A\u5206\u6790\u5427</p>' +
    '</div>';
}

function renderTaskCard(t) {
  var s = STATUS_MAP[t.status] || { label: t.status || '\u672A\u77E5', cls: 'pixel-badge-info' };
  var title = t.title || '\u672A\u547D\u540D';
  var time = t.created_at
    ? new Date(t.created_at).toLocaleString('zh-CN', { month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit' })
    : '-';
  var competitorCount = t.competitors ? t.competitors.length : 0;

  return '' +
  '<div class="pixel-card pixel-animate-fade-in" style="width:calc(33.333% - 12px); min-width:200px"' +
       ' onclick="window.location.hash=' + "'#/task/" + t.task_id + "'" + '">' +
    '<div class="pixel-flex pixel-items-center pixel-gap-sm pixel-mb-sm">' +
      '<span class="pixel-badge ' + s.cls + '">' + s.label + '</span>' +
    '</div>' +
    '<p class="pixel-text-white pixel-truncate pixel-mb-xs pixel-font-mono" style="font-size:15px">' +
      title +
    '</p>' +
    '<p class="pixel-text-light" style="font-size:14px">' + time + '</p>' +
    (competitorCount > 0
      ? '<p class="pixel-text-light pixel-mt-xs" style="font-size:14px">' + competitorCount + ' \u4E2A\u7ADE\u54C1</p>'
      : '') +
  '</div>';
}

/* API.post 拦截 — 自动记录 task_id 到 localStorage */
var _origPost = API.post;
API.post = async function(path, body) {
  var result = await _origPost.call(API, path, body);
  if (path === '/api/tasks' && result.task_id) {
    addLocalTaskId(result.task_id);
  }
  return result;
};

export function unmount() {}
