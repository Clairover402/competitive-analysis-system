# -*- coding: utf-8 -*-
"""任务入参校验 —— 「重复竞品/维度拒绝」单元测试（纯函数，不依赖 DB / 网络 / LLM）。

═══════════════════════════════════════════════════════════════════════════════
                     【为什么要有这个测试文件】
═══════════════════════════════════════════════════════════════════════════════

2026-10-13 发现：`competitors=["飞书","飞书"]` 这类输入，从 `_split_i18n_commas`
到 `TaskRequest.model_post_init` 到 `TaskDAO.create`，**没有任何一层去重或拒绝**。

后果有两层，且都不报错：
  ① 成本：analyzer 内层 `for competitor in competitors` 会对重复项多跑一遍
     完整的「检索 + 精排 + LLM 分析」——白花 token（LLM 才是成本最大头）。
  ② 正确性：结果写入 `analysis_results[dim][competitor]` 是 dict 赋值，
     后一次**静默覆盖**前一次 → 报告少一格但用户毫不知情。

修复方案：用户提供的列表出现重复 → HTTP 400 显式拒绝（让用户当场改正）；
         LLM 抽取路径的重复 → 静默保序去重（不因模型副作用 400 用户）。

测试用例：
  1. test_find_duplicates_基本         — 重复值被列出、保序、不重复列
  2. test_find_duplicates_无重复       — 返回空
  3. test_find_duplicates_空与None     — 空/None 安全
  4. test_dedupe_保序去重              — 保留首次出现顺序
  5. test_reject_duplicates_竞品重复   — 抛 400 且 detail 含重复项
  6. test_reject_duplicates_维度重复   — 抛 400
  7. test_reject_duplicates_两者都重复 — 两个问题都报
  8. test_reject_duplicates_无重复放行 — 不抛
  9. test_回归_中文逗号重复也会被拒    — "飞书，飞书" 拆开后仍命中
 10. test_回归_analyzer_消费的是拆分后的列表 — body.competitors 已是拆分结果

运行方式（项目未装 pytest 也能跑）：
    python tests/test_task_validation.py
或用 pytest：
    pytest tests/test_task_validation.py
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import os
import sys

# 允许 `python tests/test_task_validation.py` 直接运行时找到 src 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException

from src.api.routes import (
    TaskRequest,
    _dedupe,
    _find_duplicates,
    _reject_duplicates,
)


# ═══════════════════════════════════════ 1. _find_duplicates ═══════════════════════════════════

def test_find_duplicates_基本():
    assert _find_duplicates(["飞书", "钉钉", "飞书"]) == ["飞书"]
    assert _find_duplicates(["A", "B", "A", "B", "A"]) == ["A", "B"]  # 保序 + 各列一次


def test_find_duplicates_无重复():
    assert _find_duplicates(["飞书", "钉钉"]) == []
    assert _find_duplicates(["唯一"]) == []


def test_find_duplicates_空与None():
    assert _find_duplicates([]) == []
    assert _find_duplicates(None) == []


# ═══════════════════════════════════════ 2. _dedupe ═══════════════════════════════════════

def test_dedupe_保序去重():
    assert _dedupe(["A", "B", "A", "C", "B"]) == ["A", "B", "C"]
    assert _dedupe([]) == []
    assert _dedupe(["飞书"]) == ["飞书"]


# ═══════════════════════════════════════ 3. _reject_duplicates ═══════════════════════════════════

def _raises_400(competitors, dimensions):
    """调用 _reject_duplicates，期望抛 HTTPException(400)；返回 detail 字符串。"""
    try:
        _reject_duplicates(competitors, dimensions)
    except HTTPException as exc:
        assert exc.status_code == 400, f"期望 400，实际 {exc.status_code}"
        return str(exc.detail)
    raise AssertionError("期望抛 HTTPException(400)，但没有抛")


def test_reject_duplicates_竞品重复():
    detail = _raises_400(["飞书", "飞书"], ["功能"])
    assert "竞品重复" in detail and "飞书" in detail


def test_reject_duplicates_维度重复():
    detail = _raises_400(["飞书"], ["功能", "功能"])
    assert "维度重复" in detail and "功能" in detail


def test_reject_duplicates_两者都重复():
    detail = _raises_400(["飞书", "飞书"], ["定价", "定价"])
    assert "竞品重复" in detail and "维度重复" in detail


def test_reject_duplicates_无重复放行():
    # 不应抛异常
    _reject_duplicates(["飞书", "钉钉"], ["功能", "定价"])
    _reject_duplicates(None, None)
    _reject_duplicates([], [])


# ═══════════════════════════════════════ 4. 回归：与拆分逻辑的联动 ═══════════════════════════════════

def test_回归_中文逗号重复也会被拒():
    """前端若把 "飞书，飞书" 当成单个元素传进来，model_post_init 先拆成
    ["飞书","飞书"]，随后 _reject_duplicates 必须命中。"""
    body = TaskRequest(title="t", competitors=["飞书，飞书"], dimensions=["功能"])
    assert body.competitors == ["飞书", "飞书"]  # 已被拆分
    _raises_400(body.competitors, body.dimensions)


def test_回归_analyzer_消费的是拆分后的列表():
    """钉桩：routes.py 里 analyzer 实际消费的 llm_parsed 取自 body.competitors，
    因此校验必须落在**拆分之后**的 body.competitors 上（本测试把这一前提钉住）。"""
    body = TaskRequest(title="t", competitors=["飞书, 钉钉"], dimensions=["功能, 定价"])
    assert body.competitors == ["飞书", "钉钉"]
    assert body.dimensions == ["功能", "定价"]
    _reject_duplicates(body.competitors, body.dimensions)  # 不抛


# ═══════════════════════════════════════ 5. HTTP 层：真的返回 400 ═══════════════════════════════════

def test_http_重复返回400():
    """端到端钉桩：覆盖鉴权与限流后，POST /api/tasks 传重复项必须拿到 400。

    不需要 DB——重复项在写库**之前**就被拒。覆盖鉴权依赖即可绕过 JWT。
    """
    from fastapi.testclient import TestClient
    from src.api import routes
    from src.auth.dependencies import CurrentUser, get_current_user

    class _AlwaysAllow:
        async def acquire(self) -> bool:
            return True

    app = routes.app
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id="u-test", username="u"
    )
    app.state.token_bucket = _AlwaysAllow()
    try:
        client = TestClient(app, raise_server_exceptions=False)
        r = client.post("/api/tasks", json={
            "title": "t", "competitors": ["飞书", "飞书"], "dimensions": ["功能"],
        })
        assert r.status_code == 400, f"期望 400，实际 {r.status_code}: {r.text[:200]}"
        assert "竞品重复" in r.json()["detail"]
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ═══════════════════════════════════════ 直接运行入口 ═══════════════════════════════════════

def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
