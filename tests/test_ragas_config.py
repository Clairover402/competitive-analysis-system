# -*- coding: utf-8 -*-
"""ragas 评估配置回归测试（源码钉桩，不需要 DB / LLM）。

═══════════════════════════════════════════════════════════════════════════════
                         【两个必须守住的技术前提】
═══════════════════════════════════════════════════════════════════════════════

  1. `answer_relevancy.strictness` 必须为 1
     ragas 会把它作为 `n` 透传给 LLM（一次生成 n 个候选问题反推）。
     DeepSeek API 不支持 n>1，直接 400 Bad Request → ragas 内部重试耗尽 → 该指标
     **恒为 NaN**，表现为 golden 跑出来 answer_relevancy 整列为空。
     实测：strictness=3 → 8 次重试、48.7s 后失败；=1 → 11.7s 返回有效分。

  2. `aevaluate` 必须显式传 run_config
     ragas 默认 `RunConfig(timeout=180)`，而 context_precision 对 retrieved_contexts
     是**串行**逐条判 LLM。12 条 contexts 的长样本要在 180s 内跑完 12 次 judge
     调用 → 必然超时 → 作业被 asyncio.wait_for 掐断 → 分数静默变 NaN。
     2026-10-16 `钉钉@功能 cp=NaN` + 日志 `Job[12]: TimeoutError()` 即此机制。
     → `build_run_config(settings)` 把 timeout 抬到配置值（默认 600）。

  两处都是「一旦回退就静默丢分」的类型，故用源码扫描钉死，不依赖运行时。
═══════════════════════════════════════════════════════════════════════════════
"""
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAGAS_CORE = os.path.join(ROOT, "src", "evaluation", "ragas_core.py")
LLM_FACTORY = os.path.join(ROOT, "src", "evaluation", "llm_factory.py")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


class TestRagasConfig(unittest.TestCase):
    def test_回归_answer_relevancy的strictness必须为1(self):
        """DeepSeek 不支持 n>1；strictness 一旦回到 >1，该指标会全 NaN。"""
        src = _read(RAGAS_CORE)
        m = re.search(r"answer_relevancy\.strictness\s*=\s*(\d+)", src)
        assert m, (
            "ragas_core.py 必须显式设置 `answer_relevancy.strictness`："
            "ragas 会把它作为 n 透传给 LLM，而 DeepSeek 不支持 n>1（返回 400）。"
        )
        assert m.group(1) == "1", (
            f"answer_relevancy.strictness 必须为 1（DeepSeek 不支持 n>1），"
            f"实际为 {m.group(1)}"
        )

    def test_回归_strictness修复须带原因注释(self):
        """修复点必须有注释说明「DeepSeek 不支持 n>1」，避免后人误删。"""
        src = _read(RAGAS_CORE)
        idx = src.find("answer_relevancy.strictness = 1")
        assert idx != -1, "未找到 strictness 修复行"
        ctx = src[max(0, idx - 700) : idx]
        assert "400" in ctx and "n>1" in ctx, (
            "strictness 修复行上方必须注释说明 DeepSeek 不支持 n>1（400 Bad Request）"
        )

    def test_回归_aevaluate必须显式传run_config(self):
        """不传 run_config = 用 ragas 默认 180s 单作业超时 → 长样本静默 NaN。

        只要求「不传」不能出现，且 build_run_config 在代码里被真正调用：
        单看 import 不够（import 了不传照样退化）。
        """
        src = _read(RAGAS_CORE)
        # 只认真实调用点（`await aevaluate(`）；注释里的 `aevaluate()` 不算，
        # 否则会误伤 docstring 与说明性注释。
        idxs = [m.start() for m in re.finditer(r"await aevaluate\(", src)]
        assert idxs, "ragas_core.py 里找不到 aevaluate 调用点"
        for i in idxs:
            window = src[i : i + 400]
            assert "run_config" in window, (
                "aevaluate 必须显式传 run_config（ragas 默认 180s 会让长样本超时→NaN）："
                f"\n{window[:200]}"
            )
        assert "build_run_config(" in src, "run_config 必须由 build_run_config 构造"

    def test_回归_超时与并发走配置而非硬编码(self):
        """超时/并发必须可配（build_run_config 读 settings），不许写死常量。"""
        src = _read(LLM_FACTORY)
        assert "ragas_eval_timeout" in src and "ragas_eval_max_workers" in src, (
            "build_run_config 必须从 settings 读 ragas_eval_timeout / "
            "ragas_eval_max_workers，硬编码会让不同环境无法调参"
        )
        assert "RunConfig(timeout=timeout" in src, "RunConfig 必须由配置值构造"


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
