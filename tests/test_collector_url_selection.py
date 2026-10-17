# -*- coding: utf-8 -*-
"""Collector URL 选取（步骤3）—— 单元测试（纯本地，不依赖 DB / 网络 / LLM）。

═══════════════════════════════════════════════════════════════════════════════
                        【为什么要有这个测试文件】
═══════════════════════════════════════════════════════════════════════════════

2026-10-14 只读侦察（docs/golden_step3_cp_recon.py，读 runs/20261013-230903）发现：

  P0   URL 选取是「按 query 顺序拼接后直接 [:8]」，而 `all_queries` 是按
       「竞品 → 关键词」拼的 → **第一条 query 的结果吃掉整个配额**。
       实测：38 个 (任务,竞品) 组合里 32 个正好采满 8 页（上限 100% 触顶），
       但其中 13 个组合「官方域零覆盖」（41%）。
       典型：golden_002「语雀」采到的 8 页来自 8 个**不同第三方域名**，
       官网 yuque.com 一页都没有 → 召回池自然没有它 → source_coverage=0。

  P0'  所以真瓶颈不是「配额 8 太小」，而是「怎么选」——单纯把 8 提到 12
       只会多抓第三方页。修法 = 「每 query 内官网优先 + 跨 query 轮转」。

  P1   官网域从哪来：复用关键词生成的那次 LLM 调用顺带产出
       （零额外调用），且**只作排序提示、不做过滤**——猜错最坏等于没提示。
       同时 `_extract_json_payload` 必须同时认 `{...}` 与 `[...]`
       （旧实现只认 `[...]`，格式一变就会静默降级成模板 query）。

测试用例：
  1.  test_轮转_第一条query不再吃掉全部配额     — 钉死 P0
  2.  test_轮转_配额与query数无关时取满         — 边界：cap < 组数 / cap > 候选数
  3.  test_官网优先_组内置顶而非全局置顶        — 钉死 P0'（防止退回「8页同一个站」）
  4.  test_官网优先_子域命中与猜错无害          — 归一化 + 「猜错等于没提示」
  5.  test_选取_空输入与cap边界                 — 安全边界
  6.  test_候选缺query字段时退化但可用          — 防御：老调用方可能不带 query
  7.  test_extract_json_payload_两种形态都认     — 钉死 P1
  8.  test_回归_不再出现顺序截断前8             — 扫源码，防回潮

运行方式（项目未装 pytest 也能跑）：
    ./.venv/Scripts/python.exe tests/test_collector_url_selection.py
或用 pytest：
    pytest tests/test_collector_url_selection.py
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.agents.collector import (  # noqa: E402
    _extract_json_payload,
    _is_official_host,
    select_competitor_urls,
)


def _c(url: str, query: str = "") -> dict:
    return {"url": url, "query": query}


# ─────────────────────────────────────────────────────────────────────────────
# 1~2. 轮转
# ─────────────────────────────────────────────────────────────────────────────

def test_轮转_第一条query不再吃掉全部配额():
    """P0：旧实现下 q1 的 8 条会占满配额，q2/q3 一条都进不来。

    修复后：每个 query 至少先各拿 1 条，再按轮次加深。
    """
    candidates = [_c(f"https://s{i}.com/p{j}", f"q{i}") for i in range(3) for j in range(10)]
    sel = select_competitor_urls(candidates, cap=8)
    assert len(sel) == 8
    used_queries = {c["query"] for c in sel}
    assert used_queries == {"q0", "q1", "q2"}, f"有 query 被完全饿死：{used_queries}"
    # 轮转语义：q0,q1,q2,q0,q1,q2,q0,q1
    assert [c["query"] for c in sel] == ["q0", "q1", "q2", "q0", "q1", "q2", "q0", "q1"]
    # 与旧行为对照：旧实现只会取到 q0 的 8 条
    assert not all(c["query"] == "q0" for c in sel)


def test_轮转_配额与query数无关时取满():
    """边界：cap 小于 query 数时取前 cap 个 query；cap 大于候选数时能取多少取多少。"""
    # 5 个 query、各 1 条，cap=8 → 只能取 5 条（不伪造）
    five = [_c(f"https://s{i}.com/a", f"q{i}") for i in range(5)]
    assert len(select_competitor_urls(five, cap=8)) == 5
    # cap=3 < query 数 5 → 取前 3 个 query 各 1 条
    # （q0..q4 首次出现顺序即 query 顺序）
    sel3 = select_competitor_urls(five, cap=3)
    assert [c["query"] for c in sel3] == ["q0", "q1", "q2"]
    # 组大小不均：q0 有 3 条、q1 有 1 条，cap=4 → q0,q1,q0,q0
    uneven = [
        _c("https://s.com/a0", "q0"), _c("https://s.com/a1", "q0"),
        _c("https://s.com/a2", "q0"), _c("https://s.com/b0", "q1"),
    ]
    assert [c["query"] for c in select_competitor_urls(uneven, cap=4)] == ["q0", "q1", "q0", "q0"]


# ─────────────────────────────────────────────────────────────────────────────
# 3~4. 官网优先
# ─────────────────────────────────────────────────────────────────────────────

def test_官网优先_组内置顶而非全局置顶():
    """P0'：官网优先必须发生在「各自 query 组内」，不能全局置顶。

    若全局置顶，像飞书这种 50+ 个 feishu.cn 结果的竞品会退回
    「8 页全同一个站」，丢掉多 query 的信息量——那正是我们要修的病。
    """
    candidates = [
        _c("https://t1.com/a", "q0"), _c("https://t1.com/b", "q0"),
        _c("https://me.com/a", "q0"),          # q0 的官网
        _c("https://t2.com/x", "q1"), _c("https://t2.com/y", "q1"),
        _c("https://me.com/z", "q1"),          # q1 的官网
    ]
    sel = select_competitor_urls(candidates, cap=4, official_domains=["me.com"])
    # q0 组内 me.com 置顶 → 第 1 条；q1 组内 me.com 置顶 → 第 2 条；再轮转取各组第 2 条
    assert [c["url"] for c in sel] == [
        "https://me.com/a", "https://me.com/z", "https://t1.com/a", "https://t2.com/x",
    ]
    # 关键：不能是「两条官网 + 随便两条」——两组各出了官网，说明是组内优先
    assert [c["query"] for c in sel] == ["q0", "q1", "q0", "q1"]


def test_官网优先_子域命中与猜错无害():
    """官网判定含子域；官网域猜错（不在候选里）时行为等价于纯轮转。"""
    assert _is_official_host("https://www.feishu.cn/p", ["feishu.cn"]) is True
    assert _is_official_host("https://open.feishu.cn/d", ["feishu.cn"]) is True
    assert _is_official_host("https://notfeishu.cn/x", ["feishu.cn"]) is False
    assert _is_official_host("", ["feishu.cn"]) is False
    # 提示写成 www. 前缀也能命中
    assert _is_official_host("https://www.feishu.cn/p", ["www.feishu.cn"]) is True

    candidates = [_c(f"https://s{i}.com/p", f"q{i}") for i in range(4)]
    base = [c["url"] for c in select_competitor_urls(candidates, cap=4)]
    wrong = [c["url"] for c in select_competitor_urls(
        candidates, cap=4, official_domains=["hallucinated.invalid"])]
    assert base == wrong, "官网域猜错不得改变选取结果（仅作提示、不做过滤）"


# ─────────────────────────────────────────────────────────────────────────────
# 5~6. 边界与防御
# ─────────────────────────────────────────────────────────────────────────────

def test_选取_空输入与cap边界():
    assert select_competitor_urls([], cap=8) == []
    assert select_competitor_urls([_c("https://a.com/x", "q0")], cap=0) == []
    assert select_competitor_urls([_c("https://a.com/x", "q0")], cap=-1) == []
    # 候选不足 cap → 全取
    one = [_c("https://a.com/x", "q0"), _c("https://b.com/y", "q1")]
    assert len(select_competitor_urls(one, cap=8)) == 2


def test_候选缺query字段时退化但可用():
    """防御：老调用方/历史候选可能不带 query → 归入同一组，顺序即原顺序。"""
    candidates = [{"url": f"https://s{i}.com/p"} for i in range(10)]
    sel = select_competitor_urls(candidates, cap=8)
    assert [c["url"] for c in sel] == [f"https://s{i}.com/p" for i in range(8)]


# ─────────────────────────────────────────────────────────────────────────────
# 7~8. LLM 输出形态 + 防回潮
# ─────────────────────────────────────────────────────────────────────────────

def test_extract_json_payload_两种形态都认():
    """P1：关键词生成改为输出对象后，剥离函数必须同时认 {} 与 []。"""
    assert _extract_json_payload('{"queries": ["a"], "official_domains": ["b.com"]}') == \
        '{"queries": ["a"], "official_domains": ["b.com"]}'
    assert _extract_json_payload('["a", "b"]') == '["a", "b"]'
    # markdown 围栏
    assert _extract_json_payload('```json\n{"queries": ["a"]}\n```') == '{"queries": ["a"]}'
    assert _extract_json_payload('```\n["a"]\n```') == '["a"]'
    # 前后有解释文字
    assert _extract_json_payload('好的，结果如下：["a", "b"] 以上') == '["a", "b"]'
    assert _extract_json_payload('输出：{"queries": ["a"]}（完）') == '{"queries": ["a"]}'
    # 无 JSON → 原样返回（交给 json.loads 抛错并触发重试/降级）
    assert _extract_json_payload('完全不是 JSON') == '完全不是 JSON'
    assert _extract_json_payload('') == ''


def test_回归_不再出现顺序截断前8():
    """扫源码防回潮：URL 选取不得回到「按顺序 [:8] 截断」。"""
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "src", "agents", "collector.py",
    )
    with open(path, encoding="utf-8") as f:
        src = f.read()
    assert 'competitor_urls[c][:8]' not in src, (
        "collector.py 又出现了「按顺序取前 8」——这正是官方域零覆盖的根因"
    )
    assert "for u in urls[:8]" not in src, (
        "抓取循环又回到了顺序截断；应遍历 select_competitor_urls 的结果"
    )
    assert "select_competitor_urls" in src, "选取函数被删了？"


# ─────────────────────────────────────────────────────────────────────────────
# 无 pytest 时的兜底运行器
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cases = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in cases:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception as e:  # noqa: BLE001 —— 测试运行器需要捕获全部异常
            failed += 1
            print(f"  FAIL  {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(cases) - failed}/{len(cases)} 通过")
    sys.exit(1 if failed else 0)
