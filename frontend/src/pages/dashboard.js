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
            '<button id="btn-new-chat" class="pixel-chat-new" type="button">+ \u65B0\u5BF9\u8BDD</button>' +
          '</div>' +
          '<div id="chat-log" class="pixel-chat-log"></div>' +
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
  var btnSubmitStd = document.getElementById('btn-submit-std');
  var btnSubmitChat = document.getElementById('btn-submit-chat');
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

  function renderChatMessage(m) {
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
    return '<div class="pixel-chat-row"><div class="pixel-chat-bubble bot">' +
      '<div class="pixel-chat-meta">' + esc(label) + esc(hit) + esc(conf) + '</div>' +
      '<div>' + esc(m.summary || '没有结果。') + '</div>' +
      renderItems(m.intent, m.items) +
      '</div></div>';
  }

  function renderChatLog() {
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
    chatLog.innerHTML = chatMessages.map(renderChatMessage).join('');
    chatLog.scrollTop = chatLog.scrollHeight;
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
    };
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
      renderChatLog();
      if (chatInput) chatInput.focus();
    });
  }

  if (chatLog) {
    chatLog.addEventListener('click', function(e) {
      var t = e.target;
      if (!t || !t.closest) return;

      // 来源链接要能直接打开，别被下面的"点条目跳任务"抢先拦截
      if (t.closest('a')) return;

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
