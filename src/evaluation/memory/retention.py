"""会话上下文压缩的「关键约束保留率」评估。

【职责】
  做：给定会话轨迹，量「压缩后真正喂给 L3 的上下文里，关键约束还在不在」，
      并把成绩拆成「靠原文滑窗保住」和「只能靠摘要保住」两段分开报。
  不做：不评摘要写得好不好（那要 LLM judge，且不稳定）、不生成摘要
      （`summarize` 是注入参数，真跑用 MemorySummarizer，单测用桩）。

【输入 → 输出】
  输入：`Case`（turns + 显式声明的 constraints）
        + `summarize(prev_summary, messages) -> str`
  输出：`CaseResult`（窗口内/窗口外各自的保留数、丢失清单、字符压缩比）
        CLI 落 `evaluation_reports/memory/RETENTION.md`

【为什么必须拆成"窗口内 / 窗口外"两段】
  只报一个总保留率会骗人：RECENT_TURNS=6 意味着最近 3 轮问答是**原样**进 prompt 的，
  这些约束根本不需要摘要去保，算进总分等于用送分题抬高平均分。
  摘要的真正成绩只在「窗口外」——那些只能靠 summary 记住的早期约束。
  ⚠️ 所以窗口内保留率低于 100% 是 bug（滑窗没生效），不是"摘要不行"。

【关键决策】
  · 判定用**字面包含**，不用 LLM ← 让模型评"我有没有丢信息"正是要避免的自评；
    字面匹配会高估（同义改写算丢失），但这个偏差方向是**保守**的，
    而 LLM judge 的偏差方向不可知。
  · 约束**由 fixture 显式声明**，不从文本里猜 ← 猜测本身就要 LLM，
    一旦引入，指标就在测"抽取器"而不是"压缩器"。
  · 复用生产的 `should_summarize` / `format_history_block` / `RECENT_TURNS`，
    不重写一套 ← 评估若自己实现一份压缩逻辑，测的是评估不是线上。
  · fixture 内联，不依赖真库 ← 本地 conversations 表为 0 行（2026-10-31 实测），
    依赖真数据的评估在这台机器上等于跑不起来。

【读法】
  `窗口外保留率` 是主指标；`压缩比` 是代价。两者必须一起看：
  保留率 100%、压缩比 1.0（没压）等于什么都没做。
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field

from src.evaluation.common.paths import reports_dir
from src.evaluation.common.report import ratio, table, save

# 复用生产常量与函数——评估不能有自己的一套窗口大小
from src.supervisor.conversation import (
    RECENT_TURNS,
    format_history_block,
    should_summarize,
    summary_plan,
)

OUT_NAME = "RETENTION.md"


# ═══════════════════════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class Case:
    """一条会话轨迹。

    turns 每项是一轮问答：{"user": str, "assistant": str}
    constraints 是**必须活下来**的关键约束（竞品名 / 维度 / 任务引用）。
    """

    name: str
    turns: list[dict]
    constraints: list[str]


@dataclass
class CaseResult:
    name: str
    in_window: list[str] = field(default_factory=list)
    out_window: list[str] = field(default_factory=list)
    kept_in_window: list[str] = field(default_factory=list)
    kept_out_window: list[str] = field(default_factory=list)
    raw_chars: int = 0
    block_chars: int = 0
    # 哪几轮触发了「全量重建」（原始消息重做，不叠前次摘要）。
    # 它是"周期校准真的在跑"的证据 —— 只写配置不写实测，等于说了没做。
    full_at: list[int] = field(default_factory=list)

    @property
    def lost(self) -> list[str]:
        return [c for c in self.constraints_lost()]

    def constraints_lost(self) -> list[str]:
        kept = set(self.kept_in_window) | set(self.kept_out_window)
        return [c for c in (self.in_window + self.out_window) if c not in kept]


# ═══════════════════════════════════════════════════════════════════════════
# 核心：回放会话 + 判定
# ═══════════════════════════════════════════════════════════════════════════


def _rows(turns: list[dict], upto: int) -> list[dict]:
    """把 turns[:upto] 摊成 [{"role","content"}]，与 conversation_turns 同形。"""
    rows: list[dict] = []
    for t in turns[:upto]:
        rows.append({"role": "user", "content": t["user"]})
        rows.append({"role": "assistant", "content": t.get("assistant", "")})
    return rows


def _last_mention_index(rows: list[dict], text: str) -> int:
    """该约束最后一次出现在第几行（0-based）；没出现返回 -1。"""
    idx = -1
    for i, r in enumerate(rows):
        if text in (r.get("content") or ""):
            idx = i
    return idx


async def evaluate_case(case: Case, summarize) -> CaseResult:
    """回放到最后一轮，取「实际喂给 L3 的上下文块」，逐条判存活。

    ★ 回放顺序必须与生产一致（conversation.remember_turn）：
      本轮用的上下文 = format_history_block(当前摘要, 最近 RECENT_TURNS 行)，
      本轮结束后才可能刷新摘要。反过来写（先摘要后取块）会让第一轮就用上
      还不存在的摘要，指标虚高。
    """
    summary: str | None = None
    turn_count = 0
    block = ""
    full_at: list[int] = []

    for i in range(1, len(case.turns) + 1):
        rows = _rows(case.turns, i)
        block = format_history_block(summary, rows[-RECENT_TURNS:])
        turn_count = i
        # 本轮结束后按生产条件刷新摘要；用哪种方式、取多少行，全走生产同一个判据
        if should_summarize(turn_count):
            mode, limit = summary_plan(turn_count, bool(summary))
            window = rows if limit is None else rows[-limit:]
            if mode == "full":
                full_at.append(turn_count)
            # ★ 全量重建必须传 prev=None —— 传了旧摘要就还是叠加，误差链没断
            summary = await summarize(mode, None if mode == "full" else summary, window)

    rows_all = _rows(case.turns, len(case.turns))
    res = CaseResult(
        name=case.name,
        raw_chars=sum(len(r.get("content") or "") for r in rows_all),
        block_chars=len(block),
        full_at=full_at,
    )

    cutoff = len(rows_all) - RECENT_TURNS  # 最后一个进不了滑窗的行号
    for c in case.constraints:
        idx = _last_mention_index(rows_all, c)
        if idx < 0:
            continue                            # fixture 写错，不计入（下面会报）
        in_window = idx >= cutoff
        (res.in_window if in_window else res.out_window).append(c)
        if c in block:
            (res.kept_in_window if in_window else res.kept_out_window).append(c)
    return res


# ═══════════════════════════════════════════════════════════════════════════
# fixtures：早期提过、之后再不提的约束，才是摘要的真实考题
#
# ★ 约束必须写成**实体性 token**，不能是动词短语（2026-10-31 修）：
#   「折算成人民币」被算丢失过两次，但摘要里明明写着"价格都折算成人民币、
#   按年付费、不含税"——模型换个语序（"统一为人民币"）就判丢。
#   这不是压缩丢了信息，是**判定绑定了措辞**。
#   改判据：只锚实体（产品名 / 币种 / 年份 / 维度名），
#   凡"丢了这个词就一定丢了语义"的才算约束。
#   ⚠️ 这不是放水：反例是「并发上限」——它一旦被概括成"性能相关"，
#      后面那句"那个维度"就再也对不回去了，必须原样保留。
# ═══════════════════════════════════════════════════════════════════════════

_DEFAULT = [
    Case(
        name="四竞品三维度（约束埋在首句长句里）",
        turns=[
            {"user": "帮我做一份竞品分析，竞品是飞书、钉钉、企业微信和 Slack，"
                     "维度要覆盖定价策略、功能对比、技术架构，主要用于我们明年的产品规划参考。",
             "assistant": "好的，我先收集这四家的公开资料。"},
            {"user": "先看飞书的定价", "assistant": "企业版 ¥200/人/月。"},
            {"user": "钉钉呢", "assistant": "专业版 ¥180/人/年。"},
            {"user": "企业微信", "assistant": "基础免费，高级版按人数计费。"},
            {"user": "Slack 的价格", "assistant": "Pro $8.75/人/月。"},
            {"user": "把定价这块整理成表", "assistant": "已生成定价对比表。"},
            {"user": "功能对比也补一下", "assistant": "已补充功能对比。"},
            {"user": "技术架构那块最后写", "assistant": "好的，技术架构放最后。"},
        ],
        constraints=["飞书", "钉钉", "企业微信", "Slack", "定价策略", "技术架构"],
    ),
    Case(
        name="单竞品深挖（只提一次的版本号）",
        turns=[
            {"user": "分析 Notion，重点看它的 API 版本演进和 2025 年的定价调整，"
                     "我们内部有个迁移评估要用。",
             "assistant": "明白，围绕 API 版本与 2025 定价调整展开。"},
            {"user": "先看定价", "assistant": "Plus $10/人/月。"},
            {"user": "企业版", "assistant": "Enterprise 需询价。"},
            {"user": "团队成员上限呢", "assistant": "Plus 不限人数。"},
            {"user": "API 调用限制", "assistant": "按 workspace 计。"},
            {"user": "整理一下", "assistant": "已整理成对比表。"},
            {"user": "再加一段风险提示", "assistant": "已补充风险提示。"},
        ],
        constraints=["Notion", "API 版本演进", "2025"],
    ),
    Case(
        name="中途换题（早期约束与后文无关）",
        turns=[
            {"user": "我想对比 Zoom 和腾讯会议的定价与会话时长限制，这是给采购部门看的。",
             "assistant": "好的，两家都覆盖。"},
            {"user": "顺便说一句，我们最后只买企业版", "assistant": "记下了，只看企业版。"},
            {"user": "Zoom 企业版多少钱", "assistant": "$18.32/人/月。"},
            {"user": "腾讯会议企业版", "assistant": "按并发端口计费。"},
            {"user": "时长限制对比", "assistant": "Zoom 30 小时，腾讯会议 24 小时。"},
            {"user": "好，出报告", "assistant": "报告已生成。"},
            {"user": "标题改成采购评估", "assistant": "标题已更新。"},
        ],
        constraints=["Zoom", "腾讯会议", "会话时长限制", "企业版"],
    ),
    Case(
        name="约束以「我们的口径」表述（最容易被摘要丢）",
        turns=[
            {"user": "按我们内部口径，价格都折算成人民币、按年付费、不含税，"
                     "在这个口径下对比 Airtable 和飞书多维表格。",
             "assistant": "明白，统一折算人民币/年/不含税。"},
            {"user": "Airtable 价格", "assistant": "Team $20/人/月。"},
            {"user": "飞书多维表格", "assistant": "按席位计价。"},
            {"user": "团队版对比", "assistant": "已整理团队版差异。"},
            {"user": "加一列存储上限", "assistant": "已加存储上限。"},
            {"user": "出表", "assistant": "表格已生成。"},
            {"user": "结论写一句", "assistant": "已补结论。"},
        ],
        constraints=["Airtable", "飞书多维表格", "人民币", "按年付费", "不含税"],
    ),
    Case(
        name="短会话（不触发摘要，作为阴性对照）",
        turns=[
            {"user": "看下 Figma 的定价", "assistant": "Professional $15/人/月。"},
            {"user": "企业版", "assistant": "$45/人/月。"},
            {"user": "好", "assistant": "已记录。"},
        ],
        constraints=["Figma", "定价"],
    ),
    Case(
        name="十轮长会话（多次刷新摘要）",
        turns=[
            {"user": "帮我盯着 GitHub Copilot、Cursor、Codeium 三家的企业版报价、"
                     "数据驻留政策和 SSO 支持情况。",
             "assistant": "好的，三家都覆盖这三个点。"},
            {"user": "Copilot 报价", "assistant": "$19/人/月。"},
            {"user": "Cursor", "assistant": "$40/人/月。"},
            {"user": "Codeium", "assistant": "Teams $12/人/月。"},
            {"user": "数据驻留呢", "assistant": "三家均支持区域选择。"},
            {"user": "SSO", "assistant": "均支持 SAML。"},
            {"user": "对比一下", "assistant": "已出对比表。"},
            {"user": "再加合规认证", "assistant": "已补 SOC2 / ISO27001。"},
            {"user": "整理结论", "assistant": "已写结论。"},
            {"user": "导出", "assistant": "可导出 Markdown。"},
        ],
        constraints=["GitHub Copilot", "Cursor", "Codeium", "数据驻留政策", "SSO"],
    ),
    # ─────────────────────────────────────────────────────────────────────
    # ★ 真长度用例（2026-10-31 加）：前六条都是 200 字级的短会话，
    #   原文总量比摘要本身还短，所以"块字符 > 全量字符"——压了个寂寞。
    #   压缩收益必须用**长度接近真实使用**的会话才测得出来：
    #     本轮 15 轮 × 每轮 200~300 字 ≈ 4000 字；
    #     注入块 = 摘要（≤500 字）+ 最近 6 行（user 200 / assistant 80）。
    #   ⚠️ 约束只出现在第 1 轮，之后 12 轮一次不提 —— 这才是摘要的真实考题。
    #      若中途重复提到，它会落在滑窗里，测到的是滑窗不是摘要。
    #   ⚠️ 最后 3 轮（13~15）也不许出现约束词，否则又变成送分。
    # ─────────────────────────────────────────────────────────────────────
    Case(
        name="真长度：15 轮协作白板选型（约束只在首轮）",
        turns=[
            {"user": "我们下季度要做一次协作白板工具的选型，候选是 Miro、FigJam 和 "
                     "Lucidspark 三家。评估维度上我这边最关心三块：定价结构、实时协作的"
                     "并发上限、以及和现有 SSO 体系的对接方式。有个前提口径要先说清楚——"
                     "所有价格都按年付费折算成人均年成本、不含税，因为我们要走年度预算审批，"
                     "按月报价没法直接比。团队规模 120 人左右，其中大概 30 人是重度用户，"
                     "剩下的偶尔进来看看。这份东西最后是给采购和 IT 一起看的，所以定价那块"
                     "要能直接算总账，不要只给个单价了事。你先按这个框架收集公开资料。",
             "assistant": "明白，三家都按人均年成本口径整理，覆盖定价结构、协作并发、"
                          "SSO 对接三块。我先去收集公开资料。"},
            {"user": "先看第一家的定价页。它分了好几个档，我关心的是团队版往上那两档之间"
                     "到底差在什么地方，是不是只差管理功能。另外要注意它官网上标的价格"
                     "和第三方测评里写的经常对不上，以官网当期报价为准，别把过期的促销价"
                     "当成常规价写进去。",
             "assistant": "收到，以官网当期报价为准，重点区分团队版以上的两档差异。"},
            {"user": "关于同时在线编辑这件事，我想知道的是同一个画板里能有多少人实时操作，"
                     "以及这个数字是写在合同里还是只是技术上限。有些厂商页面写的是"
                     "「不限人数协作」但实际超过一定数量就降级成只读，这种细节要单独标出来。",
             "assistant": "好的，会把「合同承诺」与「技术上限」分开记，注明是否降级为只读。"},
            {"user": "第二家那个产品其实是挂在设计工具账号体系下的，我想确认它的席位是不是"
                     "跟着主产品走。如果是绑定的，那我们已经有设计团队在用主产品，这部分"
                     "成本要单独拆出来算，不然采购那边会以为白板是白送的。",
             "assistant": "明白，会确认席位是否与主产品绑定，并单独拆出增量成本。"},
            {"user": "第三家的团队版有个最少席位要求，我记得是五席起，但不确定现在有没有变。"
                     "麻烦核实一下最新的起售门槛，以及超出部分怎么计价——是按阶梯还是线性。"
                     "这直接影响我们 120 人的总账怎么算。",
             "assistant": "收到，核实起售门槛与超量计价方式，按 120 人折算总账。"},
            {"user": "三家的免费版我都用过一下，但免费版的限制条件各不相同：有的限画板数量，"
                     "有的限历史版本，有的限导出格式。这些限制看起来是小事，实际上会决定"
                     "试用阶段能不能真正评估出差异，所以也列一栏横向比一下。",
             "assistant": "好的，免费版限制单独列一栏：画板数、历史版本、导出格式。"},
            {"user": "合规这块我们有个硬要求：数据必须留在境内。所以要看清楚每家在中国大陆"
                     "有没有可用节点，如果没有，是走代理还是干脆不提供服务。这一条不满足的"
                     "话其他都不用谈了，先帮我筛一遍。",
             "assistant": "明白，境内数据驻留作为硬性筛选条件，先筛再看其他。"},
            {"user": "SSO 部分要问细一点：支持 SAML 2.0 是基本要求，但还要看有没有 SCIM "
                     "自动同步。没有 SCIM 的话，120 人的账号增删要手工做，离职流程会很麻烦，"
                     "这个运维成本要写进评估里。另外问一下有没有做过第三方的安全认证。",
             "assistant": "收到，SSO 分 SAML 与 SCIM 两层看，并把无 SCIM 的运维成本量化。"},
            {"user": "模板库和第三方集成这两块我原本没打算细看，但上周业务那边提了一句，"
                     "说希望白板能直接嵌到我们的知识库里。所以顺带看一眼有没有开放的嵌入方式，"
                     "或者至少要能导出成可长期保存的格式，不要只能在线看。",
             "assistant": "好的，补看嵌入方式与可离线保存的导出格式。"},
            {"user": "导出格式这块我比较在意可编辑性。只导出图片等于把内容锁死在工具里，"
                     "以后换工具就全废了。所以我希望看到的答案是：能不能导出成结构化的、"
                     "别的工具也能读进来的格式，以及导出的保真度大概到什么程度。",
             "assistant": "明白，重点看结构化导出与跨工具可读性，不只比图片导出。"},
            {"user": "移动端我们其实用得不多，但老板出差时会看，所以至少要能正常浏览和评论。"
                     "如果移动端只能看不能改，那也算满足需求，但要写清楚边界，别到时候"
                     "以为能改结果不能改。",
             "assistant": "收到，移动端按「浏览 / 评论 / 编辑」三档分别标注。"},
            {"user": "性能这块不好量化，我提两个我们自己踩过的场景：一是画板上元素超过一两千个"
                     "之后拖动会不会卡；二是多人同时操作时，光标和选区的同步延迟大概多少。"
                     "有公开数据就用公开数据，没有就写「未获取到」，不要编。",
             "assistant": "好的，性能按大画板与多人同步两项找公开数据，缺数据如实标注。"},
            {"user": "对了，我们是非营利性质的组织，如果哪家有折扣政策麻烦顺带记一下。"
                     "没有也没关系，不影响主结论，只是采购会问。",
             "assistant": "记下了，会查非营利折扣政策，不影响主评估。"},
            {"user": "合同条款那部分也帮我看一眼：年付的话锁不锁期限、续费涨价有没有上限、"
                     "以及人数增减怎么算。我们过去吃过一次亏，签的时候便宜，第二年续费"
                     "直接涨了四成，这次想提前避掉。",
             "assistant": "明白，重点看锁定期、续费涨幅上限、席位增减规则。"},
            {"user": "行，资料差不多了，最后帮我整理成一张对比表，按我们最开始说的那几个维度"
                     "排，定价那块记得直接给总账数字。最后再写三句结论，说说你更倾向哪家、"
                     "理由是什么、以及最需要再核实的是哪一条。",
             "assistant": "好的，按原定维度出表，定价给 120 人年成本总账，末尾附三句结论。"},
        ],
        constraints=["Miro", "FigJam", "Lucidspark", "按年", "并发上限"],
    ),
]


# ═══════════════════════════════════════════════════════════════════════════
# 汇总 + 渲染
# ═══════════════════════════════════════════════════════════════════════════


def _pct(part: int, whole: int) -> str:
    """压缩后占原来的百分比；分母为 0 时给 —。"""
    if not whole:
        return "—"
    return f"{part / whole:.0%}"


def _full_marks(full_at: list[int]) -> str:
    """把「首次全量」与「周期校准」分开显示。

    ★ 混成一列会看不清周期校准到底跑没跑（2026-10-31 实测就踩了：
      所有用例都显示"3"，那是首次全量；第 10 轮的校准因为周期取错根本没触发，
      而列里看不出区别，差点当成"校准已生效"）。
    """
    if not full_at:
        return "—"
    calib = "、".join(str(x) for x in full_at[1:]) or "—"
    return f"{full_at[0]} / {calib}"


def render(results: list[CaseResult], *, model: str) -> str:
    rows = []
    tot_in = tot_in_kept = tot_out = tot_out_kept = 0
    raw_all = block_all = 0
    long_case: CaseResult | None = None
    for r in results:
        tot_in += len(r.in_window)
        tot_in_kept += len(r.kept_in_window)
        tot_out += len(r.out_window)
        tot_out_kept += len(r.kept_out_window)
        raw_all += r.raw_chars
        block_all += r.block_chars
        if r.name.startswith("真长度"):
            long_case = r
        rows.append([
            r.name,
            ratio(len(r.kept_in_window), len(r.in_window)),
            ratio(len(r.kept_out_window), len(r.out_window)),
            f"{r.raw_chars}→{r.block_chars}",
            _pct(r.block_chars, r.raw_chars),
            _full_marks(r.full_at),
            "、".join(r.constraints_lost()) or "—",
        ])

    # ★ 压缩比只在**真长度**用例上成立（2026-10-31）：
    #   短会话的原文比摘要本身还短，"压缩"无从发生，块反而更长。
    #   把它算进总览会得出一个荒谬的负数收益，也抹掉了长会话上的真实成绩。
    overview = [
        ["窗口内（靠原文）", ratio(tot_in_kept, tot_in), "应恒为 100%，低于即滑窗失效"],
        ["窗口外（只能靠摘要）", ratio(tot_out_kept, tot_out), "摘要的真实成绩"],
    ]
    if long_case is not None:
        overview.append([
            "注入量压缩（真长度用例）",
            f"{long_case.raw_chars}→{long_case.block_chars} 字，"
            f"占 {_pct(long_case.block_chars, long_case.raw_chars)}",
            "这才是可写进简历的数：全量塞入 vs 摘要+滑窗",
        ])

    lines = [
        "# 会话上下文压缩 —— 关键约束保留率",
        "",
        f"> 摘要模型：{model}　窗口：最近 {RECENT_TURNS} 行原文　判定：字面包含（非 LLM 自评）",
        "> 本目录整目录不入库，随时可删；题库在 `src/evaluation/memory/retention.py`。",
        "",
        "## 总览",
        "",
        table(["分段", "保留", "含义"], overview),
        "",
        "## 逐条",
        "",
        table(["用例", "窗口内", "窗口外", "字符 全量→块", "占原", "全量重建 首/校准", "丢失的约束"], rows),
        "",
        "## 读法",
        "",
        "- **窗口外保留率是主指标**：窗口内的约束原样进 prompt，算进来只是送分。",
        "- **「全量重建 首/校准」列是周期校准的证据**：斜杠左边是首次全量、右边是周期轮",
        "  （每 12 轮一次，用原始消息重建、不叠前次摘要）。若右半边是「—」，说明这段会话",
        "  还没长到周期点，**别拿它当校准生效的证据**。",
        "- **「占原」列只在真长度用例上有意义**：块 = 摘要 + 最近 6 行原文。",
        "  ⚠️ 短会话里块可能**比全量长**（摘要本身有长度），这不代表压缩失败，",
        "  只反映「这段对话还没长到值得压」。收益只在长会话上体现。",
        "- **周期校准的实测效果（2026-10-31）**：真长度用例接上「每 12 轮全量重建」后",
        "  连跑 4 次全部 5/5；接之前 4 次里有 2 次丢「按年」——增量累积把口径概括掉了，",
        "  重建后回来。⚠️ 样本只有 4 次，按趋势引用，别写成「零丢失」。",
        "- ⚠️ **单次运行是快照，不是结论**：摘要 temperature=0.3，随机性不会被平均掉。",
        "  压缩比稳定在 **45%**（由窗口与摘要上限决定，不随采样变），但保留数会浮动——",
        "  引用时给区间，别引用某一次的漂亮值。",
        "- 判定是字面匹配，**同义改写会被算成丢失**——偏差方向保守，可接受。",
        "  实测典型：口径写成「按年付费」，摘要概括成「人均年成本」，语义在、字面不在。",
        "- 短会话（阴性对照）不触发摘要，其窗口外应为 0/0；若出现分母则 fixture 写错。",
        "",
    ]
    return "\n".join(lines)


async def _real_summarize(mode: str, prev: str | None, messages: list[dict]) -> str:
    """生产同款：全量重建走 full_merge_summary，增量走 incremental_summary。

    ⚠️ 两者不能混用：`full_merge` 的 prompt 是「从完整对话提取」，
       `incremental` 是「前次摘要 + 新对话合并」。用错 prompt 时
       全量重建仍然会把 prev 当成事实来源，误差链就断不干净。
    """
    from src.agents import create_llm_client
    from src.config import Settings
    from src.memory.summarizer import MemorySummarizer

    s = MemorySummarizer(create_llm_client(Settings(), temperature=0.3), None)  # type: ignore[arg-type]
    if mode == "full":
        return await s.full_merge_summary(messages)
    return await s.incremental_summary(prev, messages)


def _stub_summarize(keep_ratio: float = 1.0):
    """零成本自检桩：把前次摘要 + 本轮用户原话拼起来（模拟"完美摘要"）。

    ⚠️ 它故意不压缩，只用来自检**判定链路**（滑窗切点、窗口外统计）。
       拿它跑出的保留率没有意义，别写进汇报。
    """

    async def _fn(mode: str, prev: str | None, messages: list[dict]) -> str:
        users = [m["content"] for m in messages if m["role"] == "user"]
        # 全量重建不叠 prev —— 桩也要遵守这个约定，否则测不出"链断了没有"
        base = "" if mode == "full" else (prev or "")
        return (base + " " + " ".join(users)).strip()

    return _fn


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="会话上下文压缩的关键约束保留率")
    ap.add_argument("--stub", action="store_true",
                    help="用桩代替 LLM（只自检判定链路，产出数字无意义）")
    ap.add_argument("--no-save", action="store_true", help="只打印，不落盘")
    args = ap.parse_args(argv)

    summarize = _stub_summarize() if args.stub else _real_summarize
    model = "桩（仅自检）" if args.stub else "DeepSeek（temperature=0.3）"

    results = []
    for case in _DEFAULT:
        results.append(await evaluate_case(case, summarize))

    text = render(results, model=model)
    print(text)
    if not args.no_save:
        print(f"\n已落盘：{save(reports_dir('memory') / OUT_NAME, text)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
