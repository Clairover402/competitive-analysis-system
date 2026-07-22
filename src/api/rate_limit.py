"""三层限流 — TokenBucket 入口 QPS + AgentSemaphore 并发 + LLMRateLimiter 滑动窗口。

═══════════════════════════════════════════════════════════════════════════════
                        【L5 面试 — 限流架构】
═══════════════════════════════════════════════════════════════════════════════

  三层限流不同职责：

  Layer 1: TokenBucket (入口 QPS)
    — 全局 100 QPS，挡在 POST /api/tasks 之前
    — 频率太高直接 429，不消耗下游资源
    — 算法：令牌桶（平滑突发，非严格计数）

  Layer 2: AgentSemaphore (并发控制)
    — 最多 3 个 Agent 同时执行，避免 PG 连接池耗尽
    — asyncio.Semaphore 原生协程

  Layer 3: LLMRateLimiter (LLM API 限流)
    — 60 RPM 滑动窗口，防 API 账号被 rate limit
    — 滑动窗口 ≠ 固定窗口——防"跨窗口突刺"

  ▸ 与 harness/guard.py 中的 TokenBucket 区别：
    — api/rate_limit: HTTP 入口全局 QPS
    — harness/guard: 单 Agent 调用的频控（Agent 级，在 A2A 通路上）
    — 两者共存，各管各层
"""

from __future__ import annotations

import asyncio
import time


# ═════════════════════════════════════════════════════════════════════════════
# §1 TokenBucket — 令牌桶入口限流（Layer 1: 全局 QPS）
# ═════════════════════════════════════════════════════════════════════════════

class TokenBucket:
    """令牌桶入口限流器。

    算法：
      — 桶容量 = capacity（默认 100）
      — 每秒补充 refill_rate 个 token（默认 10 tokens/s）
      — 每次请求消耗 1 token
      — token 不足 → 请求被拒绝（429 Too Many Requests）

    【L4 工程】asyncio.Lock 保护 tokens + last_refill 的读-改-写原子性。
    不用 float 精度问题——tokens 是浮点数，1.0 就是 1 个完整请求。

    【L5 面试追问】为什么选令牌桶不选固定窗口？
    → "固定窗口在窗口边界有突刺问题：59 秒发 100 个 + 下一秒再发 100 个
       = 2 秒 200 个请求。令牌桶的 refill 是平滑的，突发被桶容量 cap 住。"
    """

    def __init__(self, capacity: int = 100, refill_rate: float = 10.0):
        self.capacity = float(capacity)
        self.tokens = float(capacity)          # 初始满桶
        self.refill_rate = refill_rate         # tokens per second
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> bool:
        """获取一个 token，成功返回 True，被限流返回 False。

        Returns:
            True: 放行（令牌足够）
            False: 拒绝（令牌不足，需返回 429）
        """
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            # ── 补充令牌 ──
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
            self.last_refill = now

            # ── 尝试消耗 ──
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True
            return False

    @property
    def available(self) -> float:
        """当前可用 token 数（调试用）。"""
        return self.tokens


# ═════════════════════════════════════════════════════════════════════════════
# §2 AgentSemaphore — Agent 并发控制（Layer 2: 资源保护）
# ═════════════════════════════════════════════════════════════════════════════

class AgentSemaphore:
    """Agent 并发控制——基于 asyncio.Semaphore。

    限制同时执行的 Agent 调用数，防止：
      — PG 连接池被耗尽（10 个连接，3 并发 = 安全余量 7）
      — LLM API 突发调用（3 并发 → 60 RPM 可控）
      — 系统内存膨胀（每个 Agent 调用持有 httpx session + LLM context）

    【L5 决策】max_concurrent=3 不是魔数：
      — PG 连接池 max=10，3 并发 → 余量 7 给查询/日志/SSE 轮询
      — LLM API 60 RPM，3 并发 × 20 req/min ≈ 正好卡在 60 线上
      — 更高并发增加收益递减（限速在 LLM 和网络 IO，不在并发度）
    """

    def __init__(self, max_concurrent: int = 3):
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._active_count = 0

    async def acquire(self) -> None:
        """获取信号量（阻塞直到有槽位）。"""
        await self._semaphore.acquire()
        self._active_count += 1

    def release(self) -> None:
        """释放信号量。"""
        self._semaphore.release()
        self._active_count = max(0, self._active_count - 1)

    @property
    def active_count(self) -> int:
        """当前活跃的 Agent 调用数。"""
        return self._active_count


# ═════════════════════════════════════════════════════════════════════════════
# §3 LLMRateLimiter — API 限流（Layer 3: 滑动窗口 RPM）
# ═════════════════════════════════════════════════════════════════════════════

class LLMRateLimiter:
    """LLM API 限流器——滑动窗口算法。

    核心原理：
      维护过去 60 秒内所有请求的时间戳列表。
      每次新请求前：
        1. 清理 60 秒窗口外的过期时间戳
        2. 如果窗口内请求数 >= max_rpm → 计算需要等待多久
        3. 异步等待到期时间
        4. 再次清理后，追加本次请求时间戳

    【L5 面试追问】为什么滑动窗口优于固定窗口？
    固定窗口问题：
      09:00:59 → 发 60 个请求（第一分钟窗口）
      09:01:00 → 发 60 个请求（第二分钟窗口）
      → 2 秒内 120 个请求，LLM API 直接 429

    滑动窗口：
      09:00:59 → 60 个请求全在 60 秒窗口内 → 第 61 个被限
      09:01:01 → 窗口内只剩 1 个请求（09:00:01 的那个已过期）→ 可发 59 个
      → 不会在窗口边界突刺

    时间戳列表做 O(n) 清理而不是用 deque——
    因为 n ≤ 60，O(60) ≈ 0。不用为 60 个元素引入双端队列。
    """

    def __init__(self, max_rpm: int = 60):
        self.max_rpm = max_rpm
        self.requests: list[float] = []  # 时间戳列表
        self._lock = asyncio.Lock()

    async def wait_if_needed(self) -> None:
        """检查滑动窗口，必要时异步等待。

        调用方：
          await limiter.wait_if_needed()
          # 此时可以安全调用 LLM API
        """
        async with self._lock:
            now = time.monotonic()
            window_start = now - 60.0

            # ── 清理过期时间戳 ──
            self.requests = [t for t in self.requests if t > window_start]

            # ── 是否触发限流 ──
            if len(self.requests) >= self.max_rpm:
                # 最早请求何时过期 = 最早请求 + 60 秒 - 当前时间
                oldest = self.requests[0]
                wait_time = oldest + 60.0 - now
                if wait_time > 0:
                    await asyncio.sleep(wait_time)
                    # 等完后再清理一遍（等完后的窗口边界可能还有其他请求过期）
                    now = time.monotonic()
                    self.requests = [
                        t for t in self.requests if t > now - 60.0
                    ]

            # ── 记录本次请求 ──
            self.requests.append(time.monotonic())

    @property
    def current_count(self) -> int:
        """当前窗口内的请求数（调试用）。"""
        return len(self.requests)
