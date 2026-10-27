/* dashboard.js — Pixel CRT Terminal Dashboard */

import { Auth } from '../js/auth.js';
import { API } from '../js/api.js';
import { SFX } from '../js/sounds.js';

// 两种模式 = 后端两条执行路径，命名必须跟着架构走，别让 UI 词比代码更"先进"：
//   标准模式（默认）：用户自己给竞品与维度 → 参数齐全 → Pipeline 确定性分析
//   高级模式        ：一句话交给 LLM 抽实体 → 常缺参数 → Supervisor 探索补齐
// 不要把"一句话"叫快速模式：它更省事，但要跑 ReAct 探索，更慢、更贵、更依赖模型推断。
// 把更慢更不确定的那条叫"高级"，也比叫"快速"诚实。
var mode = 'standard';

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
          '<button id="tab-standard" class="pixel-tab active">\u6807\u51C6\u6A21\u5F0F</button>' +
          '<button id="tab-advanced" class="pixel-tab">\u9AD8\u7EA7\u6A21\u5F0F</button>' +
        '</div>' +

        // 默认入口：结构化入参。参数齐全直接进 Pipeline，快、稳、可复现。
        '<div id="panel-standard">' +
          '<p class="pixel-mode-hint">\u81EA\u5DF1\u6307\u5B9A\u7ADE\u54C1\u4E0E\u7EF4\u5EA6 \u00B7 \u4E24\u9879\u9F50\u5168\u5373\u8D70\u786E\u5B9A\u6027\u5206\u6790\uFF08Pipeline\uFF09\uFF0C\u7ED3\u679C\u53EF\u590D\u73B0\u3001\u8017\u65F6\u53EF\u9884\u671F</p>' +
          '<label class="pixel-input-label" for="std-title">[ \u6807\u9898 ]</label>' +
          '<input id="std-title" class="pixel-input pixel-mb-md" ' +
                 ' placeholder="> \u4F8B\u5982\uFF1AMOBA \u5934\u90E8\u6E38\u620F\u7ADE\u54C1\u5206\u6790" maxlength="200">' +
          '<label class="pixel-input-label" for="std-competitors">[ \u7ADE\u54C1 \u00B7 \u6700\u591A 4 \u4E2A ]</label>' +
          '<input id="std-competitors" class="pixel-input pixel-mb-md" ' +
                 ' placeholder="> \u4F8B\u5982\uFF1A\u738B\u8005\u8363\u8000, \u82F1\u96C4\u8054\u76DF, \u82F1\u96C4\u8054\u76DF\u624B\u6E38, \u51B3\u6218\u5E73\u5B89\u4EAC\uFF08\u6700\u591A 4 \u4E2A\uFF09" maxlength="2000">' +
          '<label class="pixel-input-label" for="std-dimensions">[ \u7EF4\u5EA6 \u00B7 \u6700\u591A 4 \u4E2A ]</label>' +
          '<input id="std-dimensions" class="pixel-input pixel-mb-md" ' +
                 ' placeholder="> \u4F8B\u5982\uFF1A\u73A9\u6CD5\u8BBE\u8BA1, \u7F8E\u672F\u98CE\u683C, \u7535\u7ADE\u751F\u6001, \u5546\u4E1A\u5316\uFF08\u6700\u591A 4 \u4E2A\uFF09" maxlength="2000">' +
          '<button id="btn-submit-std" class="pixel-btn">' +
            '&blacktriangleright; \u5F00\u59CB\u5206\u6790' +
          '</button>' +
        '</div>' +

        // 高级入口：一句话。省掉填表，代价是竞品/维度要靠模型抽，缺了参数就转探索。
        '<div id="panel-advanced" class="pixel-hidden">' +
          '<p class="pixel-mode-hint">\u4E00\u53E5\u8BDD\u4EA4\u7ED9 Agent \u81EA\u5DF1\u8BC6\u522B \u00B7 \u5148\u62BD\u7ADE\u54C1\u4E0E\u7EF4\u5EA6\uFF0C\u7F3A\u53C2\u6570\u65F6\u8D70 Supervisor \u63A2\u7D22\u8865\u9F50\uFF0C\u7701\u4E8B\u4F46\u66F4\u6162\u3001\u66F4\u4F9D\u8D56\u6A21\u578B\u63A8\u65AD</p>' +
          '<textarea id="adv-query" class="pixel-textarea pixel-mb-sm" rows="4"' +
                    ' placeholder="> \u4F8B\u5982\uFF1A\u5BF9\u6BD4\u738B\u8005\u8363\u8000\u548C\u82F1\u96C4\u8054\u76DF\u5728\u73A9\u6CD5\u8BBE\u8BA1\u3001\u7F8E\u672F\u98CE\u683C\u3001\u7535\u7ADE\u751F\u6001\u3001\u5546\u4E1A\u5316\u65B9\u9762\u7684\u8868\u73B0"></textarea>' +
          '<p class="pixel-mode-hint pixel-text-amber">\u26A0 \u81F3\u5C11\u9700\u80FD\u8BC6\u522B\u51FA\u7ADE\u54C1\u6216\u7EF4\u5EA6\u4E4B\u4E00\uFF1B\u4E24\u8005\u90FD\u8BC6\u522B\u4E0D\u5230\u4F1A\u88AB\u76F4\u63A5\u62D2\u7EDD</p>' +
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
  var tabStandard = document.getElementById('tab-standard');
  var tabAdvanced = document.getElementById('tab-advanced');
  var panelStandard = document.getElementById('panel-standard');
  var panelAdvanced = document.getElementById('panel-advanced');
  var msgEl = document.getElementById('submit-msg');
  var stdTitle = document.getElementById('std-title');
  var stdCompetitors = document.getElementById('std-competitors');
  var stdDimensions = document.getElementById('std-dimensions');
  var advQuery = document.getElementById('adv-query');
  var btnSubmitStd = document.getElementById('btn-submit-std');
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

  // 单次任务规模上限——与后端 src/config.py 的 max_competitors / max_dimensions 保持一致。
  // 后端也会 400，这里只是让用户不必等一次网络往返才看到错误。
  // 【为什么要上限】竞品一多，检索池（按 task 混池取 60 条）被摊薄，
  // 容易出现某竞品在某维度整块「数据不足」，而报告看起来仍是完整的。
  var MAX_COMPETITORS = 4;
  var MAX_DIMENSIONS = 4;

  function splitList(s) {
    return s.split(/[,，]/).map(function(x) { return x.trim(); }).filter(Boolean);
  }

  function checkLimit(list, label, max) {
    if (list.length > max) {
      showMsg('> ' + label + ' ' + list.length + ' 个，最多 ' + max + ' 个，请精简后重试', 'error');
      SFX.error();
      return false;
    }
    return true;
  }

  if (tabStandard && tabAdvanced && panelStandard && panelAdvanced) {
    tabStandard.addEventListener('click', function() {
      mode = 'standard';
      tabStandard.classList.add('active');
      tabAdvanced.classList.remove('active');
      panelStandard.classList.remove('pixel-hidden');
      panelAdvanced.classList.add('pixel-hidden');
      SFX.blip();
      if (stdCompetitors) stdCompetitors.focus();
    });

    tabAdvanced.addEventListener('click', function() {
      mode = 'advanced';
      tabAdvanced.classList.add('active');
      tabStandard.classList.remove('active');
      panelAdvanced.classList.remove('pixel-hidden');
      panelStandard.classList.add('pixel-hidden');
      SFX.blip();
      if (advQuery) advQuery.focus();
    });
  }

  async function submitStandard() {
    var title = stdTitle.value.trim();
    var competitors = stdCompetitors.value.trim();
    var dimensions = stdDimensions.value.trim();

    var compList = competitors ? splitList(competitors) : [];
    var dimList = dimensions ? splitList(dimensions) : [];

    // 竞品/维度至少一项：标题只负责命名，不构成分析锚点。
    // 后端 _reject_no_anchor 对两个都空的请求返回 400，这里先本地拦一次，省一趟往返。
    if (!compList.length && !dimList.length) {
      showMsg('> \u7ADE\u54C1\u6216\u7EF4\u5EA6\u81F3\u5C11\u586B\u5199\u4E00\u9879\uFF08\u6807\u9898\u4EC5\u7528\u4E8E\u547D\u540D\uFF09', 'error');
      SFX.error();
      return;
    }
    if (!checkLimit(compList, '\u7ADE\u54C1', MAX_COMPETITORS)) return;
    if (!checkLimit(dimList, '\u7EF4\u5EA6', MAX_DIMENSIONS)) return;

    btnSubmitStd.disabled = true;
    btnSubmitStd.textContent = '\u5904\u7406\u4E2D...';

    try {
      var body = { title: title || '\u672A\u547D\u540D' };
      if (compList.length) body.competitors = compList;
      if (dimList.length) body.dimensions = dimList;
      var data = await API.post('/api/tasks', body);
      showMsg('> \u4EFB\u52A1\u5DF2\u521B\u5EFA :: ' + data.task_id, 'success');
      SFX.confirm();
      setTimeout(function() { window.location.hash = '#/task/' + data.task_id; }, 600);
    } catch (e) {
      showMsg('> \u9519\u8BEF: ' + e.message, 'error');
      SFX.error();
      btnSubmitStd.disabled = false;
      btnSubmitStd.textContent = '\u25B6 \u5F00\u59CB\u5206\u6790';
    }
  }

  async function submitAdvanced() {
    var query = advQuery.value.trim();
    if (!query) {
      showMsg('> \u8BF7\u8F93\u5165\u5206\u6790\u5185\u5BB9', 'error');
      SFX.error();
      return;
    }

    btnSubmitAdv.disabled = true;
    btnSubmitAdv.textContent = '\u5904\u7406\u4E2D...';

    try {
      var data = await API.post('/api/tasks', { title: query.substring(0, 80), query: query });
      showMsg('> \u4EFB\u52A1\u5DF2\u521B\u5EFA :: ' + data.task_id, 'success');
      SFX.confirm();
      setTimeout(function() { window.location.hash = '#/task/' + data.task_id; }, 600);
    } catch (e) {
      // 抽不出竞品/维度时后端给 400，detail 已经是给用户看的话，直接透出
      showMsg('> \u9519\u8BEF: ' + e.message, 'error');
      SFX.error();
      btnSubmitAdv.disabled = false;
      btnSubmitAdv.textContent = '\u25B6 \u5F00\u59CB\u5206\u6790';
    }
  }

  if (advQuery) {
    advQuery.addEventListener('keydown', function(e) {
      if (e.key === 'Enter' && e.ctrlKey) {
        e.preventDefault();
        submitAdvanced();
      }
    });
  }
  if (stdDimensions) {
    stdDimensions.addEventListener('keydown', function(e) {
      if (e.key === 'Enter') submitStandard();
    });
  }

  if (btnSubmitStd) btnSubmitStd.addEventListener('click', submitStandard);
  if (btnSubmitAdv) btnSubmitAdv.addEventListener('click', submitAdvanced);
  if (btnRefresh) btnRefresh.addEventListener('click', function() { SFX.click(); loadTasks(); });
  if (btnLogout) btnLogout.addEventListener('click', function() { SFX.click(); Auth.logout(); });

  // 默认落在标准模式：第一个该填的是竞品，不是标题
  if (stdCompetitors) stdCompetitors.focus();

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
