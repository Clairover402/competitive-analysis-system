# -*- coding: utf-8 -*-
"""全量评估留痕与版本对比：run 归档 + compare + trend。

【职责】
  做   给每次评分落一个不可变 run 目录，并提供「两 run 逐 cell 对比」与趋势表。
  不做 跑指标、调 LLM、替人下结论——纯读 JSON，不依赖 ragas / DB。

【用法】
  python -m src.evaluation.run_archive compare <A> <B>    # A/B 可为 run_id 或 baseline
  python -m src.evaluation.run_archive trend [--write] [--since YYYY-MM-DD]
  python -m src.evaluation.run_archive baseline [--set <run_id>]

  跑分时自动归档（默认开）：ragas_core 在跑分路径上调
    handle = begin_run("golden", note="top_k 15→20")
    finish_run(handle, entries)

【产物：一个 run 恒定两个文件，形状不随内容变】
  evaluation_runs/
    TREND.md       全版本趋势（派生视图，trend --write 可整体重建）
    baseline.txt   当前基线 run_id（唯一允许覆盖写的对象）
    <run_id>/
      run.json     manifest（跑了什么）+ cases（每格分数 + 每轮原始分）
      summary.md   人读分数表

【三条设计原则（都是踩过的坑）】
  1. 长在唯一入口上 ← 旧 golden_run.py 有完整的 baseline/diff 机制，却从没有过一次
     真实归档，因为它需要人记得去调。故归档必须是跑分路径的一部分，默认开。
  2. run 目录不可变、纯增不替换 ← 能改的档案不是留痕，是笔记。跑错了就重跑一个新
     run；旧档永不补写、不回填、不删除，只在留痕文档里标 superseded。
  3. 复现三要素缺一不可：git commit / golden_hash / resolved settings。
     ⚠️ config 默认值（top_k=60 / page_cap=8）与 .env 实际值（15 / 20）不一致，
     所以必须 dump **解析后**的值——dump 默认值等于没记口径。

【对比纪律：只回答「变没变」，不回答「好不好」】
  · 只比同一个 cell（竞品@维度 × 指标），永不跨 cell 汇总下结论；
  · |Δ| 必须超过该 cell 自己的跨轮极差 θ，才算「疑似变化」，否则记「噪声内」；
  · faithfulness 是护栏指标，只呈现不判定。
  两条强制免责：golden_hash 不同 → 表头挂「数据集已变」；某 cell 的 n_contexts
  变化 >20% → 标注「材料量已变，Δ 含采集因素」。
  这是「脚本逼你守纪律」，不是「脚本替你做判断」。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# 归档格式版本：写进每个 run 的 manifest。当前只写不读——留作日后加字段时
# 让对比逻辑能识别「老档缺字段」，而不是靠猜测补 0。
ARCHIVE_SCHEMA = 1

# 归档目录与文件名的唯一出处（改这里就够，别在函数里散落字面量）。
ARCHIVE_DIRNAME = "evaluation_runs"
TREND_FILENAME = "TREND.md"
BASELINE_FILENAME = "baseline.txt"
RUN_JSON = "run.json"
SUMMARY_MD = "summary.md"

# 四指标名单与顺序必须与 ragas_core._METRIC_NAMES 一致。
# 这里**不 import** ragas_core：它在 import 期会 monkey-patch 并拉起 ragas，
# 而归档/对比必须在无 ragas 的环境里也能跑（纯读 JSON）。
METRICS = (
    "context_precision",
    "context_recall",
    "faithfulness",
    "answer_relevancy",
)
# 表格里的缩写列名（cp / cr / faith / ar）。
METRIC_SHORT = {
    "context_precision": "cp",
    "context_recall": "cr",
    "faithfulness": "faith",
    "answer_relevancy": "ar",
}
# 护栏指标：只呈现，不参与「变好 / 变坏」判定（铁律③）。
GUARDRAIL = {"faithfulness"}

# 取 project root（src/evaluation/run_archive.py → 上三级）。
PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ── 基础工具 ────────────────────────────────────────────────────────────────────


def _git(*args: str, root: Path | None = None) -> str | None:
    """跑一条 git 命令，失败返回 None（没有 git 也不该让归档崩掉）。"""
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(root or PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def git_info(root: Path | None = None) -> dict:
    """代码版本三件套：commit / dirty / branch。

    dirty 必须记：工作区有未提交改动时，别人拿这个 sha 复现不出同样结果，
    对比结论要打折（本模块只做标记，不阻止运行——拦着人跑更糟）。
    """
    commit = _git("rev-parse", "--short", "HEAD", root=root)
    branch = _git("rev-parse", "--abbrev-ref", "HEAD", root=root)
    status = _git("status", "--porcelain", root=root)
    return {
        "git_commit": commit or "nogit",
        "git_dirty": bool(status),
        "git_branch": branch or None,
    }


def golden_fingerprint() -> str:
    """Golden 数据集指纹 = 规范化 JSON 的 sha256。

    为什么需要：reference 是手改最多的东西，改完不一定立刻 commit。
    只看 git commit 会把「数据集变了」误判成「代码改进了」。
    sort_keys 保证键序不影响指纹（否则纯格式变动就假报「数据集已变」）。
    """
    from src.evaluation.golden_dataset import GOLDEN_DATASET

    payload = json.dumps(
        GOLDEN_DATASET, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_settings(settings=None):
    """拿到 resolved Settings：调用方给了就用它，没给就自己构造一份。

    为什么要「自己构造」而不是留空：manifest 的 settings / eval_config 是复现三要素
    之一，空着等于没记口径。构造失败（无配置环境）才退化为 None。
    """
    if settings is not None:
        return settings
    try:
        from src.config import Settings

        return Settings()
    except Exception:  # pragma: no cover - 无配置环境（CI/纯离线）
        return None


def _settings_dump(settings=None) -> dict:
    """resolved Settings 全量 dump（脱敏：凡是 key/secret/password/token 一律记 `***`）。

    为什么必须 resolved：同一个仓库，`.env` 不同 → 检索口径不同 → 分数不可比。
    config.py 默认 top_k=60，而本机 .env 是 15，只记默认值等于没记。
    """
    settings = _load_settings(settings)
    if settings is None:
        return {}
    try:
        fields = type(settings).model_fields
    except Exception:  # pragma: no cover - 非 pydantic 对象
        return {}
    out: dict = {}
    for name in fields:
        try:
            value = getattr(settings, name)
        except Exception:  # pragma: no cover
            continue
        if any(tag in name.lower() for tag in ("key", "secret", "password", "token")):
            out[name] = "***" if value else ""
        elif isinstance(value, (str, int, float, bool)) or value is None:
            out[name] = value
        else:
            out[name] = str(value)
    return out


def archive_root(root: Path | None = None) -> Path:
    """归档根目录（不存在则创建）。"""
    path = Path(root) if root else PROJECT_ROOT / ARCHIVE_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


# ── 归档：begin_run / finish_run ───────────────────────────────────────────────


@dataclass(frozen=True)
class RunHandle:
    """一次运行的句柄（begin_run 产出，finish_run 消费）。

    frozen：run_id 一旦定下就不许改——它是目录名，也是所有对比的键。
    kind / note 也存进来：finish_run 不该靠猜（`task_xxx` 这种 golden_id 也是真值，
    按「有没有 golden_id」推 kind 会把单评误标成 golden，进而污染趋势表）。
    """

    run_id: str
    dir: Path
    started_at: datetime
    kind: str = "golden"
    note: str | None = None


def _next_run_id(base: str, root: Path) -> str:
    """同分钟重跑时追加 -2 / -3，保证 run_id 唯一（目录名即主键）。"""
    candidate = base
    n = 2
    while (root / candidate).exists():
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def begin_run(
    kind: str = "golden",
    note: str | None = None,
    root: Path | None = None,
) -> RunHandle:
    """开始一次运行：定 run_id、建目录、记开始时间。

    run_id = `YYYYmmdd-HHMM-<短sha>`（dirty 追加 `-dirty`，同分钟重跑追加 `-2`）。
    为什么把 sha 塞进目录名：`ls evaluation_runs/` 就成了「代码版本 × 时间」的时间线，
    不打开任何文件也能看出哪几次是同一版本重复跑。
    """
    root = archive_root(root)
    now = datetime.now()
    info = git_info()
    base = f"{now:%Y%m%d-%H%M}-{info['git_commit']}"
    if info["git_dirty"]:
        base += "-dirty"
    run_id = _next_run_id(base, root)
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    handle = RunHandle(
        run_id=run_id, dir=run_dir, started_at=now, kind=kind, note=note
    )
    # note 先落一份「进行中」的占位，避免跑到一半崩溃后目录毫无线索。
    (run_dir / ".pending").write_text(
        json.dumps(
            {"run_id": run_id, "kind": kind, "note": note,
             "started_at": now.isoformat(timespec="seconds")},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return handle


def _case_key(case: dict) -> str:
    """case 在 cases 段里的键：Golden 用 golden_id，单评任务用 task_id。"""
    return str(case.get("golden_id") or case.get("task_id") or "unknown")


def _overall(cases: dict, metric: str) -> float | None:
    """全量聚合 = 各 case 该指标 aggregate 的均值（严格忽略 None，不按 0 补）。

    ⚠️ 这是「跨 case 汇总」，只供趋势表看方向（§七 铁律①：禁止引用绝对值）。
    真正的判定永远发生在逐 cell 层。
    """
    vals = []
    for case in cases.values():
        value = (case.get("aggregates") or {}).get(metric)
        if isinstance(value, (int, float)):
            vals.append(float(value))
    if not vals:
        return None
    return round(sum(vals) / len(vals), 3)


def _fmt(value, digits: int = 3) -> str:
    """数值渲染：None → 「—」（不显示 0，避免被读成「跑了但得了 0」）。

    ⚠️ 当前只判 None：float('nan') 会渲染成 "nan"。archived 数据来自 JSON，
    NaN 已在 ragas_core 侧归 None，故暂不处理；若以后引入外部 JSON 需补。
    """
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _delta(value: float | None) -> str:
    """Δ 渲染：带符号；None → 「—」。"""
    if value is None:
        return "—"
    return f"{value:+.3f}"


def build_summary(manifest: dict, cases: dict) -> str:
    """渲染 summary.md（人读分数表）。

    结构：头部运行状态行 → case 级总表 → 逐 case 的 cell 明细。
    为什么保留 cell 明细：聚合值掩盖分布，而「哪一格塌了」才是要修的东西。
    """
    m = manifest
    lines: list[str] = []
    lines.append(f"# {m['run_id']}")
    lines.append("")
    dirty = "（工作区有未提交改动，复现打折）" if m.get("git_dirty") else ""
    lines.append(
        f"- 类型：`{m.get('kind')}`　备注：{m.get('note') or '—'}　"
        f"时间：{m.get('started_at')}　耗时：{m.get('duration_sec')}s"
    )
    lines.append(
        f"- 代码：`{m.get('git_commit')}` @ `{m.get('git_branch')}`{dirty}　"
        f"数据集指纹：`{str(m.get('golden_hash'))[:12]}`"
    )
    settings = m.get("settings") or {}
    eval_cfg = m.get("eval_config") or {}
    lines.append(
        "- 口径：top_k={tk} / rerank_top_k={rtk} / page_cap={pc} / chunks_per_url={cpu}"
        "　尺子：repeat={rp} / timeout={to}s / workers={wk}".format(
            tk=settings.get("vector_search_top_k", "?"),
            rtk=settings.get("rerank_top_k", "?"),
            pc=settings.get("per_competitor_page_cap", "?"),
            cpu=settings.get("chunks_per_url", "?"),
            rp=eval_cfg.get("repeat", "?"),
            to=eval_cfg.get("ragas_eval_timeout", "?"),
            wk=eval_cfg.get("ragas_eval_max_workers", "?"),
        )
    )
    failed = [c for c in m.get("cases", []) if c.get("error")]
    lines.append(
        f"- 完成：{m.get('cases_completed')}/{m.get('cases_requested')}　"
        f"失败：{('、'.join(c['golden_id'] for c in failed) if failed else '无')}"
    )
    lines.append("")
    lines.append("## case 级总表")
    lines.append("")
    lines.append(
        "| case | route | n_ctx(avg) | cp | cr | faith | ar | 状态 |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    for key, case in cases.items():
        agg = case.get("aggregates") or {}
        n_ctx = (case.get("n_contexts") or {}).get("avg")
        status = "失败" if case.get("error") else "ok"
        lines.append(
            "| {k} | {r} | {n} | {cp} | {cr} | {fa} | {ar} | {s} |".format(
                k=key,
                r=case.get("route") or "—",
                n=_fmt(n_ctx),
                cp=_fmt(agg.get("context_precision")),
                cr=_fmt(agg.get("context_recall")),
                fa=_fmt(agg.get("faithfulness")),
                ar=_fmt(agg.get("answer_relevancy")),
                s=status,
            )
        )
    lines.append(
        "| **全量均值** |  |  | {cp} | {cr} | {fa} | {ar} |  |".format(
            cp=_fmt(_overall(cases, "context_precision")),
            cr=_fmt(_overall(cases, "context_recall")),
            fa=_fmt(_overall(cases, "faithfulness")),
            ar=_fmt(_overall(cases, "answer_relevancy")),
        )
    )
    lines.append("")
    lines.append("> 跨 case 汇总只供看方向，禁止引用绝对值（材料量差几倍，横比无意义）。")
    lines.append("")

    for key, case in cases.items():
        if case.get("error") and not case.get("rows"):
            lines.append(f"## {key} — 失败")
            lines.append("")
            lines.append(f"`{case['error']}`")
            lines.append("")
            continue
        lines.append(f"## {key}（route={case.get('route') or '—'}）")
        lines.append("")
        lines.append("| 样本 | n_contexts | cp | cr | faith | ar |")
        lines.append("|---|---|---|---|---|---|")
        for row in case.get("rows") or []:
            lines.append(
                "| {k} | {n} | {cp} | {cr} | {fa} | {ar} |".format(
                    k=row.get("key"),
                    n=row.get("n_contexts"),
                    cp=_fmt(row.get("context_precision")),
                    cr=_fmt(row.get("context_recall")),
                    fa=_fmt(row.get("faithfulness")),
                    ar=_fmt(row.get("answer_relevancy")),
                )
            )
        spread = case.get("repeat_spread") or {}
        if case.get("repeat", 1) > 1 and spread:
            lines.append("")
            # 注意：这是 ragas_core 里「全部有效分数」的极差（跨样本 × 跨轮），
            # 只能粗看离散程度；判定某格变化要用 compare 输出的逐 cell θ。
            lines.append(
                "全量极差(max-min)："
                + " · ".join(f"{METRIC_SHORT[n]}={_fmt(spread.get(n))}" for n in METRICS)
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def finish_run(
    handle: RunHandle,
    entries: list[dict],
    *,
    settings=None,
    cases_requested: int | None = None,
    write_trend_file: bool = True,
    root: Path | None = None,
) -> Path:
    """收尾：写 run.json + summary.md，并把本 run 并入 TREND.md。

    ⚠️ **不修改入参 entries**（有守卫测试）：归档是「抄一份」，不是「接手改」。
    调用方在跑完后还要用 entries 打印分数表，若这里就地写字段会污染输出。

    entries 每项来自 ragas_core.run_golden：
      {golden_id, task_id, route, rows, aggregates, n_contexts, skipped,
       repeat, repeat_spread, scores_by_repeat, error}
    """
    root = archive_root(root)
    finished = datetime.now()
    duration = int((finished - handle.started_at).total_seconds())

    cases: dict[str, dict] = {}
    for entry in entries:
        key = _case_key(entry)
        # 深拷贝一份再序列化：避免把调用方持有的对象写进档（也保证不改入参）。
        cases[key] = json.loads(json.dumps(entry, ensure_ascii=False, default=str))

    kind = handle.kind
    repeat = max((int(c.get("repeat") or 1) for c in cases.values()), default=1)
    # resolved settings 只解析一次：dump 与 eval_config 必须来自同一份，
    # 否则会出现「settings 里 top_k=15，eval_config 里 timeout=None」这种半截口径。
    resolved = _load_settings(settings)

    manifest = {
        "run_id": handle.run_id,
        "archive_schema": ARCHIVE_SCHEMA,
        "kind": kind,
        "note": handle.note or _read_pending_note(handle.dir),
        "started_at": handle.started_at.isoformat(timespec="seconds"),
        "finished_at": finished.isoformat(timespec="seconds"),
        "duration_sec": duration,
        **git_info(),
        "golden_hash": golden_fingerprint(),
        "settings": _settings_dump(resolved),
        "eval_config": {
            "repeat": repeat,
            "ragas_eval_timeout": getattr(resolved, "ragas_eval_timeout", None),
            "ragas_eval_max_workers": getattr(resolved, "ragas_eval_max_workers", None),
        },
        "metrics": {
            "names": list(METRICS),
            "ragas_version": _ragas_version(),
            "python_version": platform.python_version(),
        },
        "cases": [
            {
                "golden_id": c.get("golden_id") or key,
                "task_id": c.get("task_id"),
                "route": c.get("route"),
                "error": c.get("error"),
            }
            for key, c in cases.items()
        ],
        "cases_completed": sum(1 for c in cases.values() if not c.get("error")),
        "cases_requested": (
            cases_requested if cases_requested is not None else len(cases)
        ),
        "operator": _operator(),
    }

    payload = {"manifest": manifest, "cases": cases}
    (handle.dir / RUN_JSON).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    (handle.dir / SUMMARY_MD).write_text(build_summary(manifest, cases), encoding="utf-8")
    # 占位文件只在「跑到一半崩溃」时有用，正常收尾必须删掉——
    # 不删的话目录里就多出第三个文件，破坏「恒定两个文件」。
    pending = handle.dir / ".pending"
    if pending.exists():
        pending.unlink()

    if write_trend_file:
        write_trend(root)
    return handle.dir


def _read_pending_note(run_dir: Path) -> str | None:
    """从 .pending 读回 note（begin_run 时写的，收尾后才删）。"""
    pending = run_dir / ".pending"
    if not pending.exists():
        return None
    try:
        return json.loads(pending.read_text(encoding="utf-8")).get("note")
    except Exception:  # pragma: no cover
        return None


def _ragas_version() -> str | None:
    """ragas 版本（指标实现升级必须可识别，例如 0.4.3 → 0.5 分口径会变）。"""
    try:
        import ragas  # noqa: PLC0415

        return getattr(ragas, "__version__", None)
    except Exception:
        return None


def _operator() -> str:
    """谁跑的（多机协作时排障要用）。"""
    import os

    return os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"


# ── 读档 / baseline ───────────────────────────────────────────────────────────


def load_run(run_dir: Path) -> dict:
    """读一个 run 目录的 run.json（缺文件就抛——静默跳过等于假装没有历史）。"""
    path = Path(run_dir) / RUN_JSON
    if not path.exists():
        raise FileNotFoundError(f"没有 run.json：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_run_id(token: str, root: Path | None = None) -> str:
    """把 CLI 参数解析成 run_id：支持 `baseline` 关键字，其余当 run_id。"""
    if token == "baseline":
        current = read_baseline(root)
        if not current:
            raise SystemExit("baseline.txt 为空：先 --set-baseline <run_id>")
        return current
    return token


def read_baseline(root: Path | None = None) -> str | None:
    """读 baseline.txt（一行文本；不存在 → None）。"""
    path = archive_root(root) / BASELINE_FILENAME
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text or None


def set_baseline(run_id: str, root: Path | None = None) -> str:
    """覆盖写 baseline.txt（唯一允许的「覆盖写」：它是指针不是数据）。"""
    root = archive_root(root)
    if not (root / run_id).exists():
        raise SystemExit(f"run 不存在：{root / run_id}")
    (root / BASELINE_FILENAME).write_text(run_id + "\n", encoding="utf-8")
    return run_id


def list_runs(root: Path | None = None, kind: str | None = "golden") -> list[dict]:
    """按 started_at 升序列出 run（默认只要 golden；单评是弱 baseline，不进趋势）。"""
    root = archive_root(root)
    runs = []
    for path in sorted(root.iterdir()):
        if not path.is_dir():
            continue
        try:
            payload = load_run(path)
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        manifest = payload.get("manifest") or {}
        if kind and manifest.get("kind") != kind:
            continue
        runs.append(payload)
    runs.sort(key=lambda p: (p["manifest"].get("started_at") or "", p["manifest"]["run_id"]))
    return runs


# ── 趋势表 TREND.md（派生视图，可整体重建） ────────────────────────────────────────────────


def _trend_rows(runs: list[dict]) -> list[dict]:
    """把 run 列表压成趋势行（每行一个版本，Δ 相对上一行）。"""
    rows: list[dict] = []
    prev: dict | None = None
    for index, payload in enumerate(runs, start=1):
        m = payload["manifest"]
        cases = payload.get("cases") or {}
        row = {
            "index": index,
            "run_id": m["run_id"],
            "note": m.get("note") or "—",
            "date": (m.get("started_at") or "")[5:10],
            "commit": m.get("git_commit"),
            "golden": str(m.get("golden_hash"))[:8],
            "repeat": (m.get("eval_config") or {}).get("repeat"),
            "top_k": (m.get("settings") or {}).get("vector_search_top_k"),
            "values": {n: _overall(cases, n) for n in METRICS},
            "delta": {},
        }
        for name in METRICS:
            current = row["values"][name]
            before = (prev["values"][name] if prev else None)
            row["delta"][name] = (
                round(current - before, 3)
                if current is not None and before is not None
                else None
            )
        rows.append(row)
        prev = row
    return rows


def build_trend(runs: list[dict]) -> str:
    """渲染 TREND.md（全版本趋势表）。

    为什么「重建」而不是「追加一行」：重建是幂等的——
    手改坏了、删了某个 run 目录导致对不上，跑一次 `trend --write` 就自动对齐。
    追加需要判断「这行在不在」，反而更容易漂移。
    """
    rows = _trend_rows(runs)
    lines = [
        "# 全量评估趋势（四指标）",
        "",
        "> 派生视图，事实源是各 run 的 `run.json`。对不上就跑：",
        "> `python -m src.evaluation.run_archive trend --write`",
        ">",
        "> 读法：口径列（commit / golden / repeat / top_k）必须一起看——"
        "把「换了参数的一行」读成「系统变好了」是这张表唯一的谎言方式。",
        "> Δ 只给方向，不给等级；跨 case 汇总禁止引用绝对值。faithfulness 是护栏，不判好坏。",
        "",
        "| # | run_id | 备注 | 日期 | commit | golden | repeat | top_k "
        "| cp | cr | faith | ar | Δcp | Δcr | Δfaith | Δar |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        cells = [
            str(row["index"]),
            f"`{row['run_id']}`",
            row["note"],
            row["date"],
            row["commit"] or "—",
            row["golden"],
            _fmt(row["repeat"], 0),
            _fmt(row["top_k"], 0),
        ]
        cells += [_fmt(row["values"][n]) for n in METRICS]
        cells += [_delta(row["delta"][n]) for n in METRICS]
        lines.append("| " + " | ".join(cells) + " |")
    if not rows:
        lines.append("| — | （还没有 golden run） |  |  |  |  |  |  |  |  |  |  |  |  |  |  |")
    lines.append("")
    return "\n".join(lines)


def write_trend(root: Path | None = None) -> Path:
    """从全部 run.json 重建 TREND.md 并落盘。"""
    root = archive_root(root)
    path = root / TREND_FILENAME
    path.write_text(build_trend(list_runs(root, kind="golden")), encoding="utf-8")
    return path


# ── 两两对比（逐 cell，守纪律） ────────────────────────────────────────────────────────


def _cell_spread(case: dict, cell: str, metric: str) -> float | None:
    """该 run 里某 cell 某指标的跨轮极差（= 判定阈值）。

    为什么阈值必须来自**这一格自己**：不同 cell 的 n_contexts 差几倍，
    噪声尺度天然不同（cr 二值抖动可达 1.000，而 ar 稳定在 0.05 内）。
    拿全局阈值判，等于用一把尺子量两种东西。
    """
    values = ((case.get("scores_by_repeat") or {}).get(cell) or {}).get(metric) or []
    nums = [float(v) for v in values if isinstance(v, (int, float))]
    if len(nums) < 2:
        return None
    return round(max(nums) - min(nums), 3)


def _rows_by_cell(cases: dict) -> dict[tuple[str, str], dict]:
    """(case_key, cell) → 分数行。对比按二元组对齐，不同 case 的 cell 不混。"""
    out: dict[tuple[str, str], dict] = {}
    for case_key, case in cases.items():
        for row in case.get("rows") or []:
            out[(case_key, row.get("key"))] = row
    return out


def _verdict(delta: float | None, threshold: float | None, metric: str, comparable: bool) -> str:
    """单格判定。规则只有一条 + 两条例外，顺序不能乱。

    1. 不可比（一侧缺格）→ 「不可比」，禁止按 0 补
    2. 护栏指标 → 「护栏」，只呈现
    3. 阈值未知（repeat=1，没有跨轮极差）→ 「噪声不可测」，只呈现 Δ
    4. |Δ| > 阈值 → 疑似变化；否则 → 噪声内
    """
    if not comparable:
        return "不可比"
    if metric in GUARDRAIL:
        return "护栏"
    if delta is None:
        return "—"
    if threshold is None:
        return "噪声不可测"
    if abs(delta) > threshold:
        return "⚠️ 疑似上升" if delta > 0 else "⚠️ 疑似下降"
    return "噪声内"


def build_compare(a: dict, b: dict) -> str:
    """渲染两个 run 的对比报告（返回值直接打印，不落盘）。

    四段：manifest 对照（能不能比）→ 逐 cell 表（核心）→ 汇总（带免责）→ 结论骨架。
    """
    ma, mb = a["manifest"], b["manifest"]
    ca, cb = a.get("cases") or {}, b.get("cases") or {}
    ra, rb = _rows_by_cell(ca), _rows_by_cell(cb)

    lines: list[str] = []
    lines.append(f"# 对比：`{ma['run_id']}` → `{mb['run_id']}`")
    lines.append("")
    warnings: list[str] = []
    if ma.get("golden_hash") != mb.get("golden_hash"):
        warnings.append(
            "**数据集已变**（golden_hash 不同）：此对比仅作参考，不能当回归证据"
        )
    if ma.get("git_dirty") or mb.get("git_dirty"):
        warnings.append("有 run 在 dirty 工作区跑的（git_dirty=true），复现打折")
    repeat_a = (ma.get("eval_config") or {}).get("repeat")
    repeat_b = (mb.get("eval_config") or {}).get("repeat")
    if repeat_a != repeat_b:
        warnings.append(f"repeat 不同（{repeat_a} vs {repeat_b}）：方差尺度不可比")
    if ma.get("cases_completed") != ma.get("cases_requested") or mb.get(
        "cases_completed"
    ) != mb.get("cases_requested"):
        warnings.append("存在未完成的 case：以下只比两边都有分的格子")
    for w in warnings:
        lines.append(f"> ⚠️ {w}")
    if warnings:
        lines.append("")

    lines.append("## ① manifest 对照")
    lines.append("")
    lines.append("| 项 | A | B |")
    lines.append("|---|---|---|")
    for label, key in (
        ("run_id", "run_id"),
        ("备注", "note"),
        ("commit", "git_commit"),
        ("golden_hash", "golden_hash"),
        ("case 完成", "cases_completed"),
    ):
        lines.append(f"| {label} | {ma.get(key)} | {mb.get(key)} |")
    for label, key in (
        ("repeat", "repeat"),
        ("timeout", "ragas_eval_timeout"),
        ("workers", "ragas_eval_max_workers"),
    ):
        lines.append(
            f"| {label} | {(ma.get('eval_config') or {}).get(key)} "
            f"| {(mb.get('eval_config') or {}).get(key)} |"
        )
    for label, key in (("top_k", "vector_search_top_k"), ("page_cap", "per_competitor_page_cap")):
        lines.append(
            f"| {label} | {(ma.get('settings') or {}).get(key)} "
            f"| {(mb.get('settings') or {}).get(key)} |"
        )
    lines.append("")

    lines.append("## ② 逐 cell 对比（只同 cell 比）")
    lines.append("")
    lines.append(
        "| case | 样本 | n_ctx A→B | cp A→B (Δ, θ) | cr A→B (Δ, θ) "
        "| faith A→B (Δ, θ) | ar A→B (Δ, θ) | 判定 |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    tallies = {"up": [], "down": [], "noise": [], "guard": [], "n/a": []}
    zero_threshold = False
    for key in sorted(set(ra) | set(rb)):
        case_key, cell = key
        row_a, row_b = ra.get(key), rb.get(key)
        comparable = row_a is not None and row_b is not None
        n_a = (row_a or {}).get("n_contexts")
        n_b = (row_b or {}).get("n_contexts")
        n_change = (
            (n_b - n_a) / n_a if isinstance(n_a, int) and isinstance(n_b, int) and n_a else None
        )
        cells = [f"{case_key}", f"{cell}", f"{_fmt(n_a, 0)}→{_fmt(n_b, 0)}"]
        verdicts: list[str] = []
        for metric in METRICS:
            va = (row_a or {}).get(metric)
            vb = (row_b or {}).get(metric)
            delta = (
                round(vb - va, 3)
                if isinstance(va, (int, float)) and isinstance(vb, (int, float))
                else None
            )
            spread_a = _cell_spread(ca.get(case_key) or {}, cell, metric)
            spread_b = _cell_spread(cb.get(case_key) or {}, cell, metric)
            known = [s for s in (spread_a, spread_b) if s is not None]
            threshold = max(known) if known else None
            if threshold == 0:
                zero_threshold = True
            verdict = _verdict(delta, threshold, metric, comparable)
            if verdict.startswith("⚠️"):
                verdicts.append((metric, verdict))
            cells.append(
                "{a}→{b} ({d}, θ{t}){flag}".format(
                    a=_fmt(va),
                    b=_fmt(vb),
                    d=_delta(delta),
                    t=_fmt(threshold) if threshold is not None else "—",
                    flag=" ⚠️" if verdict.startswith("⚠️") else "",
                )
            )
            if metric in GUARDRAIL:
                tallies["guard"].append(f"{cell}·{METRIC_SHORT[metric]}")
            elif verdict.startswith("⚠️"):
                tallies["up" if "上升" in verdict else "down"].append(
                    f"{cell}·{METRIC_SHORT[metric]}"
                )
            elif verdict == "噪声内":
                tallies["noise"].append(f"{cell}·{METRIC_SHORT[metric]}")
            elif verdict == "不可比":
                tallies["n/a"].append(f"{cell}·{METRIC_SHORT[metric]}")
        if verdicts:
            verdict_text = "；".join(f"{METRIC_SHORT[m]} {v}" for m, v in verdicts)
        elif any(_cell_spread(ca.get(case_key) or {}, cell, m) is not None for m in METRICS):
            verdict_text = "噪声内"
        else:
            verdict_text = "噪声不可测"
        # 材料量变化 >20% 时 Δ 里混着采集因素，必须显式免责（见模块头「对比纪律」）。
        if n_change is not None and abs(n_change) > 0.2:
            verdict_text += "（材料量已变，Δ 含采集因素）"
        lines.append("| " + " | ".join(cells + [verdict_text]) + " |")
    lines.append("")
    lines.append(
        "> θ = 该 cell 该指标的跨轮极差（A/B 取大者），即「噪声阈值」；"
        "|Δ| > θ 才判疑似变化。"
    )
    if zero_threshold:
        lines.append(
            "> θ=0 表示该格三轮分数完全一致（无抖动可测）：此时任何 Δ 都会「超出阈值」，"
            "请结合 repeat 与 n_contexts 谨慎读。"
        )
    lines.append("")

    lines.append("## ③ 汇总（Δ，仅供看方向）")
    lines.append("")
    lines.append("> 跨 cell 汇总仅供参考，禁止引用绝对值（各 cell 材料量差几倍）。")
    lines.append("")
    lines.append("| 指标 | A 全量均值 | B 全量均值 | Δ |")
    lines.append("|---|---|---|---|")
    for metric in METRICS:
        va = _overall(ca, metric)
        vb = _overall(cb, metric)
        delta = round(vb - va, 3) if va is not None and vb is not None else None
        suffix = "（护栏，不判）" if metric in GUARDRAIL else ""
        lines.append(
            f"| {METRIC_SHORT[metric]} | {_fmt(va)} | {_fmt(vb)} | {_delta(delta)}{suffix} |"
        )
    lines.append("")

    lines.append("## ④ 结论骨架（人只填归因）")
    lines.append("")
    lines.append(f"- 变好候选：{'、'.join(tallies['up']) or '无'}")
    lines.append(f"- 变坏候选：{'、'.join(tallies['down']) or '无'}")
    lines.append(f"- 噪声内：{len(tallies['noise'])} 格")
    lines.append(f"- 护栏（faithfulness）：{'、'.join(tallies['guard']) or '无'}")
    lines.append(f"- 不可比：{'、'.join(tallies['n/a']) or '无'}")
    lines.append("")
    lines.append("归因（人工填）： ")
    return "\n".join(lines)


# ── CLI ─────────────────────────────────────────────────────────────────────


def _find_run_dir(run_id: str, root: Path | None = None) -> Path:
    """按 run_id 找目录；不存在就报错（不猜、不做模糊匹配）。"""
    root = archive_root(root)
    path = root / run_id
    if not path.exists():
        raise SystemExit(f"run 不存在：{path}")
    return path


def main(argv: list[str] | None = None) -> None:
    """子命令入口。

    用法：
      python -m src.evaluation.run_archive compare <A> <B>     # A/B 可为 run_id 或 "baseline"
      python -m src.evaluation.run_archive trend               # 打印趋势表
      python -m src.evaluation.run_archive trend --write       # 重建 TREND.md
      python -m src.evaluation.run_archive trend --since 2026-10-01   # 只看该日期之后
      python -m src.evaluation.run_archive baseline            # 打印当前基线
      python -m src.evaluation.run_archive baseline --set <run_id>
    """
    parser = argparse.ArgumentParser(
        description="全量评估留痕：对比两个 run、查看趋势、管理基线（纯读 JSON，零 LLM 成本）"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_cmp = sub.add_parser("compare", help="逐 cell 对比两个 run")
    p_cmp.add_argument("a", help="基准 run_id 或 baseline")
    p_cmp.add_argument("b", help="对照 run_id 或 baseline")

    p_trend = sub.add_parser("trend", help="趋势表（打印或重建 TREND.md）")
    p_trend.add_argument("--write", action="store_true", help="重建并覆盖 TREND.md")
    p_trend.add_argument("--since", help="只显示该日期（YYYY-MM-DD）之后的 run")

    p_base = sub.add_parser("baseline", help="查看/设置当前基线")
    p_base.add_argument("--set", dest="set_to", help="把基线指向该 run_id")

    args = parser.parse_args(argv)

    if args.cmd == "compare":
        a_id = resolve_run_id(args.a)
        b_id = resolve_run_id(args.b)
        a = load_run(_find_run_dir(a_id))
        b = load_run(_find_run_dir(b_id))
        print(build_compare(a, b))
        return

    if args.cmd == "trend":
        if args.write:
            path = write_trend()
            print(f"已重建：{path}")
            return
        runs = list_runs()
        if args.since:
            runs = [
                r
                for r in runs
                if (r["manifest"].get("started_at") or "")[:10] >= args.since
            ]
        print(build_trend(runs))
        return

    if args.cmd == "baseline":
        if args.set_to:
            print(f"基线已指向：{set_baseline(args.set_to)}")
        else:
            print(read_baseline() or "（未设置）")
        return


if __name__ == "__main__":
    main()
