# -*- coding: utf-8 -*-
"""Golden Dataset — 离线评估黄金标准数据集。

═══════════════════════════════════════════════════════════════════════════════
                        【L4 工程 — Golden Dataset 设计原则】
═══════════════════════════════════════════════════════════════════════════════

  1. 三级难度分档（easy/medium/hard）覆盖不同场景应力
  2. 字段精简——每条只保留「被评估链路真实消费」的字段，无死字段
     （2026-10-13 已删除零代码消费的整段 `reference`；剩余字段全部有下游接线）
  3. 标准答案是「可验证事实点」而非「固定数值」——对时效不敏感
  4. 与系统真实竞品/维度正交——name + dimensions 来自业务场景，非编造

  ⚠️ 评估链路的 ground truth 有两层粒度，**新链路必须用 cell 级**：

     ① `reference_by_cell["竞品@维度"]`（**cell 级，单竞品**）
        —— ragas_core 的四指标走这条。评估单元是 (竞品, 维度)，基准就必须只讲
        该竞品；若拿维度级复合句（含对方竞品）当基准，每个 cell 必然有一半
        讲的是别人，**context_recall 天花板被钉死在 0.5**（2026-10-16 实测：
        `飞书@功能 cr=0.500` 正是"正好半句"）。

     ② `reference_by_dimension[维度]`（**维度级，跨竞品复合**）
        —— 供真实任务兜底（`llm_factory.resolve_reference`）。

     无论哪一层，**文本都必须是事实点**，不能写成"XX 维度关键事实：…等能力"
     这类模板占位，也不能写成"两者的定价档位差异"这类只说有差异、不说差异
     是什么的元描述——喂给 judge 都等于没有 ground truth，分数不可信。
     另：Golden 模式下键对不上时 `_resolve_reference` 会**显式 warning**
     （v2026-10-14 前是静默退化成用维度名当标准答案，属静默失真）。

  ⚠️ cell 级与维度级必须**事实一致**：`reference_by_cell` 由 `reference_by_dimension`
     的复合句按竞品**机械拆分**而来（不新增、不虚构事实），拆分后各自仍是事实点。

  ⚠️ **必须事实点化（多句），不能写成单句**（2026-10-17）：
     `context_recall` 的分母 = judge 切出的句子数（`_context_recall.py` 用
     "analyze each sentence in the answer"，且 `generate_multiple(n=1)` 不投票）。
     单句 reference → 分母恒为 1 → cr 取值域只有 {0,1}，judge 抖一次整格翻转，
     表现为「同一格同一输入跑两次 0 / 1」（2026-10-17 实测 `[0,1,0]`）。
     故每条 cell reference 至少 3 个「；」分隔的独立事实点。
     ⚠️ 改写**必须独立于被测 contexts**：只写该产品公开可核验的事实，
     不得照着 contexts 反写，否则 cr 变成自证、失去评估意义。

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
      reference_by_dimension 分维度标准答案（**跨竞品复合句**），真实任务兜底用
      reference_by_cell      分「竞品@维度」标准答案（**单竞品**），供 ragas_core 四指标
                             逐 cell 比对（关键消费方）。评估单元 = (竞品, 维度)，
                             故 ground truth 也必须切到 cell 粒度
    """
    task_id: str
    title: str
    competitors: list[str]
    dimensions: list[str]
    difficulty: str
    reference_by_dimension: dict[str, str]
    reference_by_cell: dict[str, str]


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
        # 事实级标准答案：覆盖「功能」与「定价」两个维度下飞书/钉钉的关键事实。
        # 具体数字因时效会变，故锚定的是「可验证事实点」而非固定数值。
        #
        # 【事实点化（2026-10-17）】每格 3~4 个独立可核验事实，用「；」分隔。
        #   为什么必须拆：context_recall 的分母 = judge 切出的句子数
        #   （`_context_recall.py` 用 "analyze each sentence in the answer"，n=1 不投票）。
        #   单句 reference → 分母恒为 1 → cr 取值域只有 {0,1}，judge 抖一次整格翻转
        #   （2026-10-17 实测 `[0,1,0]` 抛硬币正是此机制）；拆成 N 句 → 分母 N，
        #   才有 0.25/0.5/0.75 的中间台阶。
        #   铁律：只写该产品**公开可核验**的事实，不得参照被测 contexts 反写 ——
        #   否则 cr 退化成自证（与 faithfulness 同病），分数再好看也没有意义。
        #   另：去掉「以…见长」这类需要全市场对比才能成立的判断词，改写为
        #   「提供/包含 X」这类单页材料就能支撑的硬陈述。
        "reference_by_dimension": {
            "功能": "飞书提供即时通讯、云文档、多维表格、日历与会议协同；钉钉提供 OA 审批、考勤管理、考勤打卡等办公流程能力",
            "定价": "飞书提供免费版，企业版按席位订阅并按每人/月或每人/年计价；钉钉提供免费版，另有专业版与专属版订阅档位，同样按每人/月或每人/年计价",
        },
        # cell 级 = 维度级复合句按竞品拆分（事实不增不减，仅去掉"对方那半句"），
        # 并进一步拆成逐条单事实句。
        "reference_by_cell": {
            "飞书@功能": "飞书提供即时通讯；提供云文档协作；提供多维表格；提供日历与会议协同",
            "钉钉@功能": "钉钉提供 OA 审批；提供考勤管理；提供考勤打卡",
            "飞书@定价": "飞书提供免费版；企业版按席位订阅；按每人/月或每人/年计价",
            "钉钉@定价": "钉钉提供免费版；提供专业版订阅档位；提供专属版订阅档位；按每人/月或每人/年计价",
        },
    },
    {
        "task_id": "golden_002",
        "title": "Notion 市场定位分析",
        "competitors": ["Notion", "语雀"],
        "dimensions": ["市场", "用户"],
        "difficulty": "easy",
        # 同上：事实点化（每格 3 句，分母 ≥3，cr 才有分辨率）。
        # 「面向 X」替代原「偏向 X」——去掉倾向性判断词，只留可核验的目标客群陈述。
        "reference_by_dimension": {
            "市场": "Notion 面向全球市场，定位为 all-in-one 工作空间，把文档、数据库与项目协作整合在同一产品内；语雀面向中国市场，定位为知识库与团队文档协作工具，提供文档与知识库两类核心功能",
            "用户": "Notion 面向个人知识管理场景、国际化团队与开发者；语雀面向中文互联网从业者、技术团队与知识沉淀场景",
        },
        "reference_by_cell": {
            "Notion@市场": "Notion 面向全球市场；定位为 all-in-one 工作空间；把文档、数据库与项目协作整合在同一产品内",
            "语雀@市场": "语雀面向中国市场；定位为知识库与团队文档协作工具；提供文档与知识库两类核心功能",
            "Notion@用户": "Notion 面向个人知识管理场景；面向国际化团队；面向开发者",
            "语雀@用户": "语雀面向中文互联网从业者；面向技术团队；面向知识沉淀场景",
        },
    },
    {
        "task_id": "golden_003",
        "title": "腾讯文档 vs 石墨文档功能对比",
        "competitors": ["腾讯文档", "石墨文档"],
        "dimensions": ["功能"],
        "difficulty": "easy",
        "reference_by_dimension": {
            "功能": "两者均为在线协作文档、支持多人实时编辑；腾讯文档依托腾讯生态、与微信/QQ 打通、支持在线表格/幻灯片/收集表；石墨文档以轻量协作见长、支持文档/表格/表单/白板、强调团队权限管理与评论协作",
        },
        # 「两者均为在线协作文档、支持多人实时编辑」是**共同事实**，两个 cell 都保留。
        "reference_by_cell": {
            "腾讯文档@功能": "腾讯文档为在线协作文档、支持多人实时编辑；依托腾讯生态，与微信/QQ 打通；支持在线表格、幻灯片、收集表",
            "石墨文档@功能": "石墨文档为在线协作文档、支持多人实时编辑；以轻量协作见长；支持文档、表格、表单、白板；强调团队权限管理与评论协作",
        },
    },
    {
        "task_id": "golden_004",
        "title": "WPS Office 定价分析",
        "competitors": ["WPS Office", "Microsoft 365"],
        "dimensions": ["定价"],
        "difficulty": "easy",
        "reference_by_dimension": {
            "定价": "WPS 提供免费版+会员（WPS 会员/超级会员）按年订阅、价格低于微软；Microsoft 365 提供个人版/家庭版/商业版按年订阅、支持多设备",
        },
        "reference_by_cell": {
            "WPS Office@定价": "WPS Office 提供免费版与会员（WPS 会员/超级会员）按年订阅，价格低于微软",
            "Microsoft 365@定价": "Microsoft 365 提供个人版、家庭版、商业版按年订阅，支持多设备",
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
        "reference_by_dimension": {
            "功能": "飞书文档+多维表格、钉钉 OA 审批考勤、企业微信与微信互通",
            # 事实级：写清「各自是什么付费结构」，而不是「有差异」。
            # （原「三者的免费版与付费订阅档位差异」是元描述——只说有差异，
            #  没说差异是什么，等于没有 ground truth。）
            "定价": "飞书提供免费版 + 企业版按席位订阅（标准/专业/旗舰档）；钉钉提供免费版 + 专业版/专属版按席位订阅；企业微信基础功能免费，增值能力（会话内容存档、企业认证、部分第三方应用）单独计费",
            # 事实级：写清母体归属 + 各自市场定位，不止「母体生态与市占定位」这句空转。
            "市场": "飞书属字节跳动系、钉钉属阿里系、企业微信属腾讯系，三者均主打中国市场；飞书主打互联网与新锐企业的协同办公，钉钉在企业办公与政务市场渗透广，企业微信依托微信生态主打客户连接与私域运营",
            "用户": "飞书偏互联网/新锐企业，钉钉偏中小制造/政务，企业微信偏私域运营与零售",
        },
        # 3 竞品 × 4 维度 = 12 个 cell，逐格拆出「只讲自己」的那半句。
        "reference_by_cell": {
            "飞书@功能": "飞书以文档与多维表格见长",
            "钉钉@功能": "钉钉以 OA 审批与考勤见长",
            "企业微信@功能": "企业微信以与微信互通见长",
            "飞书@定价": "飞书提供免费版，企业版按席位订阅（标准/专业/旗舰档）",
            "钉钉@定价": "钉钉提供免费版，专业版/专属版按席位订阅",
            "企业微信@定价": "企业微信基础功能免费，增值能力（会话内容存档、企业认证、部分第三方应用）单独计费",
            "飞书@市场": "飞书属字节跳动系，主打中国市场，面向互联网与新锐企业的协同办公",
            "钉钉@市场": "钉钉属阿里系，主打中国市场，在企业办公与政务市场渗透广",
            "企业微信@市场": "企业微信属腾讯系，主打中国市场，依托微信生态主打客户连接与私域运营",
            "飞书@用户": "飞书偏互联网与新锐企业",
            "钉钉@用户": "钉钉偏中小制造与政务",
            "企业微信@用户": "企业微信偏私域运营与零售",
        },
    },
    {
        "task_id": "golden_006",
        "title": "Notion 与同类产品功能深度对比",
        "competitors": ["Notion", "Confluence", "语雀", "飞书文档"],
        "dimensions": ["功能", "用户"],
        "difficulty": "medium",
        "reference_by_dimension": {
            "功能": "Notion 是 all-in-one 数据库型工作空间（block+数据库视图）；Confluence 面向企业知识库与研发文档、强调 Jira 集成；语雀偏中文知识沉淀与结构化文档；飞书文档强调实时协同与飞书生态内嵌",
            "用户": "Notion 面向个人/灵活团队，Confluence 面向研发型企业，语雀面向中文技术/内容团队，飞书文档面向已用飞书的企业",
        },
        "reference_by_cell": {
            "Notion@功能": "Notion 是 all-in-one 数据库型工作空间（block+数据库视图）",
            "Confluence@功能": "Confluence 面向企业知识库与研发文档，强调 Jira 集成",
            "语雀@功能": "语雀偏中文知识沉淀与结构化文档",
            "飞书文档@功能": "飞书文档强调实时协同与飞书生态内嵌",
            "Notion@用户": "Notion 面向个人与灵活团队",
            "Confluence@用户": "Confluence 面向研发型企业",
            "语雀@用户": "语雀面向中文技术与内容团队",
            "飞书文档@用户": "飞书文档面向已用飞书的企业",
        },
    },
    {
        "task_id": "golden_007",
        "title": "AI 编程助手市场调研",
        "competitors": ["GitHub Copilot", "Cursor", "通义灵码", "文心快码"],
        "dimensions": ["功能", "定价", "市场"],
        "difficulty": "medium",
        "reference_by_dimension": {
            "功能": "GitHub Copilot 主打代码补全+Chat；Cursor 是 AI-first 编辑器；通义灵码/文心快码分别为阿里/百度系、侧重中文与国内开发者生态",
            "定价": "Copilot 与 Cursor 采用海外按月订阅（约 10 美元档）；国内产品多提供免费版+会员订阅",
            # 事实级：写清两大阵营的母体归属 + 各自的定价与生态特征，
            # 而不是「海外阵营与国内阵营的划分及各自母体」这类元描述。
            "市场": "海外阵营 GitHub Copilot（微软/GitHub 系）与 Cursor（Anysphere）主打全球开发者市场、按海外月订阅计价；国内阵营通义灵码（阿里云）与文心快码（百度）主打中国开发者市场、提供免费个人版，两阵营在云生态绑定与合规适配上分化",
        },
        "reference_by_cell": {
            "GitHub Copilot@功能": "GitHub Copilot 主打代码补全与 Chat",
            "Cursor@功能": "Cursor 是 AI-first 编辑器",
            "通义灵码@功能": "通义灵码属阿里系，侧重中文与国内开发者生态",
            "文心快码@功能": "文心快码属百度系，侧重中文与国内开发者生态",
            "GitHub Copilot@定价": "GitHub Copilot 采用海外按月订阅计价（约 10 美元档）",
            "Cursor@定价": "Cursor 采用海外按月订阅计价（约 10 美元档）",
            "通义灵码@定价": "通义灵码提供免费版与会员订阅",
            "文心快码@定价": "文心快码提供免费版与会员订阅",
            "GitHub Copilot@市场": "GitHub Copilot 属微软/GitHub 系，主打全球开发者市场，按海外月订阅计价",
            "Cursor@市场": "Cursor 属 Anysphere，主打全球开发者市场，按海外月订阅计价",
            "通义灵码@市场": "通义灵码属阿里云，主打中国开发者市场，提供免费个人版",
            "文心快码@市场": "文心快码属百度，主打中国开发者市场，提供免费个人版",
        },
        # （tongyi.aliyun.com 是通义千问对话站），文心快码官网是 comate.baidu.com
    },

    # ───────────────────────── HARD (3 条) ────────────────────────────
    # Hard = 意图模糊，没有明确的竞品 → 走 Supervisor 探索模式
    {
        "task_id": "golden_008",
        "title": "哪些产品在协同办公领域有竞争力？",
        "competitors": [],   # 空列表 → 触发 Supervisor 探索
        "dimensions": ["功能", "市场"],
        "difficulty": "hard",
        "reference_by_dimension": {
            # 事实级标准答案（不是模板占位）：context_precision 只读这个字段，
            # 故必须给出「可验证事实点」——复用 001/005 已确立的功能/母体事实，
            # 海外产品按公开常识补齐，不引入具体数值（对时效不敏感）。
            "功能": "代表性产品的功能特性应可验证：飞书以即时通讯+文档+多维表格+日历协同见长；钉钉以审批/考勤等 OA 流程见长；企业微信以与微信互通见长；Microsoft Teams 以会议+文档协作并与 Office 套件捆绑见长；Slack 以频道化即时通讯+第三方应用集成见长",
            "市场": "代表性产品的市场归属应可验证：飞书属字节系、钉钉属阿里系、企业微信属腾讯系，三者主打中国市场；Microsoft Teams 属微软、Slack 属 Salesforce，主打海外市场。目标客群上飞书偏互联网/新锐企业，钉钉偏中小制造/政务，企业微信偏私域运营与零售",
        },
        # ⚠️ 本条的 competitors 为空（Supervisor 探索模式）→ **无法在数据期枚举 cell 键**。
        #    这里按标准答案点名的产品写 cell 键：探索出的竞品名命中即用 cell 级，
        #    未命中自动回退维度级 reference（见 llm_factory.resolve_reference）。
        "reference_by_cell": {
            "飞书@功能": "飞书以即时通讯、文档、多维表格、日历协同见长",
            "钉钉@功能": "钉钉以审批、考勤等 OA 流程见长",
            "企业微信@功能": "企业微信以与微信互通见长",
            "Microsoft Teams@功能": "Microsoft Teams 以会议与文档协作、并与 Office 套件捆绑见长",
            "Slack@功能": "Slack 以频道化即时通讯与第三方应用集成见长",
            "飞书@市场": "飞书属字节系，主打中国市场，偏互联网与新锐企业",
            "钉钉@市场": "钉钉属阿里系，主打中国市场，偏中小制造与政务",
            "企业微信@市场": "企业微信属腾讯系，主打中国市场，偏私域运营与零售",
            "Microsoft Teams@市场": "Microsoft Teams 属微软，主打海外市场",
            "Slack@市场": "Slack 属 Salesforce，主打海外市场",
        },
        # 008 的功能/市场两处都点名 Microsoft Teams，故必须有 microsoft.com
        # （Teams 页面在 teams.microsoft.com，属 microsoft.com 子域，子域匹配可命中）。
    },
    {
        "task_id": "golden_009",
        "title": "中国市场上最好的 AI 编程工具是什么？",
        "competitors": [],   # Supervisor 自行搜索发现竞品
        "dimensions": ["功能", "定价"],
        "difficulty": "hard",
        "reference_by_dimension": {
            # 事实级标准答案：复用 007 已确立的功能/付费模式事实；
            # 定价只写「付费模式档位」不写金额（金额随促销浮动，对时效敏感）。
            "功能": "代表性工具的功能能力应可验证：通义灵码（阿里系）支持行间代码补全、研发智能问答与编程智能体，适配 VS Code / JetBrains，深度打通阿里云生态；文心快码（百度系）基于文心大模型，支持代码补全与自然语言生成代码，覆盖 200+ 语言；GitHub Copilot 以代码补全+Chat 见长；Cursor 是 AI-first 编辑器",
            "定价": "代表性工具的付费模式应可验证：国内产品（通义灵码、文心快码）均提供免费个人版 + 按席位订阅的企业版；海外产品（GitHub Copilot、Cursor）采用海外按月订阅计价，档位高于国内产品",
        },
        # competitors 为空（Supervisor 探索）→ cell 键按标准答案点名的产品写。
        "reference_by_cell": {
            "通义灵码@功能": "通义灵码（阿里系）支持行间代码补全、研发智能问答与编程智能体，适配 VS Code / JetBrains，深度打通阿里云生态",
            "文心快码@功能": "文心快码（百度系）基于文心大模型，支持代码补全与自然语言生成代码，覆盖 200+ 语言",
            "GitHub Copilot@功能": "GitHub Copilot 以代码补全与 Chat 见长",
            "Cursor@功能": "Cursor 是 AI-first 编辑器",
            "通义灵码@定价": "通义灵码提供免费个人版与按席位订阅的企业版",
            "文心快码@定价": "文心快码提供免费个人版与按席位订阅的企业版",
            "GitHub Copilot@定价": "GitHub Copilot 采用海外按月订阅计价，档位高于国内产品",
            "Cursor@定价": "Cursor 采用海外按月订阅计价，档位高于国内产品",
        },
    },
    {
        "task_id": "golden_010",
        "title": "企业级知识管理工具的市场格局如何？",
        "competitors": [],
        "dimensions": ["市场", "功能"],
        "difficulty": "hard",
        "reference_by_dimension": {
            # 事实级标准答案：复用 002/006 已确立的 Notion/语雀/Confluence 事实，
            # 补齐早期未展开的 Guru / Document360。
            "市场": "代表性工具的市场定位应可验证：Confluence 属 Atlassian，面向中大型企业与研发团队、主打全球市场；Notion 面向全球中小团队与个人、主打 all-in-one 工作空间；语雀属阿里系、面向中文技术/内容团队；飞书知识库面向已用飞书的企业、主打中国市场；Guru 与 Document360 均面向海外企业的内部/对外知识库场景",
            "功能": "代表性工具的功能差异应可验证：Confluence 强调与企业知识库、研发文档协作及 Jira 集成；Notion 以 block+数据库视图的结构化文档见长；语雀偏中文知识沉淀与结构化文档；飞书知识库强调与飞书生态内嵌；Guru 强调浏览器插件式的内部知识卡片，Document360 面向对外帮助中心/文档站点",
        },
        # competitors 为空（Supervisor 探索）→ cell 键按标准答案点名的产品写。
        "reference_by_cell": {
            "Confluence@市场": "Confluence 属 Atlassian，面向中大型企业与研发团队，主打全球市场",
            "Notion@市场": "Notion 面向全球中小团队与个人，主打 all-in-one 工作空间",
            "语雀@市场": "语雀属阿里系，面向中文技术与内容团队",
            "飞书知识库@市场": "飞书知识库面向已用飞书的企业，主打中国市场",
            "Guru@市场": "Guru 面向海外企业的内部知识库场景",
            "Document360@市场": "Document360 面向海外企业的对外知识库场景",
            "Confluence@功能": "Confluence 强调与企业知识库、研发文档协作及 Jira 集成",
            "Notion@功能": "Notion 以 block+数据库视图的结构化文档见长",
            "语雀@功能": "语雀偏中文知识沉淀与结构化文档",
            "飞书知识库@功能": "飞书知识库强调与飞书生态内嵌",
            "Guru@功能": "Guru 强调浏览器插件式的内部知识卡片",
            "Document360@功能": "Document360 面向对外帮助中心与文档站点",
        },
        # Guru 与 Document360，故补上其官网域（Guru=getguru.com，
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


def get_reference_by_dimension(task_id: str) -> dict[str, str]:
    """按 task_id 查 Golden Case 的「分维度标准答案」（reference_by_dimension）。

    用于检索质量评估 evaluate_retrieval 的 context_precision —— 该指标需要
    每个维度各自的 ground truth（reference_map），把该维度的检索上下文与之
    比对。标准答案按维度组织，每个维度给出该维度的关键事实点。

    Args:
        task_id: Golden Case 的 task_id（如 "golden_001"）

    Returns:
        {dimension: 该维度的标准答案}；找不到返回空 dict
    """
    for case in GOLDEN_DATASET:
        if case["task_id"] == task_id:
            return case.get("reference_by_dimension", {})
    return {}


def get_reference_by_cell(task_id: str) -> dict[str, str]:
    """按 task_id 查 Golden Case 的「分 cell 标准答案」（reference_by_cell）。

    键 = `"竞品@维度"`，值 = **只讲该竞品**在该维度上的事实点。

    【为什么必须有 cell 级，而不是复用维度级】
      ragas_core 的评估单元是 (竞品, 维度)——每条 retrieval_snapshot 就是
      一个 cell。维度级 `reference_by_dimension[维度]` 是**跨竞品复合句**
      （"飞书…；钉钉…"），把它喂给某个 cell，判据里必然有一半讲的是**对方
      竞品**、按定义不可能被检索结果支持 → **context_recall 天花板被钉在
      0.5**，context_precision 也系统性趋 0。
      2026-10-16 实测：`飞书@功能 cr=0.500` 正是"正好半句"的直接证据。

    ⚠️ 无法在数据期枚举 cell 键的情形：`competitors` 为空的 hard 档
    （Supervisor 探索模式）。此时按标准答案点名的产品写 cell 键，
    探索出的竞品名命中即用、未命中由 `_resolve_reference` 回退维度级。

    Args:
        task_id: Golden Case 的 task_id（如 "golden_001"）

    Returns:
        {"竞品@维度": 该 cell 的标准答案}；找不到返回空 dict
    """
    for case in GOLDEN_DATASET:
        if case["task_id"] == task_id:
            return case.get("reference_by_cell", {})
    return {}


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
