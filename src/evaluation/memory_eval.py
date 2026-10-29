"""长期记忆最小评估集 —— 23 条，四个指标。

【职责】
  在**真库 + 真 embedding** 上跑三组断言，回答"长期记忆到底准不准"：
    · 取代组（8 条）—— 同 subject 的新观测该不该把旧的顶掉
    · 命中组（7 条）—— 按 subject 精确键查，会不会串竞品、会不会漏
    · 保鲜组（8 条）—— 按 observed_at + ttl_days 判 fresh/stale 对不对
  另出一个派生指标"时效标注率"：返回的每条 fact 是否都带齐
  observed_at / age_days / ttl_days（缺一个，用户就不知道这结论多旧）。

【为什么必须有这个文件】
  意图识别有 150 条评估集对着，记忆侧此前只有一个案（"我上上个任务是啥"念对了）。
  面试追问「你怎么证明长期记忆有效」时，只有个案等于没有答案。
  更硬的一处：取代阈值 0.90 一直是拍的，答不出"为什么不是 0.85"。
  取代组 + `--sweep` 就是为了把这个数标定掉。

【与生产链路的边界，别搞混】
  本文件复刻的是 `semantic.write_task_facts` 里**第 205~250 行那一段**：
  build_subject → build_fact_content → ttl_days_for → find_active_similar → insert → supersede。
  被复刻的是"编排顺序"，判定本身（`find_active_similar` 的同 subject + 余弦、
  `supersede` 的 status='active' 条件、`check_freshness` 的全部判定）都是**直接调用生产代码**。
  没复刻的是"evidence_map → fact"那段升格（要真任务 + 真报告 + 真证据行）。
  所以本评估**不证明**升格链路对，只证明"给定这些 fact，取代与时效的判定对"。

【读法】
  ⚠️ 数据隔离靠 user_id 前缀，跑前跑后各 DELETE 一次；前缀含时间戳，不会误删真数据。
  ⚠️ embedding 失败直接报错退出，**不静默降级**——没有向量时 find_active_similar
     一条候选都找不到，"取代判定 100%"会变成假的（跟 classifier 里
     "编码失败要当作放弃而不是分数全 0"是同一条纪律）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.config import Settings
from src.db.connection import create_pool
from src.db.dao import AgentMemoryDAO, PreferenceDAO
from src.mcp.tools_rag import embed_texts
from src.memory.long_term import importance_for
from src.memory.semantic import build_fact_content, build_subject
from src.supervisor.query_handlers import check_freshness, ttl_days_for

_OUT_DIR = Path(__file__).parent / "evaluation_runs"
_USER_PREFIX = "mem-eval"


# ── 用例定义 ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Step:
    """一次事实写入：竞品 + 维度 + 结论。"""

    competitor: str
    dimension: str
    claim: str


@dataclass(frozen=True)
class SupersedeCase:
    """两组写入，第二次写完之后第一条该不该被取代。

    `should_supersede=False` 的用例才是真正在防"误合并"——
    把两条不同的真信息判成同一条，代价不是多一条噪音，是**丢掉一条真信息**
    （旧行被标 superseded，verify 的 status='active' 就不再返回它）。
    """

    name: str
    first: Step
    second: Step
    should_supersede: bool
    why: str


@dataclass(frozen=True)
class HitCase:
    """给定一批 fact，按竞品+关键词查，该返回哪些 subject。"""

    name: str
    seeds: tuple[Step, ...]
    competitors: tuple[str, ...] | None
    keyword: str | None
    expect_subjects: tuple[str, ...]
    why: str


@dataclass(frozen=True)
class FreshCase:
    """一条 fact 该被判成 fresh 还是 stale。"""

    name: str
    step: Step
    age_days: int
    expect_status: str
    overrides: dict[str, int] | None = None
    why: str = ""


# 取代组 8 条：6 条该取代、2 条不该取代
SUPERSEDE_CASES: tuple[SupersedeCase, ...] = (
    SupersedeCase(
        "价格改数字",
        Step("飞书", "定价", "飞书企业版定价为每人每月 200 元，年付享 9 折优惠"),
        Step("飞书", "定价", "飞书企业版定价已调整为每人每月 220 元，年付同享 9 折"),
        True,
        "同一档位的同一个数变了，就是新观测取代旧观测",
    ),
    SupersedeCase(
        "同维度不同侧面",
        Step("飞书", "功能", "飞书支持多维表格（Base），可用于搭建轻量业务系统"),
        Step("飞书", "功能", "飞书支持自动化流程与机器人，可编排跨应用审批流"),
        False,
        "同竞品同维度的两条不同能力，句式像但不是同一件事；"
        "判成同一条会删掉一条真信息——这正是必须先同 subject 再比余弦的理由",
    ),
    SupersedeCase(
        "同维度不同档位",
        Step("飞书", "定价", "飞书专业版定价 80 元/人/月，面向 100 人以下团队"),
        Step("飞书", "定价", "飞书企业版定价 200 元/人/月，含高级安全与审计能力"),
        False,
        "专业版和企业版是两条并存的报价，谁都不取代谁",
    ),
    SupersedeCase(
        "年费调整",
        Step("钉钉", "定价", "钉钉专业版年费 9800 元，不限使用人数"),
        Step("钉钉", "定价", "钉钉专业版年费调整为 12800 元，不限使用人数"),
        True,
        "同上，同一档位的价格上调",
    ),
    SupersedeCase(
        "规模更新",
        Step("飞书", "组织", "飞书团队规模约 6000 人，研发人员占比过半"),
        Step("飞书", "组织", "飞书团队规模已突破 8000 人，研发人员占比仍过半"),
        True,
        "组织人数是时点量，新数字取代旧数字",
    ),
    SupersedeCase(
        "跨竞品同句式",
        Step("飞书", "定价", "企业版 200 元/人/月，含高级安全与审计能力"),
        Step("钉钉", "定价", "企业版 200 元/人/月，含高级安全与审计能力"),
        False,
        "正文一模一样但说的是两家；这条测的是 subject 条件真的在起作用——"
        "若有人把 find_active_similar 的 subject 条件删了，它会红",
    ),
    SupersedeCase(
        "论断反转",
        Step("飞书", "功能", "飞书暂不支持私有化部署，仅提供公有云版本"),
        Step("飞书", "功能", "飞书已支持私有化部署，提供专有云交付选项"),
        True,
        "结论被推翻是最该取代的情形；措辞高度相似，余弦会很高",
    ),
    SupersedeCase(
        "免费额度收紧",
        Step("腾讯文档", "定价", "腾讯文档免费版最多允许 10 人协作编辑同一份文档"),
        Step("腾讯文档", "定价", "腾讯文档免费版调整为最多 5 人协作编辑同一份文档"),
        True,
        "数字变小同样是新观测",
    ),
)


# 命中组 7 条：读靠键，测的是"精确取 + 不串竞品"
HIT_CASES: tuple[HitCase, ...] = (
    HitCase(
        "同竞品不同维度不串",
        (Step("飞书", "定价", "飞书企业版定价 200 元/人/月，年付 9 折"),
         Step("飞书", "功能", "飞书支持多维表格与自动化流程编排")),
        ("飞书",), "定价",
        ("飞书|定价",),
        "关键词过滤要落在 content 上，不能把同竞品的功能事实也带回来",
    ),
    HitCase(
        "跨竞品不串",
        (Step("飞书", "定价", "飞书企业版定价 200 元/人/月，年付 9 折"),
         Step("钉钉", "定价", "钉钉专业版定价 9800 元/年，不限人数")),
        ("飞书",), None,
        ("飞书|定价",),
        "竞品过滤走 subject 前缀正则，不用 content LIKE"
        "（LIKE 会把「与飞书相比，钉钉…」串到飞书头上）",
    ),
    HitCase(
        "空库如实说没有",
        (),
        ("飞书",), "定价",
        (),
        "查不到就返回空 + 明说没有，不编理由",
    ),
    HitCase(
        "未知竞品不丢",
        (Step("", "定价", "该竞品企业版定价 158 元/人/月，年付同享折扣"),),
        ("未知竞品",), None,
        ("未知竞品|定价",),
        "认不出竞品时用「未知竞品|综合」兜底而不是跳过，宁可多留一条也不丢",
    ),
    HitCase(
        "同 subject 多档位并存",
        (Step("飞书", "定价", "飞书专业版定价 80 元/人/月，面向百人以下团队"),
         Step("飞书", "定价", "飞书企业版定价 200 元/人/月，含高级安全审计")),
        ("飞书",), "定价",
        ("飞书|定价", "飞书|定价"),
        "不取代的两条都要在——命中数 2 说明保鲜判定没有把并存的档位吞掉",
    ),
    HitCase(
        "被取代的旧观测不再出现",
        (Step("飞书", "定价", "飞书企业版定价为每人每月 200 元，年付享 9 折"),
         Step("飞书", "定价", "飞书企业版定价已调整为每人每月 220 元，年付同享 9 折")),
        ("飞书",), "定价",
        ("飞书|定价",),
        "verify 只该看到当前有效的那一条（status='active'）",
    ),
    HitCase(
        "无关键词取全部",
        (Step("飞书", "定价", "飞书企业版定价 200 元/人/月，年付 9 折"),
         Step("飞书", "功能", "飞书支持多维表格与自动化流程编排")),
        ("飞书",), None,
        ("飞书|定价", "飞书|功能"),
        "不指定关键词时返回该竞品全部 fact，顺序按 observed_at 倒序",
    ),
)


# 保鲜组 8 条：ttl 来自内置关键词表，override 用例额外写偏好
FRESH_CASES: tuple[FreshCase, ...] = (
    FreshCase(
        "定价 5 天前",
        Step("飞书", "定价", "飞书企业版定价 200 元/人/月，年付享 9 折"),
        5, "fresh",
        why="定价档 30 天，5 天前当然还在保鲜期内",
    ),
    FreshCase(
        "定价 40 天前",
        Step("飞书", "定价", "飞书企业版定价 200 元/人/月，年付享 9 折"),
        40, "stale",
        why="超过 30 天档位",
    ),
    FreshCase(
        "融资 60 天前",
        Step("某公司", "融资", "该公司完成 B 轮融资，估值约 30 亿美元"),
        60, "fresh",
        why="融资档 90 天，比定价宽——价格变得快、融资轮次变得慢",
    ),
    FreshCase(
        "融资 100 天前",
        Step("某公司", "融资", "该公司完成 B 轮融资，估值约 30 亿美元"),
        100, "stale",
        why="超过 90 天档位",
    ),
    FreshCase(
        "组织 200 天前",
        Step("飞书", "组织", "飞书团队规模约 6000 人，研发人员占比过半"),
        200, "stale",
        why="组织档 180 天",
    ),
    FreshCase(
        "偏好把定价改成 7 天",
        Step("飞书", "定价", "飞书企业版定价 200 元/人/月，年付享 9 折"),
        10, "stale",
        overrides={"定价": 7},
        why="★ 落库 ttl_days 是 30，但用户配了 7 天 ⇒ 10 天前就该判超期。"
            "这条同时验证「偏好 > 落库快照」，若读回 30 天会判 fresh",
    ),
    FreshCase(
        "偏好把组织放宽到 365 天",
        Step("飞书", "组织", "飞书团队规模约 6000 人，研发人员占比过半"),
        200, "fresh",
        overrides={"组织": 365},
        why="用户把组织档放宽后，200 天前的不该再算超期",
    ),
    FreshCase(
        "正好卡在 30 天",
        Step("飞书", "定价", "飞书企业版定价 200 元/人/月，年付享 9 折"),
        30, "stale",
        why="判据是 age >= ttl（不是 >），边界当天就算超期；改这个不等号要来这里看",
    ),
)

ALL_CASE_COUNT = len(SUPERSEDE_CASES) + len(HIT_CASES) + len(FRESH_CASES)


# ── 执行 ────────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _write_fact(
    dao: AgentMemoryDAO,
    *,
    user_id: str,
    step: Step,
    vec: list[float],
    observed_at: datetime,
    threshold: float,
) -> tuple[str, int, str]:
    """写一条 fact 并做取代判定，返回 (memory_id, 取代条数, subject)。

    ⚠️ 这一段是 `semantic.write_task_facts` 第 205~250 行的复刻（顺序一致）：
      先按 subject 找旧观测、再 insert、最后 supersede。
      **不能调换**：insert 之后新行自己也会被 find_active_similar 命中（相似度 1.0），
      结果是"新行取代自己"。
    """
    subject = build_subject(step.competitor, step.dimension)
    content = build_fact_content(
        competitor=step.competitor,
        dimension=step.dimension,
        claim=step.claim,
        source_url="https://example.com/eval",
        observed_at=observed_at,
    )
    ttl = ttl_days_for(step.claim, step.dimension, None)
    olds = await dao.find_active_similar(user_id, vec, subject, threshold=threshold)
    # 记下实际相似度：只报"取代/不取代"看不出差多少，调阈值时没有依据
    max_sim = max((float(o["similarity"]) for o in olds), default=0.0)
    imp, half = importance_for("fact")
    new_id = await dao.insert(
        user_id=user_id,
        memory_type="fact",
        content=content,
        importance=imp,
        embedding=vec,
        source_task_id=None,
        half_life_days=half,
        observed_at=observed_at,
        ttl_days=ttl,
        evidence_url="https://example.com/eval",
        subject=subject,
        model_version="bge-m3",
    )
    for old in olds:
        await dao.supersede(old["id"], new_id)
    return new_id, len(olds), subject, max_sim


async def _run_supersede(
    pool, settings: Settings, base: str, vecs: dict[str, list[float]], threshold: float
) -> list[dict]:
    """取代组：每条用例独立 user_id，避免用例之间互相取代。"""
    dao = AgentMemoryDAO(pool)
    out: list[dict] = []
    for case in SUPERSEDE_CASES:
        uid = f"{base}-sup-{case.name}"
        first_id, _, first_subject, _ = await _write_fact(
            dao, user_id=uid, step=case.first,
            vec=vecs[case.first.claim], observed_at=_now() - timedelta(days=1),
            threshold=threshold,
        )
        # 先只读探一次真实相似度（threshold=0 不过滤），再走写入。
        # 这条数是调阈值的唯一依据：只报"取代/不取代"看不出离阈值多远。
        # 跨竞品用例的 subject 不同，这里会探到 0 条 → sim=None，本身就是预期。
        probe_subject = build_subject(case.second.competitor, case.second.dimension)
        probe = await dao.find_active_similar(
            uid, vecs[case.second.claim], probe_subject, threshold=0.0
        )
        sim = max((float(o["similarity"]) for o in probe), default=None)

        _, n_sup, second_subject, _ = await _write_fact(
            dao, user_id=uid, step=case.second,
            vec=vecs[case.second.claim], observed_at=_now(),
            threshold=threshold,
        )
        # 去库里核真实状态，不拿返回的计数当结论（计数错报是最难查的一类）
        status = await pool.fetchval(
            "SELECT status FROM agent_memories WHERE id = $1::uuid", first_id
        )
        got = status == "superseded"
        out.append({
            "name": case.name,
            "should_supersede": case.should_supersede,
            "got_supersede": got,
            "ok": got == case.should_supersede,
            "n_superseded": n_sup,
            "similarity": sim,
            "subject_same": first_subject == second_subject,
            "why": case.why,
        })
    return out


async def _run_hit(
    pool, settings: Settings, base: str, vecs: dict[str, list[float]], threshold: float
) -> list[dict]:
    dao = AgentMemoryDAO(pool)
    out: list[dict] = []
    for case in HIT_CASES:
        uid = f"{base}-hit-{case.name}"
        for step in case.seeds:
            await _write_fact(
                dao, user_id=uid, step=step,
                vec=vecs[step.claim], observed_at=_now() - timedelta(days=2),
                threshold=threshold,
            )
        res = await check_freshness(
            pool, uid,
            competitors=list(case.competitors) if case.competitors else None,
            keyword=case.keyword,
        )
        got = tuple(f"{i['competitor']}|{i['dimension']}" for i in res["items"])
        out.append({
            "name": case.name,
            "expect": list(case.expect_subjects),
            "got": list(got),
            "ok": sorted(got) == sorted(case.expect_subjects),
            "why": case.why,
        })
    return out


async def _run_fresh(
    pool, settings: Settings, base: str, vecs: dict[str, list[float]], threshold: float
) -> list[dict]:
    dao = AgentMemoryDAO(pool)
    prefs = PreferenceDAO(pool)
    out: list[dict] = []
    for case in FRESH_CASES:
        uid = f"{base}-fresh-{case.name}"
        if case.overrides:
            await prefs.upsert(uid, "ttl_overrides", case.overrides, source="user_set")
        await _write_fact(
            dao, user_id=uid, step=case.step,
            vec=vecs[case.step.claim],
            observed_at=_now() - timedelta(days=case.age_days),
            threshold=threshold,
        )
        res = await check_freshness(pool, uid, competitors=[case.step.competitor])
        items = res["items"]
        # 时效标注率：三个字段缺一个，用户就看不出这结论多旧
        annotated = bool(items) and all(
            i.get("observed_at") and i.get("age_days") is not None
            and i.get("ttl_days") for i in items
        )
        status = items[0]["status"] if items else "missing"
        out.append({
            "name": case.name,
            "expect": case.expect_status,
            "got": status,
            "ok": status == case.expect_status,
            "annotated": annotated,
            "ttl_days": items[0].get("ttl_days") if items else None,
            "ttl_from_user": items[0].get("ttl_from_user") if items else None,
            "age_days": items[0].get("age_days") if items else None,
            "why": case.why,
        })
    return out


# ── 汇总与渲染 ──────────────────────────────────────────────────────────────

def summarize(
    sup: list[dict], hit: list[dict], fresh: list[dict]
) -> dict:
    """算五个指标。取代组拆成两个方向报——合起来看会把误合并藏掉。"""
    should = [c for c in sup if c["should_supersede"]]
    should_not = [c for c in sup if not c["should_supersede"]]
    annotated = sum(1 for c in fresh if c["annotated"])
    return {
        "total_cases": ALL_CASE_COUNT,
        "supersede_recall": {
            "correct": sum(1 for c in should if c["ok"]), "n": len(should),
        },
        "supersede_precision": {
            "correct": sum(1 for c in should_not if c["ok"]), "n": len(should_not),
            "false_merge": sum(1 for c in should_not if c["got_supersede"]),
        },
        "hit": {"correct": sum(1 for c in hit if c["ok"]), "n": len(hit)},
        "freshness": {"correct": sum(1 for c in fresh if c["ok"]), "n": len(fresh)},
        "annotate": {"correct": annotated, "n": len(fresh)},
        "pass": all(
            c["ok"] for c in (*sup, *hit, *fresh)
        ),
        # 失败项一条不藏：报"7/8"却不说错哪条，等于没测
        "failures": [
            {"group": g, **c} for g, cs in
            (("取代", sup), ("命中", hit), ("保鲜", fresh))
            for c in cs if not c["ok"]
        ],
        "details": {"supersede": sup, "hit": hit, "freshness": fresh},
    }


def _pct(c: int, n: int) -> str:
    return f"{c / n * 100:.1f}%" if n else "—"


def _w(s: str) -> int:
    """显示宽度：中文占两列，按 len 补空格必然错位。"""
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in s)


def _pad(s: str, width: int, right: bool = False) -> str:
    gap = " " * max(0, width - _w(s))
    return (gap + s) if right else (s + gap)


def render(report: dict, threshold: float, sweep: list[dict] | None = None) -> str:
    L: list[str] = []
    L.append(f"长期记忆评估 · {report['total_cases']} 条 · 取代阈值 {threshold:.2f}")
    L.append("")
    L.append(_pad("指标", 20) + _pad("对/总", 9, True) + _pad("结果", 9, True) + "   备注")
    rows = [
        ("取代·该取代的", report["supersede_recall"], "新旧同一条观测，必须顶掉旧的"),
        ("取代·不该取代的", report["supersede_precision"], "判错就是丢一条真信息（误合并）"),
        ("事实命中", report["hit"], "按 subject 精确取，不串竞品、不漏"),
        ("过期识别", report["freshness"], "observed_at + ttl_days → fresh/stale"),
        ("时效标注", report["annotate"], "每条都带观测日/天数/保鲜期"),
    ]
    for name, s, note in rows:
        ratio = f"{s['correct']}/{s['n']}"
        L.append(_pad(name, 20) + _pad(ratio, 9, True)
                 + _pad(_pct(s["correct"], s["n"]), 9, True) + "   " + note)
    L.append("")

    L.append("取代判定逐条（sim = 新旧两条的余弦；— 表示 subject 不同，压根不参与比较）")
    for c in report["details"]["supersede"]:
        exp = "该取代" if c["should_supersede"] else "不该取代"
        mark = "✓" if c["ok"] else "✗"
        sim = "—" if c["similarity"] is None else f"{c['similarity']:.3f}"
        L.append("  " + _pad(exp, 10) + _pad(mark, 4) + _pad(c["name"], 18)
                 + _pad(sim, 7, True))
    L.append("")

    if report["failures"]:
        L.append("失败明细")
        for f in report["failures"]:
            got = f.get("got") if f["group"] != "取代" else f.get("got_supersede")
            L.append(f"  ✗ [{f['group']}] {f['name']}：预期 {f.get('expect', f.get('should_supersede'))}"
                     f"，实得 {got}")
            L.append(f"     为什么重要：{f['why']}")
        L.append("")
    else:
        L.append("失败明细：无")
        L.append("")

    if sweep:
        L.append("取代阈值扫描（同一批 8 条用例，只改 threshold，真跑三遍）")
        L.append("  " + _pad("阈值", 8) + _pad("该取代", 9, True)
                 + _pad("不该取代", 11, True) + _pad("误合并", 8, True) + "   结论")
        for s in sweep:
            tag = "← 当前" if abs(s["threshold"] - threshold) < 1e-9 else ""
            L.append("  " + _pad(f"{s['threshold']:.2f}", 8) + _pad(s["recall"], 9, True)
                     + _pad(s["precision"], 11, True) + _pad(str(s["false_merge"]), 8, True)
                     + "   " + tag)
        L.append("  判据：先要「不该取代」全对（误合并 = 丢真信息），"
                 "再在剩下的里选「该取代」最高的")
        L.append("")
    return "\n".join(L)


# ── 入口 ────────────────────────────────────────────────────────────────────

async def _collect_vectors(settings: Settings) -> dict[str, list[float]]:
    """所有 claim 一次批量编码；失败就抛，不降级。"""
    claims: list[str] = []
    for c in SUPERSEDE_CASES:
        claims += [c.first.claim, c.second.claim]
    for c in HIT_CASES:
        claims += [s.claim for s in c.seeds]
    claims += [c.step.claim for c in FRESH_CASES]
    uniq = list(dict.fromkeys(claims))
    vecs = await embed_texts(uniq, settings)
    if not vecs or len(vecs) != len(uniq):
        raise RuntimeError(
            f"embedding 失败（拿到 {len(vecs or [])}/{len(uniq)} 条）："
            "没有向量时取代判定会全部退化成「不取代」，指标会假性 100%"
        )
    return dict(zip(uniq, vecs))


async def run(threshold: float, sweep: bool, keep: bool) -> int:
    settings = Settings()
    pool = await create_pool(settings)
    base = f"{_USER_PREFIX}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    vecs = await _collect_vectors(settings)

    async def _clean() -> None:
        await pool.execute(
            "DELETE FROM agent_memories WHERE user_id LIKE $1", f"{base}%"
        )
        await pool.execute(
            "DELETE FROM user_preferences WHERE user_id LIKE $1", f"{base}%"
        )

    await _clean()
    try:
        sup = await _run_supersede(pool, settings, base, vecs, threshold)
        hit = await _run_hit(pool, settings, base, vecs, threshold)
        fresh = await _run_fresh(pool, settings, base, vecs, threshold)
        report = summarize(sup, hit, fresh)

        sweep_rows: list[dict] = []
        if sweep:
            for t in (0.85, 0.90, 0.95):
                s2 = await _run_supersede(pool, settings, f"{base}-s{t}", vecs, t)
                should = [c for c in s2 if c["should_supersede"]]
                not_should = [c for c in s2 if not c["should_supersede"]]
                sweep_rows.append({
                    "threshold": t,
                    "recall": f"{sum(1 for c in should if c['ok'])}/{len(should)}",
                    "precision": f"{sum(1 for c in not_should if c['ok'])}/{len(not_should)}",
                    "false_merge": sum(1 for c in not_should if c["got_supersede"]),
                })
    finally:
        if not keep:
            await _clean()

    text = render(report, threshold, sweep_rows or None)
    print(text)

    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = _OUT_DIR / f"memory_eval_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(
        json.dumps(
            {"threshold": threshold, "user_prefix": base,
             **report, "sweep": sweep_rows},
            ensure_ascii=False, indent=2, default=str,
        ),
        encoding="utf-8",
    )
    print(f"\n明细：{out}")
    if keep:
        print(f"⚠️ --keep：测试数据保留在 {base}% 下，自行清理")
    return 0 if report["pass"] else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="长期记忆最小评估集（23 条 · 真库实跑）")
    ap.add_argument("--threshold", type=float, default=None,
                    help="取代阈值，默认取 settings.memory_semantic_supersede_threshold")
    ap.add_argument("--sweep", action="store_true",
                    help="额外扫 0.85/0.90/0.95 三档，用来标定取代阈值")
    ap.add_argument("--keep", action="store_true",
                    help="保留测试数据（默认跑完删掉）")
    args = ap.parse_args(argv)
    threshold = args.threshold
    if threshold is None:
        threshold = Settings().memory_semantic_supersede_threshold
    return asyncio.run(run(threshold, args.sweep, args.keep))


if __name__ == "__main__":
    sys.exit(main())
