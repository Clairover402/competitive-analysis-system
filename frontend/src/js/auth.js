/* ═══════════════════════════════════════════════════════════════════════════
   auth.js — 鉴权守卫
   
   JWT token 存储在 localStorage，前端自行解析 payload 检查过期。
   ═══════════════════════════════════════════════════════════════════════════ */

/** 解析 JWT payload（不验证签名，仅提取信息） */
function _parseToken(token) {
  try {
    var parts = token.split('.');
    if (parts.length !== 3) return null;
    // payload 是 base64url 编码的 JSON
    return JSON.parse(atob(parts[1]));
  } catch(e) {
    return null;
  }
}

export const Auth = {
  /** 是否已登录 */
  isLoggedIn() {
    var token = localStorage.getItem('token');
    if (!token) return false;
    var payload = _parseToken(token);
    if (!payload) return false;
    // 检查过期时间（exp 是 Unix 时间戳，单位秒）
    if (payload.exp) {
      var now = Math.floor(Date.now() / 1000);
      if (payload.exp < now) {
        // token 已过期，清除
        localStorage.removeItem('token');
        localStorage.removeItem('username');
        localStorage.removeItem('user_id');
        return false;
      }
    }
    return true;
  },

  /** 获取用户名 */
  getUsername() {
    return localStorage.getItem('username') || '';
  },

  /** 获取用户 ID */
  getUserId() {
    return localStorage.getItem('user_id') || '';
  },

  /** 鉴权守卫：未登录 → 跳转登录页 */
  guard() {
    if (!this.isLoggedIn()) {
      window.location.hash = '#/login';
      return false;
    }
    return true;
  },

  /** 退出登录 */
  logout() {
    localStorage.removeItem('token');
    localStorage.removeItem('username');
    localStorage.removeItem('user_id');
    window.location.hash = '#/login';
  },
};
