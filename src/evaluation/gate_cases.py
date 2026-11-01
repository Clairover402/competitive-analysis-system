# -*- coding: utf-8 -*-
"""门禁的确定性用例 —— 断言的是**系统行为**，不是报告质量。

【职责】
  做   跑 7 个不依赖外部服务的用例，回答「坏输入来时系统怎么反应」：
       工具超时 / HTTP 403 / 空正文 / LLM 返回非法 JSON / 三条规则判据的样本。
  不做 判报告写得好不好（那是模型评分的事）；不连库、不调真 LLM、不联网。

【为什么放在 src/ 而不是 tests/】
  `tests/` 被 `.gitignore` 忽略。门禁的价值有一半是「任何人 clone 下来都能
  自己跑一遍」，放在看不见的地方等于没写。所以这里的用例是**交付物的一部分**，
  tests/ 下另有一份 pytest 版（跑得更快、能挂在全量测试里）。

【关键决策】
  · 断言「不得计成功」而不是「必须返回错误」← 抓取失败的正确行为是
    `text_content=""`，让下游的 200 字门禁把它当失败；抛异常也行，
    但那是实现细节。判据要钉在**可观测的契约**上。
  · 超时用例覆盖两个分支（未落表 / 已落表）← 只测一个分支的话，
    「命中就回放」这句话没有证据；而它恰好是省掉重复副作用的唯一依据。
  · 规则样本里放一份**真实事故复刻**（Asana 四种定价）← 没有它，
    R3 只是"一个能跑的函数"；有它才证明「这条规则拦得住当时那个事故」。

【读法】
  python -m src.evaluation.gate --inject
  每条打印 `[OK]/[XX] 用例名 · 观测到的事实`
"""

from __future__ import annotations

import asyncio

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import patch

import httpx

import src.mcp.server as server_module
import src.mcp.tools_web as tools_web
from src.agents.collector import _generate_keywords
from src.evaluation.gate_rules import (
    check_conflicts,
    check_source_coverage,
    check_structure,
)
from src.mcp.server import MCPServer


@dataclass
class CaseResult:
    name: str
    ok: bool
    detail: str


# ═════════════════════════════════════════════════════════════════════════════
# §1 用例替身（只实现被调用到的那几个方法）
# ═════════════════════════════════════════════════════════════════════════════


class _Audit:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def log(self, **kwargs) -> None:
        self.calls.append(kwargs)


class _Store:
    """结果日志替身。`get` 恒 None（模拟"开跑时还没有记录"），
    `get_record` 由 `land` 决定 —— 用它构造「我们超时的同时，另一次执行已落表」。"""

    def __init__(self, land: dict | None = None) -> None:
        self.land = land

    async def get(self, tool_call_id: str):
        return None

    async def save(self, tool_call_id, tool_name, task_id, args_digest, result) -> bool:
        return True

    async def get_record(self, tool_call_id: str):
        return self.land


class _Counter:
    def __init__(self, delay: float, result: dict | None = None) -> None:
        self.n = 0
        self.delay = delay
        self.result = result or {"ok": True}

    async def handler(self, args: dict):
        self.n += 1
        await asyncio.sleep(self.delay)
        return self.result


class _Resp:
    def __init__(self, status: int, body: bytes) -> None:
        self.status_code = status
        self.content = body
        self.text = body.decode("utf-8", "ignore")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            req = httpx.Request("GET", "https://example.com")
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=req,
                response=httpx.Response(self.status_code, request=req),
            )


class _Client:
    def __init__(self, resp: _Resp) -> None:
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def get(self, url: str, **kwargs):
        return self._resp


class _LLM:
    """只回一段时间内的固定文本，够测「非法 JSON → 模板兜底」。"""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    async def ainvoke(self, prompt):
        self.calls += 1
        return SimpleNamespace(content=self.content)


# ═════════════════════════════════════════════════════════════════════════════
# §2 工具行为用例（4 条）
# ═════════════════════════════════════════════════════════════════════════════


