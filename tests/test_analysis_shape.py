# -*- coding: utf-8 -*-
"""分析结果单元格形态归一 —— 单元测试（纯函数，不依赖 DB / 网络 / LLM）。

═══════════════════════════════════════════════════════════════════════════════
                     【为什么要有这个测试文件】
═══════════════════════════════════════════════════════════════════════════════

2026-10-12 发现一个静默失真 bug：analyzer 的 per_dimension.has_data 恒为 True。

  根因：单元格从 str 改成 {"conclusion", "source_url"} 后，
        统计代码仍写 str(text).startswith("[分析失败]")，
        而 str(dict) 以 "{'conclusion'" 开头 → 两个 startswith 恒 False
        → 过滤条件恒 True → 有效竞品数恒等于全部竞品。

  为什么没被发现：
    ① 它只影响 agent_logs 里的审计信号，主流程不报错、不抛异常；
    ② 这段统计逻辑内联在 analyzer_agent 里（要起 DB + LLM 才能跑到），没有单测。
    → 所以修复方案不只是改一行，而是把口径抽成纯函数并补上本测试。

测试用例：
  1. test_conclusion_text_兼容两种形态          — dict / str / None / 非字符串
  2. test_is_placeholder_判定                  — startswith 语义
  3. test_has_missing_marker_判定              — in 语义（长结论里夹标记）
  4. test_cell_has_data                        — 唯一判定口径
  5. test_summarize_per_dimension_全结构化      — 回归：新形态必须统计正确
  6. test_summarize_per_dimension_混合旧形态    — 兼容历史 checkpoint 的裸字符串
  7. test_回归_旧写法为什么会恒为True           — 把 bug 的复现条件钉在测试里
  8. test_summarize_per_dimension_防御非dict    — 维度级异常输入不抛异常

运行方式（项目未装 pytest 也能跑）：
    python tests/test_analysis_shape.py
或用 pytest：
    pytest tests/test_analysis_shape.py
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import os
import sys

# 允许 `python tests/test_analysis_shape.py` 直接运行时找到 src 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.agents.analysis_shape import (
    conclusion_text,
    has_missing_marker,
    is_placeholder,
    cell_has_data,
    summarize_per_dimension,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1~4：单元格级取值与判定
# ─────────────────────────────────────────────────────────────────────────────

def test_conclusion_text_兼容两种形态():
    # 形态 A：结构化对象 —— 只取 conclusion，忽略 source_url
    assert conclusion_text({"conclusion": "定价 198 元", "source_url": "https://a.com"}) == "定价 198 元"
    # 形态 B：旧版自由文本
    assert conclusion_text("定价 198 元（来源: a.com）") == "定价 198 元（来源: a.com）"
    # 空值/None/非字符串一律兜底成字符串，永不返回 None（下游 len()/startswith 安全）
    assert conclusion_text(None) == ""
    assert conclusion_text("") == ""
    assert conclusion_text({"source_url": "https://a.com"}) == ""
    assert conclusion_text(123) == "123"
    assert conclusion_text(True) == "True"
    # 注意 Falsy 语义：`val or ""` 会把 0/False 当成空值 —— 单元格不应出现这些值，
    # 出现时按"无结论"处理比 str(0)="0" 更安全（避免把 0 当成结论长度 1 的正文）。
    assert conclusion_text(False) == ""
    assert conclusion_text(0) == ""


def test_is_placeholder_判定():
    assert is_placeholder("[数据不足] 未找到相关信息") is True
    assert is_placeholder("[分析失败] LLM返回空响应") is True
    assert is_placeholder("[待验证]") is True
    assert is_placeholder("  定价 198 元  ") is False
    assert is_placeholder("") is False
    # startswith 语义：标记出现在正文中间不算"整格是占位"
    assert is_placeholder("定价 198 元 [待验证]") is False


def test_has_missing_marker_判定():
    # in 语义：正文里夹了标记也算（Quality 组装数据缺失清单用这个）
    assert has_missing_marker("定价 198 元 [待验证]") is True
    assert has_missing_marker("[数据不足] 未找到相关信息") is True
    assert has_missing_marker("定价 198 元，来源官网") is False
    assert has_missing_marker("") is False


def test_cell_has_data():
    assert cell_has_data({"conclusion": "定价 198 元", "source_url": "https://a.com"}) is True
    assert cell_has_data({"conclusion": "", "source_url": "https://a.com"}) is False
    assert cell_has_data({"conclusion": "[数据不足] 未找到相关信息", "source_url": ""}) is False
    assert cell_has_data({"conclusion": "[分析失败] LLM返回格式异常", "source_url": ""}) is False
    # 旧形态
    assert cell_has_data("定价 198 元") is True
    assert cell_has_data("[分析失败] 未知错误") is False
    assert cell_has_data("") is False


# ─────────────────────────────────────────────────────────────────────────────
# 5~8：维度级汇总
# ─────────────────────────────────────────────────────────────────────────────

def test_summarize_per_dimension_全结构化():
    """回归用例：三条失败分支也产出 dict 形态后的真实数据。"""
    analysis_results = {
        "定价策略": {
            "飞书": {"conclusion": "企业版¥200/人/月", "source_url": "https://feishu.cn/p"},
            "钉钉": {"conclusion": "[数据不足] 未找到相关信息", "source_url": ""},
            "企微": {"conclusion": "专业版¥180/人/年", "source_url": "https://work.weixin.qq.com"},
        },
        "功能对比": {
            "飞书": {"conclusion": "[分析失败] LLM返回空响应", "source_url": ""},
            "钉钉": {"conclusion": "[分析失败] LLM返回空响应", "source_url": ""},
            "企微": {"conclusion": "[分析失败] LLM返回空响应", "source_url": ""},
        },
    }

    per_dim = summarize_per_dimension(analysis_results)

    # 定价策略：3 个竞品里 2 个有实质结论（钉钉是 [数据不足]）
    assert per_dim["定价策略"] == {"competitors_analyzed": 2, "has_data": True}
    # 功能对比：整维度失败 → 0 个有效，has_data 必须是 False
    # （这正是修复前恒为 True、Grafana 上看不出"哪个维度没数据"的地方）
    assert per_dim["功能对比"] == {"competitors_analyzed": 0, "has_data": False}


def test_summarize_per_dimension_混合旧形态():
    """兼容历史 checkpoint 恢复出来的旧形状（裸字符串单元格）。"""
    analysis_results = {
        "美术风格": {
            "飞书": "低多边形风格，主色 #4A90D9（来源: feishu.cn）",
            "钉钉": "[数据不足] 未检索到相关网页内容",
            "企微": "[待验证] 疑似采用扁平化设计",
        },
    }

    per_dim = summarize_per_dimension(analysis_results)

    assert per_dim["美术风格"] == {"competitors_analyzed": 1, "has_data": True}


def test_回归_旧写法为什么会恒为True():
    """把 bug 的复现条件钉在测试里 —— 防止有人再写回 str(val).startswith(...)。

    旧写法：
        not str(text).startswith("[分析失败]") and not str(text).startswith("[数据不足]")

    单元格是 dict 时，str(dict) 以 "{'conclusion'" 开头，
    两个 startswith 恒 False → 条件恒 True → 所有竞品都被算成"有数据"。
    """
    cells = [
        {"conclusion": "[数据不足] 未找到相关信息", "source_url": ""},
        {"conclusion": "[分析失败] LLM返回空响应", "source_url": ""},
        {"conclusion": "", "source_url": ""},
    ]

    # 旧写法：无论单元格内容如何，都判成"有数据"（这就是 bug）
    legacy = [
        (not str(c).startswith("[分析失败]")) and (not str(c).startswith("[数据不足]"))
        for c in cells
    ]
    assert legacy == [True, True, True], "如果这里不是全 True，说明 str(dict) 的行为变了，需重新评估旧 bug 描述"

    # 新口径：三个单元格都判成"无数据"
    assert [cell_has_data(c) for c in cells] == [False, False, False]


def test_summarize_per_dimension_防御非dict():
    """维度级异常输入（旧代码可能把整维度写成 {"error": ...}）不应抛异常。"""
    per_dim = summarize_per_dimension({"坏维度": "not a dict"})
    assert per_dim["坏维度"] == {"competitors_analyzed": 0, "has_data": False}
    # 空输入
    assert summarize_per_dimension({}) == {}
    assert summarize_per_dimension(None) == {}


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
