"""配置管理 — 通过 pydantic-settings 读取环境变量与 .env 文件。

环境变量覆盖 .env 文件的值。所有字段都有合理的默认值。
"""

from __future__ import annotations

from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """竞品分析系统全局配置。

    读取顺序：环境变量 > .env 文件 > 默认值。
    使用方式：settings = Settings(); api_key = settings.deepseek_api_key
    """

    model_config = SettingsConfigDict(
        env_file=str(__import__("pathlib").Path(__file__).resolve().parent.parent / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- LLM: DeepSeek Chat API ----
    deepseek_api_key: str = "competitive-analysis-system-key"
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"

    # ---- Database: PostgreSQL + pgvector ----
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_database: str = "competitive_analysis"
    pg_user: str = "postgres"
    pg_password: str = "competitive_analysis_pwd"

    # ---- Embedding: BGE-M3 (1024维) ----
    embedding_model: str = "BAAI/bge-m3"
    embedding_device: str = "cpu"

    # ---- Embedding/Rerank API（SiliconFlow 硅基流动）----
    # 【ECS 2G 内存方案】True = 走在线 API，不加载本地 ~2GB 模型；
    # False = 走本地 BGE-M3 / reranker 模型（原逻辑，需 ≥4G 内存）
    embedding_api_enabled: bool = False
    siliconflow_api_key: str = ""
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"

    # ---- Reranker: BGE-reranker-v2-m3 ----
    reranker_model: str = "BAAI/bge-reranker-v2-m3"

    # ---- 检索阈值（Analyzer）----
    # 【2026-10-16 提升为配置】原先是 analyzer.py 内的硬编码字面量。
    # 【为什么必须可配】它们是**绝对值**，而检索池是按 task 混池
    #   （similarity_search 的 WHERE 里只有 task_id），池子规模 = 竞品数 × 页数 ×
    #   每页 chunk 数，波动极大（实测 golden_001 只有 32 chunk；满配 5 竞品可到 ~108）。
    #   当池子 < top_k 时，该层检索退化为「全量返回」→ 维度区分度归零，
    #   且**静默发生**（外部只看到分数全 0）。提升为配置后可按任务规模调整，
    #   并由 analyzer.py 的退化告警（WARN）主动提示。
    vector_search_top_k: int = 60
    rerank_top_k: int = 15

    # ---- 任务规模上限（入口校验，2026-10-21 产品约束）----
    # 【为什么要有硬上限】任务内是 N×M 格，每格一次精排 + 一次 LLM 分析，
    #   成本线性涨只是次要问题，主要问题是**质量会静默劣化**：
    #   单任务实测均值 27.5 页 / 112.8 chunk（约 4 chunk/页），
    #   while vector_search_top_k 是 **绝对值 60 条 chunk**（≈14 页），
    #   所以竞品数一多，每个竞品分到的配额就被摊薄——
    #   实测 8 竞品 / 612 chunk 的任务里，某竞品在单一维度 **0/60 条**（整块归零），
    #   那格下游只能写 [数据不足]，而报告**看起来是完整的**。
    #   N=4 时池子仍能被 60 条覆盖到一半以上，是当前 top_k 能撑住的规模。
    # 【为什么现在是配置而不是写死在路由】上限会随 top_k / 采集量一起变，
    #   写死会让「调了 top_k 却忘了调上限」再现同一类静默缺格。
    max_competitors: int = 4
    max_dimensions: int = 4

    # ---- 意图识别分层漏斗（2026-10-27，L1 确定性 → L2 语义 → L3 LLM → L4 澄清）----
    # 【为什么阈值必须可配】floor / margin 是**经验值**，换语料就飘：
    #   floor 太低 → 域外问题被硬塞进某个意图；太高 → 全部下沉，L2 白付 embedding。
    #   上线前用 `intent/classifier.calibrate()` 在自己语料上扫一遍再定，别直接抄默认。
    # 【2026-10-27 实测标定，10 句 4 类】margin 是**唯一能把两组分开**的判据：
    #   应接受组 gap = 0.124 / 0.188 / 0.247 / 0.318 / 0.318（最小 0.124）
    #   应下沉组 gap = 0.051 / 0.052 / 0.062 / 0.067 / 0.074（最大 0.074）
    #   → 取 0.10 落在空档正中；原来的 0.03 会让"这东西怎么样"以 gap=0.067 蒙过 L2，
    #     被判成 verify 并答"没有可核对的事实记录"——**回答看着合理，实际是错接受**。
    # ⚠️ floor 分不开这两组（应下沉组的 top1 最高到 0.915，"飞书和钉钉谁更好"），
    #   它只能粗筛掉明显无关句，别指望它做精度。所以 0.55 只是第二道门。
    intent_funnel_enabled: bool = True
    intent_funnel_floor: float = 0.55
    intent_funnel_margin: float = 0.10

    # ---- 长期记忆写入（2026-10-27 M3：semantic 事实 + 时效）----
    # 【为什么加开关】写记忆是"任务完成后的附加动作"，一旦它出问题
    #   （embed 服务抖动 / 表被锁），最坏结果是"记忆没写进去"，
    #   但绝不能变成"任务被判失败"。开关是留给线上紧急止损用的：
    #   关掉即回到"每份报告都从零开始"，任务链路本身不受影响。
    # 【为什么没有 episodic 开关】episode 不落记忆表 —— 一次任务的经过由
    #   tasks + reports 承载（结构化查询、随版本更新）。要停掉情景记忆等于
    #   停掉任务表本身，那不是记忆开关能承担的事。
    #   fact 出错会污染 verify 的答案（拿一条已失效的价格当当前事实念给用户），
    #   必须能单独停。
    memory_semantic_enabled: bool = True
    # 一个任务最多提炼多少条事实。报告证据行常见 8~20 条，全写会把
    # 一个任务的结论淹没在同一次爬取的复述里；20 条已覆盖结论骨架。
    memory_semantic_max_per_task: int = 20
    # 取代判据：新事实与旧事实余弦 ≥ 此值且 subject 相同 → 旧事实转 superseded。
    # 0.90 而非 M2 冲突检测用的 0.85 —— 0.85 会把"飞书支持多维表格"和
    # "飞书支持自动化流程"（同竞品同维度、句式相近）判成同一条而误取代。
    memory_semantic_supersede_threshold: float = 0.90

    # ---- 程序性记忆（2026-10-27 M4：user_preferences）----
    memory_preferences_enabled: bool = True
    # 对话里识别偏好提案的**前置门**：命中这些词才花一次 LLM。
    # 不加门的代价是每轮对话多一次 LLM，而 90% 的轮次根本没在说偏好。
    preference_cue_keywords: str = (
        "以后,今后,默认,每次,都要,不要再,别再,习惯,偏好,总是,统一"
    )

    # ---- RAGAS 评估运行参数（ragas_core 唯一口径，构造见 llm_factory）----
    # 【2026-10-16 新增】主因是一次实测事故：`钉钉@功能` 的 context_precision
    #   恒为 NaN，日志只有一行 `Job[12]: TimeoutError()`。
    #   机制（对着 ragas 0.4.3 真代码）：aevaluate 用 run_config.timeout（默认 180s）
    #   给每个 (样本 × 指标) 作业套 `asyncio.wait_for`；而 ragas 的
    #   LLMContextPrecisionWithReference._ascore 对 retrieved_contexts 是**串行**
    #   逐条调 LLM（`for context in retrieved_contexts:`）。该 cell 有 12 条
    #   contexts / 14440 字 → 12 次串行 judge 调用 → 总耗时越过 180s
    #   → 整格被判超时 → 该作业返回 np.nan。
    #   故 timeout 必须可配（宁可慢，不可静默丢分）。
    ragas_eval_timeout: float = 600.0
    # ↑ 单个 (样本,指标) 作业的上限秒数，也是底层 HTTP 请求的 request_timeout
    #   （ragas 会用同一值覆盖 langchain_llm.request_timeout，见 llms/base.py）。
    #   留足「长 contexts 样本串行判定」的余量；正常样本远用不到。
    ragas_eval_max_workers: int = 8
    # ↑ 同时运行的作业数上限（默认 16 偏高）：4 样本 × 4 指标 = 16 个作业并发，
    #   每个作业内部还在串行打 judge —— 并发过高易触发上游限流 → 重试 → 更慢
    #   → 反而更容易撞上 timeout。降到 8 换取稳定性。
    ragas_eval_repeat: int = 1
    # ↑ 同一批样本重复跑几轮（>1 时对每个 (样本,指标) 取中位数）。
    #   动机：n=4 的样本量下 judge 方差大于效应本身（实测同 reference 同 contexts
    #   两轮 cr 分别 0.5 / 1.0），单轮分数不可读。
    #   ⚠️ 成本线性 ×N，默认 1（保持现状）；压方差时用 CLI `--repeat 3` 显式开启。

    # ---- Auth: JWT ----
    jwt_secret: str = ""  # 必须通过 .env 或环境变量设置，不给默认值
    jwt_expire_hours: int = 24

    # ---- Web Search: Tavily（AI Agent 专用搜索 API，免费层 1000 次/月）----
    # 【2026-09-26】解决 cn.bing.com 被官网/下载页 SEO 霸榜、搜不到点评文章的问题
    # tavily_api_key 为空时，web_search 自动跳过 Tavily 层，回退到 Bing → Sogou
    tavily_api_key: str = ""
    tavily_enabled: bool = True          # 总开关：False 强制关闭 Tavily（即使有 key）
    tavily_max_results: int = 10

    # ---- 采集配额（Collector）----
    per_competitor_page_cap: int = 8
    # ↑ 【2026-10-16 提升为配置】原为 collector.py 的 _PER_COMPETITOR_PAGE_CAP 常量。
    #   10-14 复核记录「38 个 (任务,竞品) 组合里 32 个正好采满 8 页」，但 10-16 实测
    #   golden_001 最终只有 12 URL / 32 chunk，远低于设计注释的 40 URL / 80~120 chunk
    #   → 说明「采满」并非普遍情况，**是否扩容应由采集漏斗日志决定**（Collector 新增
    #   了「采集漏斗」与「最终落库规模」两条日志）。默认保持 8，不擅自抬高成本。
    chunks_per_url: int = 1
    # ↑ 每个 URL 最多保留几条 chunk 进入候选池。原实现硬编码为 1（URL 去重时丢弃其余），
    #   导致每页约 2/3 内容被丢弃、且「留哪一条」由检索返回顺序决定（不可复现）。
    #   默认仍为 1 以保持既有行为与 token 预算；调到 2~3 可显著增加候选多样性。

    # ---- Web Fetch: Jina Reader 兜底（免费无 key，抓 SPA 正文）----
    # 【2026-09-26】实测 r.jina.ai 在当前网络不可达（ConnectTimeout，境外线路），
    # 默认关闭。保留开关以便未来网络环境变化时启用。
    # 当前抓 SPA 正文的主力方案改为 Tavily 的 include_raw_content（见 tools_web.py）。
    jina_reader_enabled: bool = False

    # ---- Limits ----
    max_concurrent_collectors: int = 3
    max_rounds_supervisor: int = 10
    token_bucket_capacity: int = 100
    llm_rpm_limit: int = 60

    @computed_field
    @property
    def embed_model_version(self) -> str:
        """向量版本标签（`memory_model_version` 列的值）—— 由模型名派生，不单独配。

        ★ 为什么不给它一个独立配置项：两处可配就一定会漂。`embedding_model`
          换成 bge-large 而版本标签还写着 bge-m3 时，检索会拿新模型的 query 向量
          去比旧模型的记忆向量——余弦值毫无意义，但**不报错**，只是排序变乱。
          派生就没有漂的余地。

        取模型名末段（"BAAI/bge-m3" → "bge-m3"）：组织前缀换了但模型没换时，
        向量空间其实没变，不该因此让全量历史记忆退出候选。
        """
        return self.embedding_model.split("/")[-1]

    @computed_field
    @property
    def database_url(self) -> str:
        """拼接 asyncpg 连接字符串。

        Returns:
            PostgreSQL DSN，格式：postgresql://user:password@host:port/database
        """
        return (
            f"postgresql://{self.pg_user}:{self.pg_password}"
            f"@{self.pg_host}:{self.pg_port}/{self.pg_database}"
        )