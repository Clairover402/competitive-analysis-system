# -*- coding: utf-8 -*-
"""评估产物的落盘位置 —— 全仓唯一一处「算仓库根」的地方。

【职责】
  做   给出每类评估的产物目录（`evaluation_reports/<name>/`），不存在就建。
  不做 决定产物长什么样、管入库策略（那是 .gitignore 的事）。

【关键决策】
  · 为什么单拎一个模块 ← 四个评估各写一遍 `Path(__file__).resolve().parents[N]`，
    只要有一个包多一层目录就错位。2026-10-30 把 intent 收进子包时就踩过：
    `parents[2]` 从仓库根变成了 `src/`，打印路径悄悄少了前缀，不报错。
    层数只能在一个文件里跟位置绑定。
  · 产物根放在**仓库根**而不是 `src/` 下 ← 运行产物不是源码。原先
    `src/evaluation/evaluation_runs/` 与 `intent/data/` 两套并存，同一件事
    两种约定，且一个入库一个不入库。
  · ⚠️ `evaluation_reports/` 整目录不入库。所以这里给的东西**随时可删**，
    不要往里放「只此一份」的资料（题库在 cases.py / dataset.py，那才是源码）。

【读法】
  产物目录 = `evaluation_reports/<类名>/`，类名与子包同名：intent / ragas / memory。
"""

from __future__ import annotations

from pathlib import Path

# src/evaluation/common/paths.py → parents[0]=common [1]=evaluation [2]=src [3]=仓库根
# ⚠️ 本文件所在层级一变，这个下标必须跟着改（唯一的绑定处）。
REPO_ROOT = Path(__file__).resolve().parents[3]

REPORTS_DIRNAME = "evaluation_reports"
REPORTS_ROOT = REPO_ROOT / REPORTS_DIRNAME


def reports_dir(name: str, *, create: bool = True) -> Path:
    """`evaluation_reports/<name>/`，默认顺手建出来。"""
    path = REPORTS_ROOT / name
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def relative(path: Path) -> str:
    """打印用相对路径，别把 `D:\\AAAagent\\...` 甩到终端。"""
    try:
        return str(Path(path).resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)