def case_tool_timeout() -> CaseResult:
    """工具挂死 → 走超时对账：未落表判未执行、已落表回放。不得直接判任务失败。"""
    name = "工具超时 → 超时对账（未落表抛回 / 已落表回放）"
    origin = server_module._TOOL_TIMEOUT_SECONDS.get("gate_probe")
    server_module._TOOL_TIMEOUT_SECONDS["gate_probe"] = 0.05
    try:
        def build(store, counter) -> MCPServer:
            server = MCPServer(settings=None, audit=_Audit())
            server.attach_idempotency(store)
            server.register(
                name="gate_probe",
                description="门禁用例",
                parameters={"required": ["text"], "properties": {"text": {"type": "string"}}},
                handler=counter.handler,
            )
            return server

        # ① 未落表 → 判未执行，抛回给上层重试（不是判任务失败）
        counter = _Counter(delay=0.4)
        missed = build(_Store(land=None), counter)
        raised = False
        try:
            asyncio.run(missed.invoke("gate_probe", {"text": "x"}, task_id="t", idem_key="k"))
        except asyncio.TimeoutError:
            raised = True

        # ② 已落表（另一个执行体在我们等待期间落库）→ 回放，不重复副作用
        counter2 = _Counter(delay=0.4)
        landed = {"result": {"url": "u", "replayed_by": "peer"}, "created_at": "now"}
        hit_server = build(_Store(land=landed), counter2)
        replayed = asyncio.run(
            hit_server.invoke("gate_probe", {"text": "x"}, task_id="t", idem_key="k")
        )

        ok = (
            raised
            and counter.n == 1
            and replayed == {"url": "u", "replayed_by": "peer"}
            and counter2.n == 1
        )
        hit_ok = replayed == {"url": "u", "replayed_by": "peer"}
        return CaseResult(
            name, ok,
            f"未落表→抛 TimeoutError({raised})，handler 各执行 1 次"
            f"({counter.n}/{counter2.n})，已落表→回放({hit_ok})",
        )
    finally:
        if origin is None:
            server_module._TOOL_TIMEOUT_SECONDS.pop("gate_probe", None)
        else:
            server_module._TOOL_TIMEOUT_SECONDS["gate_probe"] = origin


def case_http_403() -> CaseResult:
    """HTTP 403 + 空正文 → 判抓取失败，不得计成功。"""
    name = "HTTP 403 → 判失败（空正文，不得计成功）"
    stub = SimpleNamespace(jina_reader_enabled=False, playwright_fallback_enabled=False)
    with patch.object(tools_web.httpx, "AsyncClient", lambda **kw: _Client(_Resp(403, b"Forbidden"))):
        result = asyncio.run(tools_web.web_fetch("https://example.com/x", 5000, stub))

    text = result.get("text_content") or ""
    ok = (
        result.get("status_code") == 403
        and (result.get("error") or "").startswith("HTTP 403")
        and not text
    )
    return CaseResult(name, ok, f"error={result.get('error')!r} chars={len(text)}（必须为 0）")


def case_empty_body() -> CaseResult:
    """200 但正文 < 200 字 → 触发 Playwright 兜底；兜底仍短则判失败。"""
    name = "空正文(200) → Playwright 兜底；兜底仍短则判失败"
    stub = SimpleNamespace(jina_reader_enabled=False, playwright_fallback_enabled=True)
    skeleton = b"<html><head><title>SPA</title></head><body><div id='app'></div></body></html>"

    rescued = {"status_code": 200, "title": "SPA", "text_content": "正文" * 300, "error": ""}
    with patch.object(tools_web.httpx, "AsyncClient", lambda **kw: _Client(_Resp(200, skeleton))), \
         patch.object(tools_web, "_playwright_fetch", _async_return(rescued)):
        got = asyncio.run(tools_web.web_fetch("https://example.com/spa", 5000, stub))

    with patch.object(tools_web.httpx, "AsyncClient", lambda **kw: _Client(_Resp(200, skeleton))), \
         patch.object(tools_web, "_playwright_fetch", _async_return(None)):
        failed = asyncio.run(tools_web.web_fetch("https://example.com/spa", 5000, stub))

    ok = (
        got.get("fallback") == "playwright"
        and len(got.get("text_content") or "") > 200
        and len(failed.get("text_content") or "") < 200
    )
    return CaseResult(
        name, ok,
        f"兜底成功→fallback={got.get('fallback')!r} chars={len(got.get('text_content') or '')}；"
        f"兜底失败→chars={len(failed.get('text_content') or '')}（<200 即下游判失败）",
    )


