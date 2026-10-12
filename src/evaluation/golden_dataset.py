# -*- coding: utf-8 -*-
"""Golden Dataset — 离线评估黄金标准数据集。

═══════════════════════════════════════════════════════════════════════════════
                        【L4 工程 — Golden Dataset 设计原则】
═══════════════════════════════════════════════════════════════════════════════

  1. 三级难度分档（easy/medium/hard）覆盖不同场景应力
  2. 字段精简——每条只保留「被评估链路真实消费」的字段，无死代码
  3. 标准答案是「可验证事实点」而非「固定数值」——对时效不敏感
  4. 与系统真实竞品/维度正交——name + dimensions 来自业务场景，非编造

  为什么需要 Golden Dataset？
  — 评估不能只在 CI 里跑一遍 E2E 就说"通过"
  — 必须有标准答案级别的对照集，验证输出是否符合预期覆盖率/关键词/引用
  — 类比 ML 里的 test set：不是看一次结果，是持续回归的标尺
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from typing import TypedDict


class GoldenCase(TypedDict):
    """单个 Golden Dataset 条目。

    字段分「输入」与「标准答案」两类，全部被评估链路真实消费，无死字段：

    ── 输入（喂给系统跑真实 Pipeline/Supervisor）──
      task_id     唯一标识（数据集内用，落库时换成真实 UUID）
      title       任务标题
      competitors 竞品列表（空列表 → 触发 Supervisor 探索模式）
      dimensions  维度列表
      difficulty  easy / medium / hard

    ── 标准答案（喂给评估打分）──
      reference              事实级标准答案（整段），供人工核对与文档对齐
      reference_by_dimension 分维度标准答案，供检索阶段 context_precision 比对（关键消费方）
      reference_domains      关键来源域名锚点，供检索召回 recall_hit_rate 对拍（关键消费方）
    """
    task_id: str
    title: str
    competitors: list[str]
    dimensions: list[str]
    difficulty: str
    reference: str
    reference_by_dimension: dict[str, str]
    reference_domains: list[str]


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
        # 事实级标准答案：应覆盖「功能」与「定价」两个维度下飞书/钉钉的关键事实，
        # 具体数字因时效会变，故 reference 锚定的是「可验证事实点」而非固定数值。
        "reference": (
            "飞书与钉钉的功能对比应至少说明：飞书以即时通讯+文档+多维表格+日历协同见长，"
            "钉钉以审批/考勤/考勤打卡等办公流程（OA）见长。定价维度应给出两者的付费模式："
            "飞书提供免费版与按席位订阅的企业版；钉钉提供免费版与专业版/专属版的订阅档位，"
            "且两者的订阅价格通常按「每人/月」或「每人/年」计价。标准答案应包含上述功能侧重点"
            "与定价模式两方面的可验证事实，而不是只写「功能强大」「定价合理」这类空话。"
        ),
        "reference_by_dimension": {
            "功能": "飞书以即时通讯+文档+多维表格+日历协同见长；钉钉以审批/考勤/考勤打卡等办公流程（OA）见长",
            "定价": "飞书提供免费版与按席位订阅的企业版；钉钉提供免费版与专业版/专属版的订阅档位，均按每人/月或每人/年计价",
        },
        "reference_domains": ["feishu.cn", "dingtalk.com"],
    },
    {
        "task_id": "golden_002",
        "title": "Notion 市场定位分析",
        "competitors": ["Notion", "语雀"],
        "dimensions": ["市场", "用户"],
        "difficulty": "easy",
        "reference": (
            "市场维度应说明 Notion 主打全球市场、定位为 all-in-one 工作空间，"
            "语雀主打中国市场、定位为知识库与团队文档协作。用户维度应说明两者目标用户画像："
            "Notion 偏向个人知识管理+国际化团队+开发者，语雀偏向中文互联网从业者、"
            "技术团队与知识沉淀场景。标准答案应体现两者在市场地域定位与目标用户群体上的"
            "可验证差异，而非笼统描述。"
        ),
        "reference_by_dimension": {
            "市场": "Notion 主打全球市场、定位 all-in-one 工作空间；语雀主打中国市场、定位知识库与团队文档协作",
            "用户": "Notion 偏向个人知识管理+国际化团队+开发者；语雀偏向中文互联网从业者、技术团队与知识沉淀场景",
        },
        "reference_domains": ["notion.so", "yuque.com"],
    },
    {
        "task_id": "golden_003",
        "title": "腾讯文档 vs 石墨文档功能对比",
        "competitors": ["腾讯文档", "石墨文档"],
        "dimensions": ["功能"],
        "difficulty": "easy",
        "reference": (
            "功能对比应至少说明两者均为在线协作文档，支持多人在线实时编辑。"
            "腾讯文档依托腾讯生态，与微信/QQ 打通、支持在线表格/幻灯片/收集表；"
            "石墨文档以轻量协作见长，支持文档/表格/表单/白板，并强调团队权限管理与评论协作。"
            "标准答案应覆盖两者在「实时协作能力」与「各自特色功能（生态集成 vs 轻量表单）」上的"
            "可验证差异。"
        ),
        "reference_by_dimension": {
            "功能": "两者均为在线协作文档、支持多人实时编辑；腾讯文档依托腾讯生态、与微信/QQ 打通、支持在线表格/幻灯片/收集表；石墨文档以轻量协作见长、支持文档/表格/表单/白板、强调团队权限管理与评论协作",
        },
        "reference_domains": ["docs.qq.com", "shimo.im"],
    },
    {
        "task_id": "golden_004",
        "title": "WPS Office 定价分析",
        "competitors": ["WPS Office", "Microsoft 365"],
        "dimensions": ["定价"],
        "difficulty": "easy",
        "reference": (
            "定价分析应说明 WPS Office 与 Microsoft 365 的付费模式差异："
            "WPS 提供免费版+会员（含 WPS 会员/超级会员）按年订阅，价格显著低于微软；"
            "Microsoft 365 提供个人版/家庭版/商业版按年订阅，支持多设备。"
            "标准答案应覆盖「免费与付费档位」「订阅周期（多为年付）」「两者价格水平差异」"
            "这些可验证事实点，具体金额可因促销浮动但付费模式与相对价格关系应正确。"
        ),
        "reference_by_dimension": {
            "定价": "WPS 提供免费版+会员（WPS 会员/超级会员）按年订阅、价格低于微软；Microsoft 365 提供个人版/家庭版/商业版按年订阅、支持多设备",
        },
        "reference_domains": ["wps.cn", "microsoft.com"],
    },

    # ──────────────────────── MEDIUM (3 条) ──────────────────────────
    # Medium = 多竞品/多维度，走 Pipeline 但需要 richer 的采集和分析
    {
        "task_id": "golden_005",
        "title": "三大协同办公软件全维度对比",
        "competitors": ["飞书", "钉钉", "企业微信"],
        "dimensions": ["功能", "定价", "市场", "用户"],
        "difficulty": "medium",
        "reference": (
            "全维度对比应覆盖飞书/钉钉/企业微信三者在四个维度的可验证事实："
            "功能上分别说明各自的协同特色（飞书文档+多维表格、钉钉 OA 审批考勤、企业微信与微信互通）；"
            "定价上说明三者的免费版与付费订阅档位差异；市场上说明三者母体（字节/阿里/腾讯）生态与市占定位；"
            "用户上说明目标客户画像（飞书偏互联网/新锐企业，钉钉偏中小制造/政务，企业微信偏私域运营与零售）。"
            "标准答案应逐维度给出三者之间的差异，而非只罗列「都有聊天、都有文档」的共性。"
        ),
        "reference_by_dimension": {
            "功能": "飞书文档+多维表格、钉钉 OA 审批考勤、企业微信与微信互通",
            "定价": "飞书/钉钉/企业微信三者的免费版与付费订阅档位差异",
            "市场": "三者母体生态与市占定位（飞书=字节、钉钉=阿里、企业微信=腾讯）",
            "用户": "飞书偏互联网/新锐企业，钉钉偏中小制造/政务，企业微信偏私域运营与零售",
        },
        "reference_domains": ["feishu.cn", "dingtalk.com", "work.weixin.qq.com"],
    },
    {
        "task_id": "golden_006",
        "title": "Notion 与同类产品功能深度对比",
        "competitors": ["Notion", "Confluence", "语雀", "飞书文档"],
        "dimensions": ["功能", "用户"],
        "difficulty": "medium",
        "reference": (
            "功能深度对比应说明四者定位差异：Notion 是 all-in-one 数据库型工作空间（block+数据库视图），"
            "Confluence 面向企业知识库与研发文档协作、强调与 Jira 集成，语雀偏中文知识沉淀与结构化文档，"
            "飞书文档强调实时协同与飞书生态内嵌。用户维度应说明各自典型用户：Notion 面向个人/灵活团队，"
            "Confluence 面向研发型企业，语雀面向中文技术/内容团队，飞书文档面向已用飞书的企业。"
            "标准答案应体现「功能范式差异」与「目标用户差异」两个维度的可验证对比。"
        ),
        "reference_by_dimension": {
            "功能": "Notion 是 all-in-one 数据库型工作空间（block+数据库视图）；Confluence 面向企业知识库与研发文档、强调 Jira 集成；语雀偏中文知识沉淀与结构化文档；飞书文档强调实时协同与飞书生态内嵌",
            "用户": "Notion 面向个人/灵活团队，Confluence 面向研发型企业，语雀面向中文技术/内容团队，飞书文档面向已用飞书的企业",
        },
        "reference_domains": ["notion.so", "atlassian.com", "yuque.com", "feishu.cn"],
    },
    {
        "task_id": "golden_007",
        "title": "AI 编程助手市场调研",
        "competitors": ["GitHub Copilot", "Cursor", "通义灵码", "文心快码"],
        "dimensions": ["功能", "定价", "市场"],
        "difficulty": "medium",
        "reference": (
            "市场调研应覆盖四款 AI 编程助手在功能/定价/市场的可验证事实："
            "功能上说明 GitHub Copilot 主打代码补全+Chat、Cursor 是 AI-first 编辑器、"
            "通义灵码与文心快码分别为阿里/百度系、侧重中文与国内开发者生态；"
            "定价上说明 Copilot 与 Cursor 采用海外按月订阅（如每月约 10 美元档），"
            "国内产品多提供免费版+会员订阅；市场上说明海外（Copilot/Cursor）与国内（通义灵码/文心快码）"
            "的阵营划分与各自母体。标准答案应区分「海外 vs 国内」阵营并给出各自的付费模式。"
        ),
        "reference_by_dimension": {
            "功能": "GitHub Copilot 主打代码补全+Chat；Cursor 是 AI-first 编辑器；通义灵码/文心快码分别为阿里/百度系、侧重中文与国内开发者生态",
            "定价": "Copilot 与 Cursor 采用海外按月订阅（约 10 美元档）；国内产品多提供免费版+会员订阅",
            "市场": "海外阵营（Copilot/Cursor）与国内阵营（通义灵码/文心快码）的划分及各自母体",
        },
        "reference_domains": ["github.com", "cursor.com", "tongyi.aliyun.com", "yiyan.baidu.com"],
    },

    # ───────────────────────── HARD (3 条) ────────────────────────────
    # Hard = 意图模糊，没有明确的竞品 → 走 Supervisor 探索模式
    {
        "task_id": "golden_008",
        "title": "哪些产品在协同办公领域有竞争力？",
        "competitors": [],   # 空列表 → 触发 Supervisor 探索
        "dimensions": ["功能", "市场"],
        "difficulty": "hard",
        "reference": (
            "探索模式下标准答案应至少覆盖协同办公领域 2 个以上代表性产品（如飞书、钉钉、"
            "企业微信、Microsoft Teams、Slack 中的任意若干），并说明其在功能维度（即时通讯/文档/"
            "会议/OA 流程）与市场维度（所属母体、目标客群、地域市场）上的关键事实。"
            "标准答案应体现「探索出了哪些竞品」与「每个竞品的关键差异化事实」，而非只给一个模糊结论。"
        ),
        "reference_by_dimension": {
            "功能": "协同办公产品的功能维度关键事实：即时通讯/文档/会议/OA 流程等能力",
            "市场": "协同办公产品的市场维度关键事实：所属母体、目标客群、地域市场",
        },
        "reference_domains": ["feishu.cn", "dingtalk.com", "work.weixin.qq.com", "slack.com"],
    },
    {
        "task_id": "golden_009",
        "title": "中国市场上最好的 AI 编程工具是什么？",
        "competitors": [],   # Supervisor 自行搜索发现竞品
        "dimensions": ["功能", "定价"],
        "difficulty": "hard",
        "reference": (
            "探索模式下标准答案应覆盖中国市场 2 个以上 AI 编程工具（如通义灵码、文心快码、"
            "腾讯云 AI 代码助手、以及进入中国市场的 GitHub Copilot/Cursor 等），"
            "并说明功能维度（代码补全/对话/Agent 能力）与定价维度（免费版与付费订阅档位）的关键事实。"
            "标准答案应体现「探索出了哪些工具」与「各自功能/定价的可验证事实」，"
            "而非空泛回答「各有优劣」或直接断言某一款就是最好。"
        ),
        "reference_by_dimension": {
            "功能": "AI 编程工具的功能维度关键事实：代码补全/对话/Agent 能力",
            "定价": "AI 编程工具的定价维度关键事实：免费版与付费订阅档位",
        },
        "reference_domains": ["tongyi.aliyun.com", "yiyan.baidu.com", "github.com", "cursor.com"],
    },
    {
        "task_id": "golden_010",
        "title": "企业级知识管理工具的市场格局如何？",
        "competitors": [],
        "dimensions": ["市场", "功能"],
        "difficulty": "hard",
        "reference": (
            "探索模式下标准答案应覆盖企业知识管理领域 2 个以上代表性工具（如 Confluence、Notion、"
            "语雀、飞书知识库、Guru、Document360 中的若干），并说明市场维度（目标企业规模、"
            "地域市场、所属母体）与功能维度（知识库/结构化文档/权限管理/检索能力）的关键事实。"
            "标准答案应体现「探索出了哪些工具」与「各自面向企业市场的定位与功能差异」，而非只给结论。"
        ),
        "reference_by_dimension": {
            "市场": "企业知识管理工具的市场维度关键事实：目标企业规模、地域市场、所属母体",
            "功能": "企业知识管理工具的功能维度关键事实：知识库/结构化文档/权限管理/检索能力",
        },
        "reference_domains": ["atlassian.com", "notion.so", "yuque.com", "feishu.cn"],
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


def get_reference(task_id: str) -> str | None:
    """按 task_id 查 Golden Case 的事实级标准答案（reference）。

    用于检索质量评估 evaluate_retrieval 的 context_precision —— 该指标需要
    「reference」作为 ground truth，把检索上下文与之比对（需按维度拆成
    reference_map 使用）。此前因 Golden 缺事实级标准答案，reference 只能拿
    标题凑数，导致该指标恒为 0。

    注意：生成质量评估（faithfulness / answer_relevancy）无需标准答案，
    不消费本函数——context_precision 是检索阶段指标，不在生成阶段重复算。

    Args:
        task_id: Golden Case 的 task_id（如 "golden_001"）

    Returns:
        该 Case 的事实级标准答案文本；找不到返回 None
    """
    for case in GOLDEN_DATASET:
        if case["task_id"] == task_id:
            return case.get("reference")
    return None


def get_reference_by_dimension(task_id: str) -> dict[str, str]:
    """按 task_id 查 Golden Case 的「分维度标准答案」（reference_by_dimension）。

    用于检索质量评估 evaluate_retrieval 的 context_precision —— 该指标需要
    每个维度各自的 ground truth（reference_map），把该维度的检索上下文与之
    比对。整段 reference 是命题式描述，无法直接按维度对齐，故拆成
    {dimension: 该维度的关键事实点}。

    Args:
        task_id: Golden Case 的 task_id（如 "golden_001"）

    Returns:
        {dimension: 该维度的标准答案}；找不到返回空 dict
    """
    for case in GOLDEN_DATASET:
        if case["task_id"] == task_id:
            return case.get("reference_by_dimension", {})
    return {}


def get_reference_domains(task_id: str) -> list[str]:
    """按 task_id 查 Golden Case 的关键来源域名锚点（reference_domains）。

    用于检索召回覆盖度 recall_hit_rate —— 把 retrieval_snapshot 里落盘的
    recalled_urls 与这些关键来源域名做归一化对拍，度量「召回是否漏掉了
    关键来源」。域名级匹配比 URL 级匹配稳健（具体 URL 会变，域名是稳定锚点）。

    Args:
        task_id: Golden Case 的 task_id（如 "golden_001"）

    Returns:
        该 Case 的关键来源域名列表；找不到返回空列表
    """
    for case in GOLDEN_DATASET:
        if case["task_id"] == task_id:
            return case.get("reference_domains", [])
    return []


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
