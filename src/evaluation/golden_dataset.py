# -*- coding: utf-8 -*-
"""Golden Dataset — 离线评估黄金标准数据集。

═══════════════════════════════════════════════════════════════════════════════
                        【L4 工程 — Golden Dataset 设计原则】
═══════════════════════════════════════════════════════════════════════════════

  1. 三级难度分档（easy/medium/hard）覆盖不同场景应力
  2. expected 字段用于自动化断言——测试结果和预期值可直接对比
  3. 不依赖真实网络——所有 expected 都是结构化的，不存储外部 URL
  4. 与系统真实竞品/维度正交——name + dimensions 来自业务场景，非编造

  为什么需要 Golden Dataset？
  — 评估不能只在 CI 里跑一遍 E2E 就说"通过"
  — 必须有标准答案级别的对照集，验证输出是否符合预期覆盖率/关键词/引用
  — 类比 ML 里的 test set：不是看一次结果，是持续回归的标尺
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from typing import TypedDict


class GoldenCaseExpectedQuality(TypedDict):
    """expected.quality_threshold 结构。"""
    完整性: int  # 0-100，是否覆盖所有维度
    准确性: int  # 0-100，数据来源是否可靠


class GoldenCaseExpected(TypedDict):
    """单个 Golden Case 的 expected 字段。"""
    competitors_covered: int          # 至少覆盖几个竞品
    dimensions_covered: int           # 至少覆盖几个维度
    required_keywords: list[str]      # 报告中必须出现的关键词
    min_evidence_count: int           # 最少引用条数
    quality_threshold: GoldenCaseExpectedQuality  # 质检得分门槛


class GoldenCase(TypedDict):
    """单个 Golden Dataset 条目。"""
    task_id: str                      # 唯一标识
    title: str                        # 任务标题
    competitors: list[str]            # 竞品列表
    dimensions: list[str]             # 维度列表
    difficulty: str                   # easy / medium / hard
    expected: GoldenCaseExpected      # 期望结果


# ═════════════════════════════════════════════════════════════════════════════
# Golden Dataset — 10 条，分三档
# ═════════════════════════════════════════════════════════════════════════════

GOLDEN_DATASET: list[GoldenCase] = [
    # ────────────────────────── EASY (4 条) ────────────────────────────
    # Easy = 竞品明确 + 维度常见 + 覆盖面窄，Pipeline 直达
    {
        "task_id": "golden_001",
        "title": "飞书基础功能分析",
        "competitors": ["飞书", "钉钉"],
        "dimensions": ["功能", "定价"],
        "difficulty": "easy",
        "expected": {
            "competitors_covered": 2,
            "dimensions_covered": 2,
            "required_keywords": ["飞书", "钉钉", "定价", "功能"],
            "min_evidence_count": 4,
            "quality_threshold": {"完整性": 60, "准确性": 60},
        },
    },
    {
        "task_id": "golden_002",
        "title": "Notion 市场定位分析",
        "competitors": ["Notion", "语雀"],
        "dimensions": ["市场", "用户"],
        "difficulty": "easy",
        "expected": {
            "competitors_covered": 2,
            "dimensions_covered": 2,
            "required_keywords": ["Notion", "语雀", "市场", "用户"],
            "min_evidence_count": 3,
            "quality_threshold": {"完整性": 60, "准确性": 60},
        },
    },
    {
        "task_id": "golden_003",
        "title": "腾讯文档 vs 石墨文档功能对比",
        "competitors": ["腾讯文档", "石墨文档"],
        "dimensions": ["功能"],
        "difficulty": "easy",
        "expected": {
            "competitors_covered": 2,
            "dimensions_covered": 1,
            "required_keywords": ["腾讯文档", "石墨文档", "功能"],
            "min_evidence_count": 2,
            "quality_threshold": {"完整性": 55, "准确性": 55},
        },
    },
    {
        "task_id": "golden_004",
        "title": "WPS Office 定价分析",
        "competitors": ["WPS Office", "Microsoft 365"],
        "dimensions": ["定价"],
        "difficulty": "easy",
        "expected": {
            "competitors_covered": 2,
            "dimensions_covered": 1,
            "required_keywords": ["WPS", "Microsoft", "定价", "价格"],
            "min_evidence_count": 2,
            "quality_threshold": {"完整性": 55, "准确性": 55},
        },
    },

    # ──────────────────────── MEDIUM (3 条) ──────────────────────────
    # Medium = 多竞品/多维度，走 Pipeline 但需要 richer 的采集和分析
    {
        "task_id": "golden_005",
        "title": "三大协同办公软件全维度对比",
        "competitors": ["飞书", "钉钉", "企业微信"],
        "dimensions": ["功能", "定价", "市场", "用户"],
        "difficulty": "medium",
        "expected": {
            "competitors_covered": 3,
            "dimensions_covered": 4,
            "required_keywords": ["飞书", "钉钉", "企业微信", "功能", "定价", "市场", "用户"],
            "min_evidence_count": 8,
            "quality_threshold": {"完整性": 70, "准确性": 65},
        },
    },
    {
        "task_id": "golden_006",
        "title": "Notion 与同类产品功能深度对比",
        "competitors": ["Notion", "Confluence", "语雀", "飞书文档"],
        "dimensions": ["功能", "用户"],
        "difficulty": "medium",
        "expected": {
            "competitors_covered": 4,
            "dimensions_covered": 2,
            "required_keywords": ["Notion", "Confluence", "语雀", "飞书文档", "功能", "用户"],
            "min_evidence_count": 6,
            "quality_threshold": {"完整性": 65, "准确性": 65},
        },
    },
    {
        "task_id": "golden_007",
        "title": "AI 编程助手市场调研",
        "competitors": ["GitHub Copilot", "Cursor", "通义灵码", "文心快码"],
        "dimensions": ["功能", "定价", "市场"],
        "difficulty": "medium",
        "expected": {
            "competitors_covered": 4,
            "dimensions_covered": 3,
            "required_keywords": ["Copilot", "Cursor", "通义灵码", "文心快码", "功能", "定价", "市场"],
            "min_evidence_count": 8,
            "quality_threshold": {"完整性": 70, "准确性": 70},
        },
    },

    # ───────────────────────── HARD (3 条) ────────────────────────────
    # Hard = 意图模糊，没有明确的竞品 → 走 Supervisor 探索模式
    {
        "task_id": "golden_008",
        "title": "哪些产品在协同办公领域有竞争力？",
        "competitors": [],   # 空列表 → 触发 Supervisor 探索
        "dimensions": ["功能", "市场"],
        "difficulty": "hard",
        "expected": {
            "competitors_covered": 2,       # 探索模式下至少发现 2 个竞品
            "dimensions_covered": 2,
            "required_keywords": ["协同", "功能", "市场"],
            "min_evidence_count": 4,
            "quality_threshold": {"完整性": 60, "准确性": 55},
        },
    },
    {
        "task_id": "golden_009",
        "title": "中国市场上最好的 AI 编程工具是什么？",
        "competitors": [],   # Supervisor 自行搜索发现竞品
        "dimensions": ["功能", "定价"],
        "difficulty": "hard",
        "expected": {
            "competitors_covered": 2,
            "dimensions_covered": 2,
            "required_keywords": ["AI", "编程", "功能", "定价"],
            "min_evidence_count": 4,
            "quality_threshold": {"完整性": 60, "准确性": 55},
        },
    },
    {
        "task_id": "golden_010",
        "title": "企业级知识管理工具的市场格局如何？",
        "competitors": [],
        "dimensions": ["市场", "功能"],
        "difficulty": "hard",
        "expected": {
            "competitors_covered": 2,
            "dimensions_covered": 2,
            "required_keywords": ["知识管理", "市场", "功能", "企业"],
            "min_evidence_count": 4,
            "quality_threshold": {"完整性": 60, "准确性": 55},
        },
    },
]


# ═════════════════════════════════════════════════════════════════════════════
# 工具函数
# ═════════════════════════════════════════════════════════════════════════════

def get_by_difficulty(difficulty: str) -> list[GoldenCase]:
    """按难度筛选 Golden 用例。

    Args:
        difficulty: easy / medium / hard

    Returns:
        筛选后的用例列表，难度不匹配返回空列表
    """
    return [case for case in GOLDEN_DATASET if case["difficulty"] == difficulty]


def get_pipeline_cases() -> list[GoldenCase]:
    """获取所有 Pipeline 模式用例（competitors 非空 → 走确定性流程）。

    对应 IntentRouter.classify() 中 competitors≥1 AND dimensions≥1 → Pipeline 路径。
    """
    return [case for case in GOLDEN_DATASET if len(case["competitors"]) > 0]


def get_supervisor_cases() -> list[GoldenCase]:
    """获取所有 Supervisor 探索模式用例（competitors 为空 → 走 ReAct 探索）。

    对应 IntentRouter.classify() 中 competitors=0 → Supervisor 路径。
    """
    return [case for case in GOLDEN_DATASET if len(case["competitors"]) == 0]


def assert_keywords(report_text: str, keywords: list[str]) -> dict:
    """验证报告中是否包含所有必需关键词。

    Args:
        report_text: 生成的报告正文
        keywords: 必需关键词列表

    Returns:
        {"passed": [命中关键词], "missed": [未命中关键词], "rate": 命中率}
    """
    passed = [kw for kw in keywords if kw.lower() in report_text.lower()]
    missed = [kw for kw in keywords if kw not in passed]
    return {
        "passed": passed,
        "missed": missed,
        "rate": len(passed) / len(keywords) if keywords else 1.0,
    }