def case_bad_json() -> CaseResult:
    """LLM 返回非 JSON → 降级到模板关键词，不抛异常中断采集。"""
    name = "LLM 非法 JSON → 模板关键词兜底（不中断采集）"
    llm = _LLM("抱歉，我无法完成这个请求。")
    keywords, domains = asyncio.run(
        _generate_keywords("飞书", ["定价", "功能"], llm)
    )
    ok = keywords == ["飞书 定价", "飞书 功能"] and domains == [] and llm.calls >= 1
    return CaseResult(
        name, ok,
        f"重试 {llm.calls} 次后 keywords={keywords} domains={domains}",
    )


def _async_return(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner


# ═════════════════════════════════════════════════════════════════════════════
# §3 规则判据样本（3 条）—— 用一份合格报告 + 两份坏报告验判据本身
# ═════════════════════════════════════════════════════════════════════════════

_GOOD = """# 示例报告

## 概述

两家都在做协同办公。

## 竞品对比总览

| 分析维度 | 飞书 | 钉钉 |
|---|---|---|
| 定价 | [数据不足] | 19 元/月，来源可查 |
| 功能 | 文档与多维表格 | 审批与考勤 |

## 逐维度深度分析

### 定价分析

钉钉定价 19 元/月。
来源：[https://www.dingtalk.com/](https://www.dingtalk.com/)

### 功能分析

飞书以文档协同为主。
来源：[https://www.feishu.cn/](https://www.feishu.cn/)

## 关键发现

1. 两家定位不同。

## 风险与建议

- 数据采集日期需标注。
"""

_BROKEN = """# 示例报告

## 概述

只有概述，没有总览表也没有逐维度分析。
"""

# 真实事故复刻：2026-10-28「项目管理工具」任务里 Asana 定价出现四种互斥口径，
# 当时质检只给了 7.5 分、系统照样接受 —— 因为没有任何规则拦它。库在 10-30 被
# 清空，原文已不可取，这里按当时的形态复刻（同一份报告里三处互斥的价）。
_ASANA_CONFLICT = """# 项目管理工具竞品分析

## 概述

Asana 面向中大型团队。

## 竞品对比总览

| 分析维度 | Asana |
|---|---|
| 定价 | $10.99/用户/月 |

## 逐维度深度分析

### 定价分析

Asana 的定价为 $12.99/用户/月，按团队规模分档。
来源：[https://asana.com/pricing](https://asana.com/pricing)

另有材料称 Asana 价格是 $10.99/用户/月。
来源：[https://asana.com/pricing](https://asana.com/pricing)

年付折算后约 $131.88/用户/年。
来源：[https://asana.com/pricing](https://asana.com/pricing)

## 关键发现

1. 定价口径不一致。

## 风险与建议

- 采购前需向厂商确认。
"""


# 表格失明回归样本：表格数据行里**没有竞品名**（名字只在表头），
# 按「行内出现竞品名」切窗口会整张表跳过。
# ⚠️ 上面 Asana 那份复刻样本现在命中的也是这条路径（表里 10.99 / 正文 12.99），
#    所以这里改验「纯表格内跨行」—— 三条样本各验一条路径，不互相顶替。
_TABLE_MULTI_ROW = """
| 分析维度 | Asana |
|---|---|
| 定价 | $10.99/用户/月 |
| 定价 | $12.99/用户/月 |
"""

# 同一句里并列两个价、既没有档位词也没有转折词：规则判不了真假。
# 判据要求它出现在「待确认」里 —— 只要不是静默消失就算合格。
_SAME_LINE_AMBIGUOUS = "Asana 定价 $10.99/用户/月，$12.99/用户/月。"


def rule_cases() -> list[CaseResult]:
    comps = ["飞书", "钉钉"]
    dims = ["定价", "功能"]

    good = check_structure(_GOOD, comps, dims)
    good_src = check_source_coverage(_GOOD)
    good_conf = check_conflicts(_GOOD, comps)
    ok_good = (
        good.ok == good.total == 4
        and good_src.ok == good_src.total == 2
        and good_conf.conflicts == 0
    )

    broken = check_structure(_BROKEN, comps, dims)
    ok_broken = broken.ok == 0 and broken.total == 4 and broken.notes

    asana = check_conflicts(_ASANA_CONFLICT, ["Asana"])
    ok_asana = asana.conflicts >= 1

    table = check_conflicts(_TABLE_MULTI_ROW, ["Asana"])
    ok_table = table.conflicts == 1 and "表格内跨行" in table.details[0]

    same = check_conflicts(_SAME_LINE_AMBIGUOUS, ["Asana"])
    ok_same = same.conflicts == 0 and same.ambiguous == 1 and bool(same.ambiguous_details)

    return [
        CaseResult(
            "规则样本 · 合格报告必须全绿",
            ok_good,
            f"R1 {good.ok}/{good.total} · R2 {good_src.ok}/{good_src.total} · "
            f"R3 {good_conf.conflicts}",
        ),
        CaseResult(
            "规则样本 · 缺章节必须 0 分（不能因为「有一章」就给分）",
            ok_broken,
            f"R1 {broken.ok}/{broken.total} · {broken.notes[:1]}",
        ),
        CaseResult(
            "规则样本 · Asana 四种定价必须判冲突（真实事故复刻）",
            ok_asana,
            f"R3 {asana.conflicts} · {asana.details[:1]}",
        ),
        CaseResult(
            "规则样本 · 表格内跨行两价必须抓到（表头列映射）",
            ok_table,
            f"R3 {table.conflicts} · {table.details[:1]}",
        ),
        CaseResult(
            "规则样本 · 同段两价判不了时必须进「待确认」而非消失",
            ok_same,
            f"冲突 {same.conflicts} · 待确认 {same.ambiguous} · {same.ambiguous_details[:1]}",
        ),
    ]


# ═════════════════════════════════════════════════════════════════════════════
# §4 入口
# ═════════════════════════════════════════════════════════════════════════════


def run_all() -> int:
    results: list[CaseResult] = [
        _guard(case_tool_timeout, "工具超时"),
        _guard(case_http_403, "HTTP 403"),
        _guard(case_empty_body, "空正文"),
        _guard(case_bad_json, "非法 JSON"),
        _guard(rule_cases, "规则样本", multi=True),
    ]
    flat = [r for item in results for r in ([item] if isinstance(item, CaseResult) else item)]

    print(f"=== 确定性用例 · {len(flat)} 条 ===")
    for r in flat:
        print(f"[{'OK' if r.ok else 'XX'}] {r.name}")
        print(f"     {r.detail}")
    failed = [r for r in flat if not r.ok]
    print(f"\n结论：{len(flat) - len(failed)}/{len(flat)} 通过 → exit {1 if failed else 0}")
    return 1 if failed else 0


def _guard(fn, label: str, multi: bool = False):
    """单个用例抛异常 → 记一条失败，别让整组用例停在这里。"""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — 用例本身炸了也是一种「红」
        return CaseResult(f"{label}（用例自身异常）", False, f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    raise SystemExit(run_all())
