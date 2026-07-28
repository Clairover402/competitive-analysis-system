/* ═══════════════════════════════════════════════════════════════════════════
   router.js — Hash 路由引擎
   
   监听 hashchange，匹配路由表，动态 import 页面模块。
   页面模块契约：{ render(params), mount(params), unmount() }
   ═══════════════════════════════════════════════════════════════════════════ */

import { Auth } from './auth.js';

/* ── 路由表 ── */
var routes = [
  { pattern: /^#\/login$/i,                       page: 'login',       auth: false, params: [] },
  { pattern: /^#\/dashboard$/i,                    page: 'dashboard',   auth: true,  params: [] },
  { pattern: /^#\/task\/([^/]+)$/i,                page: 'task-detail', auth: true,  params: ['id'] },
  { pattern: /^#\/report\/([^/]+)\/([^/]+)$/i,     page: 'report',      auth: true,  params: ['taskId', 'reportId'] },
  { pattern: /^#\/monitor$/i,                      page: 'monitor',     auth: true,  params: [] },
];

/** 当前挂载的页面模块 */
var currentPage = null;

/* ── 路由匹配 ── */
function _match(hash) {
  for (var i = 0; i < routes.length; i++) {
    var r = routes[i];
    var m = hash.match(r.pattern);
    if (m) {
      var params = {};
      r.params.forEach(function(key, idx) {
        params[key] = m[idx + 1] || '';
      });
      return { page: r.page, auth: r.auth, params: params };
    }
  }
  return null;
}

/* ── 显示错误页面 ── */
function _showError(pageName, err) {
  var app = document.getElementById('app');
  app.innerHTML = '<div class="flex items-center justify-center" style="min-height:100vh">' +
    '<div class="panel" style="max-width:420px; text-align:center">' +
      '<p style="font-size:24px; margin-bottom:16px;">&#x26A0;</p>' +
      '<p class="text-danger mb-sm font-mono" style="font-size:14px">页面加载失败</p>' +
      '<p class="text-secondary mb-md" style="font-size:12px">' + pageName + '.js</p>' +
      '<button class="btn btn-secondary" onclick="window.location.hash=\'#/dashboard\'">' +
        '&#x2190; 返回仪表盘' +
      '</button>' +
    '</div>' +
  '</div>';
  console.error('路由加载失败:', pageName, err);
}

/* ── 主路由函数 ── */
async function _route() {
  var hash = window.location.hash || '#/dashboard';
  var matched = _match(hash);

  // 未匹配 → 跳仪表盘
  if (!matched) {
    window.location.hash = '#/dashboard';
    return;
  }

  // 鉴权守卫
  if (matched.auth && !Auth.guard()) return;

  // 登录页：已登录直接跳仪表盘
  if (matched.page === 'login' && Auth.isLoggedIn()) {
    window.location.hash = '#/dashboard';
    return;
  }

  try {
    // 动态加载页面模块
    var mod = await import('../pages/' + matched.page + '.js');

    // 卸载旧页面
    if (currentPage && currentPage.unmount) {
      currentPage.unmount();
    }

    // 渲染新页面
    var app = document.getElementById('app');
    app.innerHTML = mod.render(matched.params);

    // 入口动画
    app.firstElementChild && app.firstElementChild.classList.add('animate-fade-in');

    // 挂载事件
    if (mod.mount) {
      mod.mount(matched.params);
    }

    currentPage = mod;
  } catch (err) {
    _showError(matched.page, err);
  }
}

/* ── 启动 ── */
function init() {
  window.addEventListener('hashchange', _route);
  _route();
}

/* ── 编程式导航 ── */
function navigate(hash) {
  window.location.hash = hash;
}

export var Router = { init: init, navigate: navigate };
