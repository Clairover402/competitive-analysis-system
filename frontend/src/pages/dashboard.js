/* dashboard.js — Pixel CRT Terminal Dashboard */

import { Auth } from '../js/auth.js';
import { API } from '../js/api.js';
import { SFX } from '../js/sounds.js';

// 两个入口 = 两种交互形态，命名跟着"用户要做什么"走：
//   标准模式（默认）：自己给竞品与维度 → 参数齐全 → Pipeline 确定性分析，**一定出报告**
//   对话模式        ：一句话交给漏斗判意图 → 四种之一，**只有 analysis 会出报告**
//
// ⚠️ 旧名叫"高级模式"，立论是"更慢更不确定的那条才配叫高级"。接上意图漏斗后
//    lookup / trace / verify 三个意图都比分析更快更确定，这个词的立论已经失效，
//    2026-10-27 改名（见 notes/实施计划_意图漏斗与对话入口_2026-10-27.md D4）。
//
// ⚠️ 对话模式**不跳页**：一次任务分析要跑几分钟，跳走就看不了对话了。
//    只有 type=task 会在气泡里给一个"查看进度"入口，由用户自己决定跳不跳。
//
// 这里原先有个 var mode = 'standard'，两个 tab 切换时改写它，但没人读过 ——
// 提交走哪个接口是由"哪个按钮被点"决定的，不是由 mode 决定的。D4 顺手删掉。

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
        // 保鲜期是唯一一项"配了但界面上点不到"的偏好（后端只有 PUT，没有入口）。
        // 放在侧边栏而不是塞进对话模式：它是设置项，不该靠用户碰巧说对一句话才生效。
        '<button id="btn-prefs" class="pixel-btn-secondary pixel-w-full" ' +
                'style="margin-bottom:8px">\u4E8B\u5B9E\u4FDD\u9C9C\u671F</button>' +
        '<button id="btn-logout" class="pixel-btn-secondary pixel-w-full">\u9000\u51FA\u767B\u5F55</button>' +
      '</div>' +

    '</nav>' +

    '<main style="flex:1; padding:28px; overflow-y:auto">' +

      '<div class="pixel-box pixel-mb-lg">' +

        '<div class="pixel-tabs">' +
          '<button id="tab-standard" class="pixel-tab active">\u6807\u51C6\u6A21\u5F0F</button>' +
          '<button id="tab-chat" class="pixel-tab">\u5BF9\u8BDD\u6A21\u5F0F</button>' +
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

        // 对话入口：一句话进去，后端按漏斗判意图。四种结果：出报告 / 查报告 / 查证据 / 反问。
        // 提示语必须写明"不是每句话都出报告"，否则用户会等着一个永远不会来的报告。
        '<div id="panel-chat" class="pixel-hidden">' +
          '<p class="pixel-mode-hint">\u4E00\u53E5\u8BDD\u8BF4\u6E05\u8981\u505A\u4EC0\u4E48 \u00B7 \u81EA\u52A8\u5224\u65AD\u610F\u56FE\uFF1A\u5206\u6790\u7ADE\u54C1 / \u627E\u5386\u53F2\u62A5\u544A / \u67E5\u8BC1\u636E\u51FA\u5904 / \u6838\u5BF9\u6570\u636E\u65B0\u65E7\uFF1B\u53EA\u6709\u300C\u5206\u6790\u300D\u4F1A\u4EA7\u51FA\u62A5\u544A</p>' +
          '<div class="pixel-chat-bar">' +
            '<span class="pixel-chat-conv">\u591A\u8F6E\u5BF9\u8BDD\uFF1A\u540E\u9762\u7684\u8BDD\u4F1A\u5E26\u4E0A\u524D\u6587</span>' +
            '<div class="pixel-chat-acts">' +
              '<button id="btn-conv-list" class="pixel-chat-new" type="button">\u25C7 \u5386\u53F2</button>' +
              '<button id="btn-new-chat" class="pixel-chat-new" type="button">+ \u65B0\u5BF9\u8BDD</button>' +
            '</div>' +
          '</div>' +
          '<div id="chat-log" class="pixel-chat-log"></div>' +
          /* 历史列表与 chat-log 同位置切换（不是浮层）：
             浮层要处理定位、层级、点外部关闭三件事，而这里只需要"换个视图看"。 */
          '<div id="conv-list" class="pixel-chat-log pixel-hidden"></div>' +
          '<textarea id="chat-input" class="pixel-textarea pixel-mb-sm" rows="3"' +
                    ' placeholder="> \u4F8B\u5982\uFF1A\u4EE5\u524D\u8DD1\u8FC7\u7684\u7ADE\u54C1\u5206\u6790\u62A5\u544A\u5728\u54EA\uFF08Enter \u53D1\u9001\uFF0CShift+Enter \u6362\u884C\uFF09"></textarea>' +
          '<button id="btn-submit-chat" class="pixel-btn">' +
            '&blacktriangleright; \u53D1\u9001' +
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

    // ── 事实保鲜期弹层 ──
    // 骨架常驻 DOM、默认 display:none：打开时才去拉接口，避免每次进仪表盘
    // 都多打一次 /api/preferences（这个页面本来就要拉任务列表 + 会话列表）。
    '<div id="pref-mask" class="pixel-modal-mask" style="display:none">' +
      '<div class="pixel-box pixel-modal">' +
        '<div class="pixel-modal-head">' +
          '<span class="pixel-modal-title">\u4E8B\u5B9E\u4FDD\u9C9C\u671F</span>' +
          '<button id="pref-close" class="pixel-btn-secondary pixel-btn-compact">\u5173\u95ED</button>' +
        '</div>' +
        '<p class="pixel-mode-hint">' +
          '\u4E00\u6761\u7ED3\u8BBA\u591A\u5C11\u5929\u540E\u7B97\u300C\u65E7\u300D\u3002' +
          '\u952E\u662F\u7EF4\u5EA6\u6216\u7ED3\u8BBA\u91CC\u7684\u5173\u952E\u8BCD\uFF08\u5982 \u5B9A\u4EF7\u3001\u878D\u8D44\uFF09\uFF0C' +
          '\u547D\u4E2D\u5C31\u6309\u8FD9\u4E2A\u5929\u6570\u7B97\uFF1B\u672A\u547D\u4E2D\u7684\u8D70\u5185\u7F6E\u6863\u4F4D\u3002' +
        '</p>' +
        '<div class="pixel-ttl-head">' +
          '<span class="pixel-ttl-key">\u952E\uFF08\u7EF4\u5EA6\u6216\u5173\u952E\u8BCD\uFF09</span>' +
          '<span class="pixel-ttl-head-days">\u5929\u6570</span>' +
        '</div>' +
        '<div id="pref-ttl-rows"></div>' +
        '<button id="pref-add-row" class="pixel-btn-secondary pixel-btn-compact pixel-mt-sm">' +
          '+ \u52A0\u4E00\u6761' +
        '</button>' +
        '<div class="pixel-modal-actions">' +
          '<button id="pref-save" class="pixel-btn pixel-btn-compact">\u4FDD\u5B58</button>' +
          '<span id="pref-msg" class="pixel-msg"></span>' +
        '</div>' +
      '</div>' +
    '</div>' +

  '</div>';
}

