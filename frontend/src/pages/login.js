/* login.js — Pixel CRT Terminal Login */

import { API } from '../js/api.js';
import { SFX } from '../js/sounds.js';

export function render() {
  return '' +
  '<div class="pixel-flex pixel-items-center pixel-justify-center" style="min-height:100vh; padding:20px">' +

    '<div style="width:460px">' +

      '<div class="pixel-text-center pixel-mb-lg">' +
        '<div style="font-size:36px; margin-bottom:12px; line-height:1">&#x1f50d;</div>' +
        '<h1 class="pixel-font-display pixel-text-green" style="font-size:18px; letter-spacing:6px">' +
          '\u7ADE \u54C1 \u5206 \u6790 \u7CFB \u7EDF' +
        '</h1>' +
        '<p class="pixel-text-amber pixel-font-mono" style="font-size:15px; margin-top:10px; letter-spacing:3px">AI \u9A71\u52A8\u7684\u591A Agent \u534F\u4F5C\u5206\u6790\u5E73\u53F0 \u00b7 v2.1</p>' +
      '</div>' +

      '<div class="pixel-box" style="padding:32px 28px">' +

        '<label class="pixel-input-label" for="login-username">[ \u7528\u6237\u540D ]</label>' +
        '<input id="login-username" class="pixel-input pixel-mb-md" type="text"' +
               ' placeholder="> \u8F93\u5165\u7528\u6237\u540D..." autocomplete="username" maxlength="50">' +

        '<label class="pixel-input-label" for="login-password">[ \u5BC6\u7801 ]</label>' +
        '<input id="login-password" class="pixel-input pixel-mb-md" type="password"' +
               ' placeholder="> \u8F93\u5165\u5BC6\u7801..." autocomplete="current-password" maxlength="128">' +

        '<div class="pixel-flex pixel-gap-sm pixel-mt-lg">' +
          '<button id="btn-login" class="pixel-btn pixel-flex-1">\u767B \u5F55</button>' +
          '<button id="btn-register" class="pixel-btn-secondary pixel-flex-1">\u6CE8 \u518C</button>' +
        '</div>' +

        '<div id="login-msg" class="pixel-msg pixel-text-center pixel-mt-md"></div>' +

      '</div>' +

      '<p class="pixel-text-center pixel-text-light pixel-font-display pixel-mt-lg" style="font-size:12px; letter-spacing:3px">' +
        '\u7CFB\u7EDF v2.0 <span class="pixel-text-amber">::</span> DeepSeek \u5F15\u64CE' +
      '</p>' +

    '</div>' +

  '</div>';
}

export function mount() {
  var usernameEl = document.getElementById('login-username');
  var passwordEl = document.getElementById('login-password');
  var msgEl = document.getElementById('login-msg');
  var btnLogin = document.getElementById('btn-login');
  var btnRegister = document.getElementById('btn-register');

  function showMsg(text, type) {
    msgEl.textContent = text;
    msgEl.className = 'pixel-msg pixel-text-center pixel-mt-md pixel-animate-fade-in';
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

  async function doLogin() {
    var username = usernameEl.value.trim();
    var password = passwordEl.value;
    if (!username || !password) {
      showMsg('> \u8BF7\u586B\u5199\u7528\u6237\u540D\u548C\u5BC6\u7801', 'error');
      SFX.error();
      return;
    }

    btnLogin.disabled = true;
    btnLogin.textContent = '\u9A8C\u8BC1\u4E2D...';

    try {
      var data = await API.post('/api/auth/login', { username: username, password: password });
      localStorage.setItem('token', data.token);
      localStorage.setItem('username', data.username);
      localStorage.setItem('user_id', data.user_id);
      showMsg('> \u9A8C\u8BC1\u901A\u8FC7 :: \u6B22\u8FCE, ' + username, 'success');
      SFX.startup();
      setTimeout(function() { window.location.hash = '#/dashboard'; }, 600);
    } catch (e) {
      showMsg('> \u9519\u8BEF: ' + e.message, 'error');
      SFX.error();
      btnLogin.disabled = false;
      btnLogin.textContent = '\u767B \u5F55';
    }
  }

  async function doRegister() {
    var username = usernameEl.value.trim();
    var password = passwordEl.value;
    if (!username || !password) {
      showMsg('> \u8BF7\u586B\u5199\u7528\u6237\u540D\u548C\u5BC6\u7801', 'error');
      SFX.error();
      return;
    }
    if (password.length < 6) {
      showMsg('> \u5BC6\u7801\u81F3\u5C11\u9700\u8981 6 \u4F4D', 'error');
      SFX.error();
      return;
    }
    if (!confirm('[\u7CFB\u7EDF] \u6CE8\u518C\u65B0\u8D26\u6237\u3002\u662F\u5426\u7EE7\u7EED\uFF1F')) return;

    btnRegister.disabled = true;
    btnRegister.textContent = '\u6CE8\u518C\u4E2D...';

    try {
      var data = await API.post('/api/auth/register', { username: username, password: password });
      localStorage.setItem('token', data.token);
      localStorage.setItem('username', data.username);
      localStorage.setItem('user_id', data.user_id);
      showMsg('> \u6CE8\u518C\u6210\u529F :: \u8D26\u6237\u5DF2\u521B\u5EFA', 'success');
      SFX.confirm();
      setTimeout(function() { window.location.hash = '#/dashboard'; }, 800);
    } catch (e) {
      showMsg('> \u9519\u8BEF: ' + e.message, 'error');
      SFX.error();
      btnRegister.disabled = false;
      btnRegister.textContent = '\u6CE8 \u518C';
    }
  }

  btnLogin.addEventListener('click', doLogin);
  btnRegister.addEventListener('click', doRegister);

  passwordEl.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') doLogin();
  });
  usernameEl.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') passwordEl.focus();
  });

  usernameEl.focus();
}

export function unmount() {}
