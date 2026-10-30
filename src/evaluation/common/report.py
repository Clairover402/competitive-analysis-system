# -*- coding: utf-8 -*-
"""三份 SUMMARY.md 共用的 markdown 小件：表格 / 涨跌 / 比例 / 落盘。

【职责】
  做   把「一份人读的评估总览」需要的机械部分收敛成四个函数。
  不做 组织内容——每类评估看什么指标差得远（意图看层分布，ragas 看四指标，
       记忆看取代判定），内容各自写，这里只提供排版。

【关键决策】
  · 抽公共件而不是各写一遍 ← 三份 summary 的表格与 Δ 列一模一样，重复三次
    就是三处漂移的开始（改了一处宽度另一处不动，读起来像两套标准）。
  · `delta` 不给等级、只给方向 ← 与 TREND.md 的既有纪律一致：跨配置的差值
    不能读成「变好了」。本函数只负责格式化，判定留给读的人。
  · `ratio` 输出 `命中/总数 = 百分比` ← memory 的用例是 8 条小样本，
    只报「87.5%」会让人以为是 875/1000，小样本必须带上分母。
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from src.evaluation.common.paths import relative


def table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    """markdown 表格。单元格里不要放换行。"""
    head = "| " + " | ".join(str(h) for h in headers) + " |"
    sep = "|" + "|".join(" --- " for _ in headers) + "|"
    body = ["| " + " | ".join("" if c is None else str(c) for c in row) + " |" for row in rows]
    return "\n".join([head, sep, *body])


def delta(now: float | None, prev: float | None, digits: int = 3) -> str:
    """涨跌列：`+0.073` / `-0.012` / `0.000` / `—`（缺对照）。

    只表达方向，不表达好坏 —— 差值可正可负，也常常落在噪声里。
    """
    if now is None or prev is None:
        return "—"
    diff = now - prev
    if abs(diff) < 0.5 * 10 ** -digits:
        return f"{0:.{digits}f}"
    return f"{diff:+.{digits}f}"


def ratio(hit: int, total: int) -> str:
    """`23/24 = 95.8%`；分母为 0 时返回 `0/0`。"""
    if total <= 0:
        return "0/0"
    return f"{hit}/{total} = {hit / total:.1%}"


def save(path: Path, text: str) -> str:
    """写文件并返回相对路径（便于调用方打印）。父目录自动建。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8", newline="\n")
    return relative(path)
