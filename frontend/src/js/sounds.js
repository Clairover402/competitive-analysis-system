/* ═══════════════════════════════════════════════════════════════════════════
   sounds.js — Web Audio 音效引擎
   
   使用 Web Audio API OscillatorNode 方波生成 8-bit 风格音效。
   静默降级：浏览器不支持 Web Audio 时全部静默，不报错。
   ═══════════════════════════════════════════════════════════════════════════ */

/** 获取或创建共享 AudioContext（延迟初始化，兼容浏览器自动播放策略） */
function getCtx() {
  if (!getCtx._ctx) {
    try {
      getCtx._ctx = new (window.AudioContext || window.webkitAudioContext)();
    } catch {
      getCtx._ctx = null;
    }
  }
  return getCtx._ctx;
}

/** 播放一个音（自动创建 oscillator → 连接 gain → 连接 destination） */
function playNote(freq, duration, type = 'square', volume = 0.08, delay = 0) {
  const ctx = getCtx();
  if (!ctx) return;

  const osc = ctx.createOscillator();
  const gain = ctx.createGain();

  osc.type = type;
  osc.frequency.value = freq;
  gain.gain.setValueAtTime(volume, ctx.currentTime + delay);
  gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + delay + duration);

  osc.connect(gain);
  gain.connect(ctx.destination);

  osc.start(ctx.currentTime + delay);
  osc.stop(ctx.currentTime + delay + duration);
}

/** 播放音序（传入 [freq, duration, delay][] 数组） */
function playSequence(notes) {
  notes.forEach(function(n) {
    playNote(n[0], n[1], 'square', 0.06, n[2] || 0);
  });
}

export const SFX = {
  /** 短促轻音 — 菜单切换、点击反馈 */
  blip() {
    playNote(800, 0.04);
  },

  /** 操作成功 — 上行两音 */
  success() {
    playSequence([
      [523, 0.08, 0],
      [784, 0.12, 0.08],
    ]);
  },

  /** 操作失败 — 下行两音 */
  error() {
    playSequence([
      [400, 0.1, 0],
      [200, 0.15, 0.1],
    ]);
  },

  /** 确认音 — 双短音 */
  confirm() {
    playSequence([
      [660, 0.06, 0],
      [880, 0.06, 0.08],
    ]);
  },

  /** 启动音 — 五音上行琶音（登录成功） */
  startup() {
    playSequence([
      [440, 0.08, 0],
      [554, 0.08, 0.08],
      [659, 0.08, 0.16],
      [784, 0.08, 0.24],
      [880, 0.15, 0.32],
    ]);
  },

  /** 点击反馈 — 极短 click */
  click() {
    playNote(1200, 0.02, 'square', 0.04);
  },
};