export function mount() {
  var tabStandard = document.getElementById('tab-standard');
  var tabChat = document.getElementById('tab-chat');
  var panelStandard = document.getElementById('panel-standard');
  var panelChat = document.getElementById('panel-chat');
  var msgEl = document.getElementById('submit-msg');
  var stdTitle = document.getElementById('std-title');
  var stdCompetitors = document.getElementById('std-competitors');
  var stdDimensions = document.getElementById('std-dimensions');
  var chatLog = document.getElementById('chat-log');
  var chatInput = document.getElementById('chat-input');
  var btnNewChat = document.getElementById('btn-new-chat');
  var btnConvList = document.getElementById('btn-conv-list');
  var convListEl = document.getElementById('conv-list');
  var btnSubmitStd = document.getElementById('btn-submit-std');
  var btnSubmitChat = document.getElementById('btn-submit-chat');
  var btnRefresh = document.getElementById('btn-refresh-tasks');
  var btnLogout = document.getElementById('btn-logout');

  // ── 事实保鲜期（ttl_overrides）编辑器 ────────────────────────────────
  // 这一项此前只有 PUT 接口、前端没有入口 —— 用户能配却点不到，等于没做。
  // 后端校验规则（src/memory/preferences.py::_check_ttl_overrides）在这里
  // 复刻了一份：不是重复造轮子，是为了不必等一次网络往返才看到"哪写错了"。
  // ⚠️ 后端那几条必须一一对上，改一处要改两处：非空、≤8 项、键≤16 字、
  //    天数 1~3650 的整数（**布尔也要挡**，JSON 里 true 会被当成 1）。
  var TTL_MAX_ITEMS = 8;
  var TTL_MIN_DAYS = 1;
  var TTL_MAX_DAYS = 3650;
  var prefMask = document.getElementById('pref-mask');
  var prefRows = document.getElementById('pref-ttl-rows');
  var prefMsg = document.getElementById('pref-msg');

  function prefSay(text, type) {
    if (!prefMsg) return;
    prefMsg.textContent = text;
    prefMsg.className = 'pixel-msg';
    if (type === 'error') prefMsg.classList.add('pixel-msg-error');
    else if (type === 'success') prefMsg.classList.add('pixel-msg-success');
    else prefMsg.classList.add('pixel-msg-info');
  }

  function addTtlRow(key, days) {
    if (!prefRows || prefRows.children.length >= TTL_MAX_ITEMS) return;
    var row = document.createElement('div');
    row.className = 'pixel-ttl-row';
    row.innerHTML =
      '<input class="pixel-input pixel-ttl-key" maxlength="16" ' +
             'placeholder="\u7EF4\u5EA6 / \u5173\u952E\u8BCD\uFF08\u5982 \u5B9A\u4EF7\uFF09">' +
      '<input class="pixel-input pixel-ttl-days" type="number" min="1" max="3650" ' +
             'placeholder="30">' +
      '<span class="pixel-ttl-unit">\u5929</span>' +
      '<button class="pixel-ttl-del" title="\u5220\u9664">\u00D7</button>';
    row.querySelector('.pixel-ttl-key').value = key || '';
    row.querySelector('.pixel-ttl-days').value = (days === 0 || days) ? days : '';
    row.querySelector('.pixel-ttl-del').onclick = function() {
      row.remove();
      SFX.blip();
    };
    prefRows.appendChild(row);
  }

  function collectTtl() {
    /* 收集并当场校验。返回 {ok:true, value} 或 {ok:false, error}。
     * 校验顺序与后端一致，报错文案也尽量贴近 —— 用户看到的两处说法
     * 不一致时，会以为是两个功能。 */
    var out = {};
    if (!prefRows) return { ok: true, value: out };
    var rows = prefRows.querySelectorAll('.pixel-ttl-row');
    for (var i = 0; i < rows.length; i++) {
      var k = rows[i].querySelector('.pixel-ttl-key').value.trim();
      var dRaw = rows[i].querySelector('.pixel-ttl-days').value.trim();
      if (!k && !dRaw) continue;                 // 整行空着 = 没填，不算错
      if (!k) return { ok: false, error: '\u7B2C ' + (i + 1) + ' \u884C\u6CA1\u586B\u5173\u952E\u8BCD' };
      if (k.length > 16) return { ok: false, error: '\u300C' + k + '\u300D\u8D85\u8FC7 16 \u5B57' };
      if (Object.prototype.hasOwnProperty.call(out, k)) {
        return { ok: false, error: '\u300C' + k + '\u300D\u91CD\u590D\u4E86' };
      }
      if (!/^\d+$/.test(dRaw)) return { ok: false, error: '\u300C' + k + '\u300D\u5929\u6570\u9700\u8981\u662F\u6574\u6570' };
      var d = parseInt(dRaw, 10);
      if (d < TTL_MIN_DAYS || d > TTL_MAX_DAYS) {
        return { ok: false, error: '\u300C' + k + '\u300D\u5929\u6570\u9700\u5728 ' +
                 TTL_MIN_DAYS + '~' + TTL_MAX_DAYS + ' \u4E4B\u95F4' };
      }
      out[k] = d;
      if (Object.keys(out).length > TTL_MAX_ITEMS) {
        return { ok: false, error: '\u6700\u591A ' + TTL_MAX_ITEMS + ' \u9879' };
      }
    }
    return { ok: true, value: out };
  }

  async function openPrefs() {
    if (!prefMask) return;
    prefMask.style.display = 'flex';
    prefSay('\u52A0\u8F7D\u4E2D...', 'info');
    if (prefRows) prefRows.innerHTML = '';
    try {
      var data = await API.get('/api/preferences');
      var ttl = null;
      (data.items || []).forEach(function(it) {
        if (it.key === 'ttl_overrides') ttl = it.value;
      });
      if (ttl && typeof ttl === 'object') {
        Object.keys(ttl).forEach(function(k) { addTtlRow(k, ttl[k]); });
      }
      if (!prefRows || !prefRows.children.length) addTtlRow('', '');
      prefSay(ttl ? '' : '\u8FD8\u6CA1\u8BBE\u7F6E\u8FC7\uFF0C\u4E0B\u9762\u662F\u4E00\u4E2A\u7A7A\u6863', 'info');
    } catch (e) {
      prefSay('\u8BFB\u53D6\u5931\u8D25: ' + (e.message || '\u8BF7\u6C42\u5931\u8D25'), 'error');
      SFX.error();
    }
  }

  async function savePrefs() {
    var res = collectTtl();
    if (!res.ok) {
      prefSay(res.error, 'error');
      SFX.error();
      return;
    }
    if (!Object.keys(res.value).length) {
      // 空对象后端会 400（"需要是非空对象"）。要么删掉这条偏好，要么别保存。
      prefSay('\u81F3\u5C11\u586B\u4E00\u6761\uFF0C\u6216\u76F4\u63A5\u5173\u95ED', 'error');
      SFX.error();
      return;
    }
    try {
      var r = await API.put('/api/preferences/ttl_overrides', {
        value: res.value, source: 'user_set',
      });
      // 回执必须如实：存下了但不会生效时，说"已保存"等于骗用户设置成功了
      if (r && r.will_apply === false) {
        prefSay('\u5DF2\u4FDD\u5B58\uFF0C\u4F46\u4E0E\u7CFB\u7EDF\u89C4\u5219\u51B2\u7A81\uFF0C' +
                '\u672C\u6B21\u4E0D\u4F1A\u751F\u6548\uFF08' + (r.reason || '\u672A\u77E5\u539F\u56E0') + '\uFF09', 'error');
      } else {
        prefSay('\u5DF2\u4FDD\u5B58\uFF0C\u4E0B\u6B21\u5224\u5B9A\u65B0\u65E7\u65F6\u6309\u8FD9\u4E2A\u7B97', 'success');
      }
      SFX.confirm();
    } catch (e) {
      prefSay('\u6CA1\u4FDD\u5B58\u6210\u529F: ' + (e.message || '\u8BF7\u6C42\u5931\u8D25'), 'error');
      SFX.error();
    }
  }

  var btnPrefs = document.getElementById('btn-prefs');
  if (btnPrefs) btnPrefs.onclick = function() { SFX.blip(); openPrefs(); };
  var prefClose = document.getElementById('pref-close');
  if (prefClose) prefClose.onclick = function() { prefMask.style.display = 'none'; };
  var prefAdd = document.getElementById('pref-add-row');
  if (prefAdd) prefAdd.onclick = function() {
    if (prefRows && prefRows.children.length >= TTL_MAX_ITEMS) {
      prefSay('\u6700\u591A ' + TTL_MAX_ITEMS + ' \u9879', 'error');
      return;
    }
    addTtlRow('', '');
    SFX.blip();
  };
  var prefSave = document.getElementById('pref-save');
  if (prefSave) prefSave.onclick = savePrefs;
  // 点遮罩关闭：只认"点在遮罩本身"，点在弹层内部冒泡上来的不算
  if (prefMask) prefMask.onclick = function(ev) {
    if (ev.target === prefMask) prefMask.style.display = 'none';
  };
  // ── 保鲜期编辑器结束 ──

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

  if (tabStandard && tabChat && panelStandard && panelChat) {
    tabStandard.addEventListener('click', function() {
      tabStandard.classList.add('active');
      tabChat.classList.remove('active');
      panelStandard.classList.remove('pixel-hidden');
      panelChat.classList.add('pixel-hidden');
      SFX.blip();
      if (stdCompetitors) stdCompetitors.focus();
    });

    tabChat.addEventListener('click', function() {
      tabChat.classList.add('active');
      tabStandard.classList.remove('active');
      panelChat.classList.remove('pixel-hidden');
      panelStandard.classList.add('pixel-hidden');
      SFX.blip();
      renderChatLog();
      if (chatInput) chatInput.focus();
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

  // ── 对话模式 ─────────────────────────────────────────────────────────────
  // 一句话 → POST /api/chat → 按返回的 type 渲染三种气泡。这里**不复制任何判定逻辑**：
  // 意图、澄清文案、选项句子全由后端给，前端只负责画。多写一份判据 = 两处不一致。
  //
  // conversation_id 由后端新建并回传，前端只负责存下来、下一轮带上。
  // ⚠️ 不要自己造 id（D4 时用过 'local-xxx' 占位）—— 后端会当成非法会话直接 404。
  // 存 localStorage：刷新页面后还能接着上一轮，而不是被迫从零开始。
  var CHAT_CONV_KEY = 'chat_conv_id';
  var chatConvId = null;
  try { chatConvId = localStorage.getItem(CHAT_CONV_KEY) || null; } catch (e) { chatConvId = null; }
  var chatMessages = [];
  var chatBusy = false;

  var INTENT_LABEL = {
    analysis: '竞品分析',
    lookup: '历史报告',
    trace: '证据溯源',
    verify: '数据新旧',
  };

  // ★ 必须转义：summary / claim / source_text 全是**从竞品官网抓回来的原文**，
  //   里面出现 < > & 是常态，不转义会被当成 HTML 执行。
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function cut(s, n) {
    var t = String(s == null ? '' : s);
    return t.length > n ? t.slice(0, n) + '…' : t;
  }

  // 与后端 _time_cn 的 "10-27 20:22" 对齐 —— 同一个气泡里两种日期格式很扎眼。
  function fmtTime(iso) {
    if (!iso) return '未知时间';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return '未知时间';
    function p(n) { return n < 10 ? '0' + n : '' + n; }
    return p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' '
      + p(d.getHours()) + ':' + p(d.getMinutes());
  }

  // 只有 http(s) 才渲染成链接。抓回来的 URL 里若出现 javascript: 之类，
  // 直接铺成文本 —— 转义挡不住协议本身。
  function renderUrl(url) {
    var u = String(url || '');
    if (!/^https?:\/\//i.test(u)) return esc(u || '来源未知');
    return '<a class="pixel-chat-src" href="' + esc(u) + '" target="_blank" rel="noopener noreferrer">' +
      esc(u) + '</a>';
  }

  function renderChatItem(body, taskId) {
    return '<div class="pixel-chat-item"' +
      (taskId ? ' data-task="' + esc(taskId) + '"' : '') + '>' + body + '</div>';
  }

  function statusCn(s) {
    var hit = STATUS_MAP[s];
    return hit ? hit.label : (s || '');
  }

  function renderLookupItem(it) {
    var comps = (it.competitors && it.competitors.length)
      ? it.competitors.join(' / ') : '竞品未记录';
    var ver = it.version ? ('v' + it.version) : '尚无报告';
    return renderChatItem(
      '<div class="pixel-chat-item-head">' + esc(it.title || '未命名') + '</div>' +
      '<div class="pixel-chat-item-sub">' + esc(comps) + ' · ' + esc(statusCn(it.status)) +
        ' · ' + esc(fmtTime(it.created_at)) + ' · 报告 ' + esc(ver) + '</div>',
      it.task_id
    );
  }

  function renderTraceItem(it) {
    return renderChatItem(
      '<div class="pixel-chat-item-head">' + esc(cut(it.claim, 100)) + '</div>' +
      renderUrl(it.source_url) +
      '<div class="pixel-chat-item-sub">' +
        (it.dimension ? esc(it.dimension) + ' · ' : '') +
        '抓取于 ' + esc(fmtTime(it.observed_at)) + '</div>',
      it.task_id
    );
  }

  function renderVerifyItem(it) {
    var st = it.status === 'stale'
      ? { t: '超过保鲜期', cls: 'pixel-badge-warning' }
      : it.status === 'fresh'
        ? { t: '保鲜期内', cls: 'pixel-badge-success' }
        : { t: '时间未知', cls: 'pixel-badge-info' };
    var age = (it.age_days == null) ? '抓取时间未知' : (it.age_days + ' 天前抓取');
    return renderChatItem(
      '<span class="pixel-badge ' + st.cls + '">' + st.t + '</span>' +
      '<div class="pixel-chat-item-head pixel-mt-xs">' + esc(cut(it.claim, 100)) + '</div>' +
      '<div class="pixel-chat-item-sub">' + esc(age) +
        ' · 保鲜期 ' + esc(it.ttl_days) + ' 天</div>',
      it.task_id
    );
  }

  function renderItems(intent, items) {
    if (!items || !items.length) return '';
    var fn = intent === 'trace' ? renderTraceItem
           : intent === 'verify' ? renderVerifyItem
           : renderLookupItem;   // lookup 是默认，也是唯一一定能走到这里的分支
    return '<div class="pixel-chat-items">' + items.map(fn).join('') + '</div>';
  }

  /* 历史报告分页条（lookup）。
   *
   * ⚠️ 按钮上的 data-pager 是**消息在 chatMessages 里的下标**，不是任务 id。
   *    点"下一页"要改的是这一条消息自己的 items，不是跳转 —— 所以它必须走
   *    单独的委托分支，不能落到下面 `[data-task]` 那条（那会跳去任务详情页）。
   *
   * 只在 total_pages > 1 时出现：一页装得下就没有"翻"这件事，
   * 画两个永远灰着的按钮只会让人以为坏了。
   */
  function renderPager(m, idx) {
    var p = m.pagination;
    if (!p || !p.total_pages || p.total_pages <= 1) return '';
    return '<div class="pixel-chat-pager">' +
      '<button type="button" class="pixel-btn pixel-btn-secondary pixel-btn-compact"' +
        ' data-pager="' + idx + '" data-page="' + (p.page - 1) + '"' +
        (p.has_prev ? '' : ' disabled') + '>上一页</button>' +
      '<span class="pixel-chat-pager-info">第 ' + p.page + ' / ' + p.total_pages +
        ' 页 · 共 ' + p.total + ' 份</span>' +
      '<button type="button" class="pixel-btn pixel-btn-secondary pixel-btn-compact"' +
        ' data-pager="' + idx + '" data-page="' + (p.page + 1) + '"' +
        (p.has_next ? '' : ' disabled') + '>下一页</button>' +
      '</div>';
  }

  function renderChatMessage(m, idx) {
    if (m.role === 'user') {
      return '<div class="pixel-chat-row user">' +
        '<div class="pixel-chat-bubble user">' + esc(m.text) + '</div></div>';
    }

    if (m.kind === 'pending') {
      return '<div class="pixel-chat-row"><div class="pixel-chat-bubble bot">' +
        '<span class="pixel-spinner" style="margin-right:8px;vertical-align:middle"></span>' +
        '判定意图中...</div></div>';
    }

    if (m.kind === 'error') {
      return '<div class="pixel-chat-row">' +
        '<div class="pixel-chat-bubble bot err">' + esc(m.text) + '</div></div>';
    }

    if (m.kind === 'clarify') {
      // 两种澄清共用 type=clarify，靠 intent 区分：
      //   unknown   → 不知道要什么（L4）
      //   analysis  → 知道要分析，但缺竞品/维度
      var meta = m.intent === 'analysis' ? '缺少参数' : '意图未判定';
      var opts = (m.options || []).map(function(o) {
        return '<button class="pixel-chat-opt" data-msg="' + esc(o.message) + '">' +
          esc(o.label) + '</button>';
      }).join('');
      return '<div class="pixel-chat-row"><div class="pixel-chat-bubble bot">' +
        '<div class="pixel-chat-meta">' + esc(meta) + '</div>' +
        '<div>' + esc(m.question) + '</div>' +
        (opts ? '<div class="pixel-chat-opts">' + opts + '</div>' : '') +
        '</div></div>';
    }

    if (m.kind === 'pref') {
      // 偏好确认卡（M4）：LLM 从对话里提取的规则**只能提议**，
      // 点"以后都这样"才转正（POST .../confirm），点"忽略"就是删掉这条提案。
      // ⚠️ 不点就一直是 proposed —— 它永远不进 prompt，这是硬约束不是提示。
      var cards = (m.cards || []).map(function(c) {
        return '<div class="pixel-chat-pref">' +
          '<div class="pixel-chat-pref-q">' + esc(c.question) + '</div>' +
          '<div class="pixel-chat-opts">' +
            '<button class="pixel-chat-opt" data-pref-key="' + esc(c.key) + '" data-pref-act="confirm">以后都这样</button>' +
            '<button class="pixel-chat-opt ghost" data-pref-key="' + esc(c.key) + '" data-pref-act="dismiss">忽略</button>' +
          '</div></div>';
      }).join('');
      return '<div class="pixel-chat-row"><div class="pixel-chat-bubble bot">' +
        '<div class="pixel-chat-meta">记住这个偏好？</div>' + cards + '</div></div>';
    }

    if (m.kind === 'note') {
      // 系统回执（目前只有偏好操作）：一句话、无条目、不可点
      return '<div class="pixel-chat-row"><div class="pixel-chat-bubble bot">' +
        '<div class="pixel-chat-meta">' + esc(m.meta || '已处理') + '</div>' +
        '<div>' + esc(m.text) + '</div></div></div>';
    }

    if (m.kind === 'replay') {
      // 回放气泡：conversation_turns 里只有当轮的回答文本，卡片的结构化结果没存。
      // 所以这里只铺文本 + 一条任务入口 —— 产物在任务详情里，不在这伪造一份。
      // ⚠️ 别想着"顺手把 cards 也存进 turns"：那等于给同一次任务造第二个真相，
      //    和当初砍掉的 episode 摘要是一个坑（见记忆系统文档第六节）。
      var rlabel = INTENT_LABEL[m.intent] || m.intent || '历史';
      var rwhen = (m.when || '').slice(0, 16).replace('T', ' ');
      return '<div class="pixel-chat-row"><div class="pixel-chat-bubble bot">' +
        '<div class="pixel-chat-meta">' + esc(rlabel) +
          (rwhen ? ' · ' + esc(rwhen) : '') + '</div>' +
        '<div>' + esc(m.text) + '</div>' +
        (m.taskId
          ? renderChatItem(
              '<div class="pixel-chat-item-action">&blacktriangleright; 查看那次任务 ' +
              esc(String(m.taskId).slice(0, 8)) + '</div>',
              m.taskId)
          : '') +
        '</div></div>';
    }

    if (m.kind === 'task') {
      var e = m.entities || {};
      var comps = (e.competitors && e.competitors.length) ? e.competitors.join(' / ') : '—';
      var dims = (e.dimensions && e.dimensions.length) ? e.dimensions.join(' / ') : '—';
      return '<div class="pixel-chat-row"><div class="pixel-chat-bubble bot">' +
        '<div class="pixel-chat-meta">任务已创建 · 后台运行</div>' +
        renderChatItem(
          '<div class="pixel-chat-item-head">竞品：' + esc(comps) + '</div>' +
          '<div class="pixel-chat-item-sub">维度：' + esc(dims) + '</div>' +
          '<div class="pixel-chat-item-action">&blacktriangleright; 查看进度 ' +
            esc(String(m.taskId || '').slice(0, 8)) + '</div>',
          m.taskId
        ) +
        '</div></div>';
    }

    // answer
    var label = INTENT_LABEL[m.intent] || m.intent || '';
    var hit = m.layer ? (' · ' + m.layer) : '';
    var conf = (typeof m.confidence === 'number' && m.confidence > 0)
      ? (' ' + m.confidence.toFixed(2)) : '';
    // data-msg 供翻页后"回到这一条顶部"用（见 scrollToMessage）。
    // 分页条放在列表**上方**：聊天区是固定高度的滚动容器，放底部会随列表变长
    // 沉到看不见的地方 —— 用户翻完页还得先滚到底才能点下一次，来回折腾。
    return '<div class="pixel-chat-row" data-msg="' + idx + '">' +
      '<div class="pixel-chat-bubble bot">' +
      '<div class="pixel-chat-meta">' + esc(label) + esc(hit) + esc(conf) + '</div>' +
      '<div>' + esc(m.summary || '没有结果。') + '</div>' +
      renderPager(m, idx) +
      renderItems(m.intent, m.items) +
      '</div></div>';
  }

  function renderChatLog(keepScroll) {
    if (!chatLog) return;
    if (!chatMessages.length) {
      chatLog.innerHTML = '<div class="pixel-chat-empty">' +
        '<p style="font-size:24px;margin-bottom:10px">&#x1f4ac;</p>' +
        '<p>试试这几句：</p>' +
        '<p>· 我以前跑过的竞品分析报告在哪</p>' +
        '<p>· 报告里飞书比钉钉便宜这个结论的证据在哪</p>' +
        '<p>· 分析飞书和钉钉在功能和定价上的表现</p>' +
        '</div>';
      return;
    }
    // ⚠️ 翻页时不能滚到底：用户点"下一页"是想看这一页的**第一行**，
    //    滚到最底会直接跳过整页内容。其他地方（新消息、错误）仍然滚到底。
    var top = chatLog.scrollTop;
    chatLog.innerHTML = chatMessages
      .map(function(m, i) { return renderChatMessage(m, i); }).join('');
    chatLog.scrollTop = keepScroll ? top : chatLog.scrollHeight;
  }

  function botMessageFrom(d) {
    if (d.type === 'task') {
      return { role: 'bot', kind: 'task', taskId: d.task_id, entities: d.entities || {} };
    }
    if (d.type === 'clarify') {
      return {
        role: 'bot', kind: 'clarify',
        question: d.question, options: d.options || [], intent: d.intent,
      };
    }
    return {
      role: 'bot', kind: 'answer',
      intent: d.intent, layer: d.layer, confidence: d.confidence,
      summary: d.summary, items: d.items || [],
      /* 历史报告分页（2026-10-29）。两个字段分工：
         · pagination 决定要不要画分页条、画第几页
         · lookup_params 是翻页时要原样带回去的查询条件（后端抽的槽，前端不复刻）
         其它意图两者都是 null，renderChatMessage 里自然不画。 */
      pagination: d.pagination || null,
      lookup_params: d.lookup_params || null,
    };
  }

  /* 把聊天区滚到某条消息的顶部。
   *
   * ⚠️ 不能用 scrollIntoView：那会连整个页面一起滚，把输入框顶出屏幕外。
   *    这里只动 chatLog 自己的 scrollTop。
   * 为什么需要它：renderChatLog 默认滚到底（新消息该在底部），但翻页时
   *    用户要的是"这一页的第一条"。停在底部 = 直接跳过整页内容。
   */
  function scrollToMessage(idx) {
    if (!chatLog) return;
    var el = chatLog.querySelector('[data-msg="' + idx + '"]');
    if (!el) return;
    // ⚠️ 用 rect 差值而不是 offsetTop：offsetTop 相对的是 offsetParent，
    //    chatLog 不一定是它（取决于哪层设了 position），offsetTop 会少算一截。
    var delta = el.getBoundingClientRect().top - chatLog.getBoundingClientRect().top;
    chatLog.scrollTop += delta - 8;
  }

  /* 翻一页历史报告。
   *
   * 走 GET /api/lookup 而不是重发那句话，三个理由：
   *   1. 重发要再走一次 L2 意图识别（白付一次编码），还要重新抽一遍槽位
   *   2. 会往会话流水里写一行"第 2 页"，把真实对话淹掉
   *   3. 同一句话第二次未必还判成 lookup —— 凭空多一个会变的环节
   *
   * 只改这一条消息自己的 items/pagination，**不新增气泡**：翻页不是新的一轮
   * 对话，用户要的是"同一份清单的另一屏"。
   *
   * ⚠️ 下标定位：`data-pager` 存的是消息在 chatMessages 里的位置。中途若有人
   *    删消息（比如 resolvePreference 里 filter 掉已处理的偏好卡），下标会错位。
   *    所以进来先校验 kind/intent —— 宁可什么都不做，也不要把别的消息改成清单。
   */
  async function loadLookupPage(idx, page) {
    var m = chatMessages[idx];
    if (!m || m.kind !== 'answer' || m.intent !== 'lookup' || !m.lookup_params) return;
    var lp = m.lookup_params;
    var qs = '?page=' + page +
      '&page_size=' + encodeURIComponent(lp.page_size || 10) +
      '&competitors=' + encodeURIComponent((lp.competitors || []).join(',')) +
      '&dimensions=' + encodeURIComponent((lp.dimensions || []).join(','));
    try {
      var d = await API.get('/api/lookup' + qs);
      m.items = d.items || [];
      m.pagination = d.pagination;
      m.summary = d.summary;
      // 后端会回显规范化后的参数（全角逗号被拆开之类），以它为准
      m.lookup_params = d.lookup_params || lp;
      SFX.blip();
      renderChatLog(true);
      scrollToMessage(idx);   // 滚到这条消息顶部，让用户看到这一页的第一行
    } catch (e) {
      chatMessages.push({
        role: 'bot', kind: 'error',
        text: '翻页失败: ' + (e.message || '请求失败'),
      });
      SFX.error();
      renderChatLog();
    }
  }

  async function resolvePreference(key, act) {
    /* 确认/忽略一条偏好提案（M4）。
     *
     * ⚠️ 确认之后必须**如实回执"会不会生效"**：后端返回 will_apply=false 意味着
     *    这条偏好与 system 硬规则冲突（比如"不要表格"），存下了但注入时被压制。
     *    这里若统一回"已记住"，用户下次发现没变化只会觉得系统坏了。
     */
    var path = '/api/preferences/' + encodeURIComponent(key);
    try {
      var res = null;
      if (act === 'confirm') {
        res = await API.post(path + '/confirm', {});
        SFX.confirm();
      } else {
        await API.del(path);
        SFX.blip();
      }
      chatMessages.forEach(function(m) {
        if (m.kind === 'pref') {
          m.cards = (m.cards || []).filter(function(c) { return c.key !== key; });
        }
      });
      // 卡片清空后整条消息也撤掉，避免留一个空壳气泡
      chatMessages = chatMessages.filter(function(m) {
        return m.kind !== 'pref' || (m.cards && m.cards.length);
      });
      chatMessages.push({
        role: 'bot', kind: 'note', meta: act === 'confirm' ? '偏好已更新' : '已忽略',
        text: act !== 'confirm' ? '好的，不改。'
          : (res && res.will_apply === false
              ? '已记住，但它与报告结构规则冲突，本次不会生效。'
              : '已记住，以后我按这个来。'),
      });
    } catch (e) {
      chatMessages.push({ role: 'bot', kind: 'error', text: '偏好没保存成功: ' + (e.message || '请求失败') });
      SFX.error();
    }
    renderChatLog();
  }

  async function sendChat(text) {
    if (chatBusy) return;
    var q = String(text || '').trim();
    if (!q) { SFX.error(); return; }

    chatBusy = true;
    if (btnSubmitChat) { btnSubmitChat.disabled = true; btnSubmitChat.textContent = '\u5224\u5B9A\u4E2D...'; }
    chatMessages.push({ role: 'user', text: q });
    chatMessages.push({ role: 'bot', kind: 'pending' });
    renderChatLog();
    if (chatInput) chatInput.value = '';

    try {
      var data = await API.post('/api/chat', { message: q, conversation_id: chatConvId });
      // 后端每轮都回 conversation_id：首轮新建、后续原样带回，这里只管覆盖
      if (data && data.conversation_id) {
        chatConvId = data.conversation_id;
        try { localStorage.setItem(CHAT_CONV_KEY, chatConvId); } catch (e) { /* 隐私模式忽略 */ }
      }
      chatMessages.pop();  // 撤掉"判定中"
      chatMessages.push(botMessageFrom(data));
      // 提案单独成一张卡：混在回答气泡里，用户会以为那是回答的一部分
      if (data && data.pending_preferences && data.pending_preferences.length) {
        chatMessages.push({ role: 'bot', kind: 'pref', cards: data.pending_preferences });
      }
      if (data.type === 'task') {
        SFX.confirm();
        loadTasks();   // 任务卡进列表，用户不跳页也能看到它
      } else {
        SFX.blip();
      }
    } catch (e) {
      chatMessages.pop();
      chatMessages.push({ role: 'bot', kind: 'error', text: '错误: ' + (e.message || '请求失败') });
      SFX.error();
    } finally {
      chatBusy = false;
      if (btnSubmitChat) { btnSubmitChat.disabled = false; btnSubmitChat.textContent = '\u25B6 \u53D1\u9001'; }
      renderChatLog();
      if (chatInput) chatInput.focus();
    }
  }

  if (btnNewChat) {
    btnNewChat.addEventListener('click', function() {
      SFX.click();
      // 只丢本地指针，不删库：上一段对话留在 conversations 表里可查，
      // 但新一段从零开始（否则用户永远在同一个会话里打转，摘要越滚越糊）
      chatConvId = null;
      try { localStorage.removeItem(CHAT_CONV_KEY); } catch (e) { /* ignore */ }
      chatMessages = [];
      hideConvList();
      renderChatLog();
      if (chatInput) chatInput.focus();
    });
  }

  /* ── 历史对话（2026-10-28）────────────────────────────────────────────
   *
   * 起因：点「新对话」只清掉本地指针，那段对话还在库里，界面上却再也找不回来。
   * conversations.title 的字段注释写的是"仅给会话列表看" —— 数据一直为列表
   * 准备着，只是从来没建过读它的接口。这次补的就是读。
   *
   * 三个刻意的取舍：
   *   1. 列表与对话同位置切换，不做浮层 —— 浮层要额外处理定位、层级、点外关闭。
   *   2. 回放只还原文本 + 任务入口，不还原卡片（见 renderChatMessage 的 replay 分支）。
   *   3. 刷新后自动把当前会话拉回来。否则视觉上是空对话、实际下一句会接在旧会话上,
   *      这种错位比"找不到历史"更容易误导人。
   */

  function convItemHtml(c) {
    var title = c.title || '（未命名会话）';
    var when = (c.updated_at || '').slice(0, 16).replace('T', ' ');
    var turns = c.turn_count ? ('共 ' + c.turn_count + ' 轮') : '还没开始';
    var sub = c.last_user_message || '';
    // 只有一轮时 title 就是首句，和 sub 几乎重复；重复显示只会让列表更难扫
    if (sub && (sub === title || sub.indexOf(title) === 0)) sub = '';
    return '<div class="pixel-conv-item' + (c.id === chatConvId ? ' active' : '') +
      '" data-conv="' + esc(c.id) + '">' +
      '<div class="pixel-conv-title">' + esc(title) + '</div>' +
      (sub ? '<div class="pixel-conv-sub">' + esc(sub) + '</div>' : '') +
      '<div class="pixel-conv-meta">' + esc(when) + ' · ' + esc(turns) + '</div>' +
      '</div>';
  }

  async function showConvList() {
    if (!convListEl || !chatLog) return;
    chatLog.classList.add('pixel-hidden');
    convListEl.classList.remove('pixel-hidden');
    // 输入框一并收起来：列表视图下发出的话会落到 chatConvId 指向的那段会话，
    // 但屏幕上没有对话可看 —— 用户会不知道这句话发到哪去了。
    if (chatInput) chatInput.classList.add('pixel-hidden');
    if (btnSubmitChat) btnSubmitChat.classList.add('pixel-hidden');
    if (btnConvList) btnConvList.textContent = '\u2190 \u8FD4\u56DE\u5BF9\u8BDD';
    convListEl.innerHTML = '<div class="pixel-chat-empty"><p>载入中...</p></div>';
    try {
      var d = await API.get('/api/conversations?limit=20');
      var items = (d && d.items) || [];
      if (!items.length) {
        convListEl.innerHTML = '<div class="pixel-chat-empty">' +
          '<p style="font-size:24px;margin-bottom:10px">&#x1f5c2;</p>' +
          '<p>还没有历史对话。</p></div>';
        return;
      }
      convListEl.innerHTML = items.map(convItemHtml).join('');
    } catch (e) {
      convListEl.innerHTML = '<div class="pixel-chat-empty"><p>历史对话读不出来：' +
        esc(e.message || '请求失败') + '</p></div>';
    }
  }

  function hideConvList() {
    if (!convListEl || !chatLog) return;
    convListEl.classList.add('pixel-hidden');
    chatLog.classList.remove('pixel-hidden');
    if (chatInput) chatInput.classList.remove('pixel-hidden');
    if (btnSubmitChat) btnSubmitChat.classList.remove('pixel-hidden');
    if (btnConvList) btnConvList.textContent = '\u25C7 \u5386\u53F2';
  }

  function replayFromTurns(d) {
    var msgs = [];
    ((d && d.items) || []).forEach(function(t) {
      if (t.role === 'user') {
        msgs.push({ role: 'user', text: t.content || '' });
      } else {
        msgs.push({
          role: 'bot', kind: 'replay',
          text: t.content || '',
          intent: t.intent,
          taskId: t.ref_task_id || null,
          when: t.created_at,
        });
      }
    });
    chatMessages = msgs;
    renderChatLog();
  }

  async function openConversation(id, opts) {
    opts = opts || {};
    try {
      var d = await API.get('/api/conversations/' + encodeURIComponent(id) + '/turns');
      replayFromTurns(d);
      chatConvId = d.conversation.id;
      try { localStorage.setItem(CHAT_CONV_KEY, chatConvId); } catch (e) { /* 隐私模式 */ }
      hideConvList();
      if (!opts.silent) {
        SFX.blip();
        chatMessages.push({
          role: 'bot', kind: 'note', meta: '已载入历史对话',
          text: '接着说话会接在这段后面；想从零开始点「+ 新对话」。',
        });
        renderChatLog();
      }
      return true;
    } catch (e) {
      // 404 = 这段会话没了（被删，或换了账号还带着旧指针）。
      // 必须顺手清指针，否则之后每句话都 404，界面看着像"发送一直失败"。
      if (e.status === 404) {
        chatConvId = null;
        try { localStorage.removeItem(CHAT_CONV_KEY); } catch (err) { /* ignore */ }
      }
      if (!opts.silent) {
        chatMessages.push({
          role: 'bot', kind: 'error',
          text: '历史对话打不开：' + (e.message || '请求失败'),
        });
        renderChatLog();
      }
      return false;
    }
  }

  if (btnConvList) {
    btnConvList.addEventListener('click', function() {
      SFX.click();
      if (convListEl && convListEl.classList.contains('pixel-hidden')) showConvList();
      else hideConvList();
    });
  }

  if (convListEl) {
    convListEl.addEventListener('click', function(e) {
      var el = (e.target && e.target.closest) ? e.target.closest('[data-conv]') : null;
      if (!el) return;
      openConversation(el.getAttribute('data-conv'));
    });
  }

  // 刷新后把当前会话拉回来：localStorage 里还留着指针，但气泡是空的。
  // 不拉的话，界面像新对话、实际下一句接在旧会话上 —— 用户会以为系统记错了。
  if (chatConvId) openConversation(chatConvId, { silent: true });

  if (chatLog) {
    chatLog.addEventListener('click', function(e) {
      var t = e.target;
      if (!t || !t.closest) return;

      // 来源链接要能直接打开，别被下面的"点条目跳任务"抢先拦截
      if (t.closest('a')) return;

      // 分页：改的是这条消息自己的清单，不是跳转，也不是新一轮对话。
      // 放在 `[data-task]` 之前 —— 那个分支会改 location.hash，抢到就翻不了页。
      var pager = t.closest('[data-pager]');
      if (pager) {
        if (pager.disabled) return;
        SFX.click();
        loadLookupPage(
          parseInt(pager.getAttribute('data-pager'), 10),
          parseInt(pager.getAttribute('data-page'), 10)
        );
        return;
      }

      var opt = t.closest('.pixel-chat-opt');
      if (opt) {
        SFX.click();
        // 偏好确认卡（M4）：转正 / 忽略，都不发回对话——它们改的是设置，不是提问。
        // ⚠️ 走完要重绘一次：卡片消失才算"处理过了"，否则用户会反复点同一条。
        var prefKey = opt.getAttribute('data-pref-key');
        if (prefKey) {
          resolvePreference(prefKey, opt.getAttribute('data-pref-act'));
          return;
        }
        // ★ 发的是选项自带的 message（完整句子），不是 label、更不是序号。
        //   发"②"这种序号 → 下一轮漏斗再判一次"②" → 四层全判不出 → 再反问 → 死循环。
        sendChat(opt.getAttribute('data-msg'));
        return;
      }

      var item = t.closest('.pixel-chat-item');
      if (item && item.getAttribute('data-task')) {
        SFX.click();
        window.location.hash = '#/task/' + item.getAttribute('data-task');
      }
    });
  }

  if (chatInput) {
    chatInput.addEventListener('keydown', function(e) {
      // 对话里 Enter 就该发送（Shift+Enter 换行）。沿用旧版的 Ctrl+Enter
      // 会把"多说一句"的成本抬高一倍，跟对话形态不符。
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendChat(chatInput.value);
      }
    });
  }
  if (stdDimensions) {
    stdDimensions.addEventListener('keydown', function(e) {
      if (e.key === 'Enter') submitStandard();
    });
  }

  if (btnSubmitStd) btnSubmitStd.addEventListener('click', submitStandard);
  if (btnSubmitChat) btnSubmitChat.addEventListener('click', function() {
    sendChat(chatInput ? chatInput.value : '');
  });
  if (btnRefresh) btnRefresh.addEventListener('click', function() { SFX.click(); loadTasks(); });
  if (btnLogout) btnLogout.addEventListener('click', function() { SFX.click(); Auth.logout(); });

  // 默认落在标准模式：第一个该填的是竞品，不是标题
  if (stdCompetitors) stdCompetitors.focus();
  renderChatLog();   // 对话面板先摆出空态示例，切过去不是一片空白

  loadTasks();
}

var STATUS_MAP = {
  'pending':     { label: '\u7B49\u5F85\u4E2D',   cls: 'pixel-badge-warning' },
  // ★ running 是库里真实存在的状态（实测 4 条），原先漏了 → 卡片和对话框都会
  //   把英文原样透出来。库里的状态全集：pending / running / completed / failed
  //   + 执行中四个阶段（collecting / analyzing / writing / qa）。
  'running':     { label: '\u8FD0\u884C\u4E2D',   cls: 'pixel-badge-warning' },
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

/* API.post 拦截 — 自动记录 task_id 到 localStorage
   ⚠️ 两条路径都要记：表单走 /api/tasks 直接回 task_id；
      对话走 /api/chat，只有 type=task 才有 task_id（answer/clarify 没有）。
      漏掉 chat 的话，对话里建的任务不会出现在下方任务队列里 —— 看着像没建成功。*/
var _origPost = API.post;
API.post = async function(path, body) {
  var result = await _origPost.call(API, path, body);
  if (path === '/api/tasks' && result.task_id) {
    addLocalTaskId(result.task_id);
  }
  if (path === '/api/chat' && result.type === 'task' && result.task_id) {
    addLocalTaskId(result.task_id);
  }
  return result;
};

export function unmount() {}
