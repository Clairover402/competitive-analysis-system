# -*- coding: utf-8 -*-
"""Golden Dataset：离线评估的标准答案集（10 条，easy/medium/hard 三档）。

【职责】
  做   提供「跑什么」（title / competitors / dimensions）与「对什么答案」
       （两层粒度的 reference）。
  不做 打分、跑链路——本文件是纯数据 + 三个查询函数。

【两层 ground truth，新链路必须用 cell 级】
  ① `reference_by_cell["竞品@维度"]`（cell 级、单竞品）
     → ragas_core 四指标走这条。评估单元是 (竞品, 维度)，基准就必须只讲该竞品。
  ② `reference_by_dimension[维度]`（维度级、跨竞品复合）
     → 真实任务兜底（llm_factory.resolve_reference 的第三级回退）。

  ⚠️ 拿维度级复合句当某个 cell 的判据，必然有一半讲对方竞品、按定义不可能被支持
     ⇒ cr 天花板钉死 0.5（实测 飞书@功能 cr=0.500，正好半句）。

【六条写作纪律（写 reference 前必读）】
  1. 必须是事实点，不能是占位模板（"XX 维度关键事实：…等能力"），也不能是
     元描述（"两者的定价档位有差异"——只说有差异、不说差异是什么）。
  2. 必须事实点化、≥3 个「；」分隔的独立事实点 ← context_recall 的分母 =
     judge 切出的句子数；单句 → 分母恒 1 → 取值域只有 {0,1}，抖一次整格翻转
     （实测 [0,1,0]）。
  3. cell 级与维度级必须**事实一致**：cell 级由维度级复合句按竞品机械拆分而来，
     不新增、不虚构。
  4. 改写必须**独立于被测 contexts**：只写公开可核验的事实，不得照着 contexts
     反写——否则 cr 变成自证，失去评估意义。
  5. 不用「以…见长 / 主打 / 偏向」这类判断词 ← 它们需要全市场对比才成立，
     单页材料按定义支撑不了，judge 判 0 是对的。改写成「提供 / 面向 / 包含」。
     ⚠️ 现状：仍有 15 处 cell 值保留「以…见长」（EASY 的腾讯文档/石墨、MEDIUM 的
     飞书/钉钉/企业微信等），属未清的存量，别照抄。
  6. 键对不上时 llm_factory.resolve_reference 会显式 warning，不静默退化
     （静默拿维度名当标准答案属静默失真，比报错危险）。

【为什么需要它】
  评估不能只在 CI 里跑一遍 E2E 就说过；必须有标准答案级的对照集，
  类比 ML 的 test set——不是看一次结果，是持续回归的标尺。
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


# ── Golden Dataset：10 条，分三档 ──────────────────────────────────────────

GOLDEN_DATASET: list[GoldenCase] = [
    # ── EASY (4 条) ── 竞品明确 + 维度常见 + 覆盖面窄，Pipeline 直达
    {
        "task_id": "golden_001",
        "title": "飞书基础功能分析",
        "competitors": ["飞书", "钉钉"],
        "dimensions": ["功能", "定价"],
        "difficulty": "easy",
        # 事实级标准答案：锚定可验证事实点而非固定数值（数字有时效）。
        # 每格 3~4 个独立事实、用「；」分隔；为什么必须多句、为什么不能照 contexts
        # 反写，见模块头「写作纪律 2 / 4」。
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

    # ── MEDIUM (3 条) ── 多竞品/多维度，Pipeline 但采集与分析更重
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

    # ── HARD (3 条) ── 意图模糊、无明确竞品 → Supervisor 探索模式
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


# ── 工具函数 ────────────────────────────────────────────────────────────────

def get_by_difficulty(difficulty: str) -> list[GoldenCase]:
    """按难度（easy / medium / hard）筛选用例；难度不匹配返回空列表。"""
    return [case for case in GOLDEN_DATASET if case["difficulty"] == difficulty]


def get_reference_by_dimension(task_id: str) -> dict[str, str]:
    """按 task_id 取「分维度标准答案」；找不到返回空 dict。

    这是**维度级、跨竞品复合**的兜底口径：真实任务（非 Golden）没有 cell 表时用它，
    cr 天花板约 0.5。Golden 跑分必须同时传 cell 级（见 get_reference_by_cell），
    两级都传时 resolve_reference 优先 cell 级。
    """
    for case in GOLDEN_DATASET:
        if case["task_id"] == task_id:
            return case.get("reference_by_dimension", {})
    return {}


def get_reference_by_cell(task_id: str) -> dict[str, str]:
    """按 task_id 取「分 cell 标准答案」（键 `"竞品@维度"`，值只讲该竞品）。

    为什么必须有 cell 级：ragas_core 的评估单元是 (竞品, 维度)，拿维度级复合句
    当某个 cell 的判据，必然有一半讲对方竞品 ⇒ cr 天花板钉在 0.5
    （实测 飞书@功能 cr=0.500，正好半句）。

    ⚠️ hard 档 competitors 为空（Supervisor 探索模式），无法在数据期枚举 cell 键：
    此时按标准答案点名的产品写键，命中即用，未命中由 resolve_reference 回退维度级。

    找不到 task_id 返回空 dict。
    """
    for case in GOLDEN_DATASET:
        if case["task_id"] == task_id:
            return case.get("reference_by_cell", {})
    return {}


def get_pipeline_cases() -> list[GoldenCase]:
    """competitors 非空的用例（= IntentRouter 走确定性 Pipeline 的形态）。"""
    return [case for case in GOLDEN_DATASET if len(case["competitors"]) > 0]


def get_supervisor_cases() -> list[GoldenCase]:
    """competitors 为空的用例（= IntentRouter 走 ReAct 探索的形态）。"""
    return [case for case in GOLDEN_DATASET if len(case["competitors"]) == 0]
