"""Token 用量提取 — 从 langchain AIMessage 提取 DeepSeek usage_metadata。

═══════════════════════════════════════════════════════════════════════════
                    【L5 面试 — Token 成本可观测性】
═══════════════════════════════════════════════════════════════════════════

为什么单独抽一个 extract_usage() 而不是在每个 Agent 里手写 resp.usage_metadata？

  1. 统一容错——usage_metadata 可能是 None（provider 不回传 / 流式未聚合），
     手写容易漏判导致 AttributeError。
  2. 字段名映射——DeepSeek 返回 input_tokens/output_tokens/total_tokens，
     而 OpenAI 风格是 prompt_tokens/completion_tokens。映射逻辑集中在一处，
     未来换模型只改这里。
  3. 归一化——统一返回 int，缺失回 0，调用方可以无脑累加（累加 0 不影响结果）。

【L5 决策】为什么 total_tokens 要 fallback 到 input+output？
─────────────────────────────────────────────────────────
某些 provider 不返回 total_tokens（只给 input/output）。如果只信 total，
可能拿到 0 导致成本面板低估。fallback 保证「即使缺 total，也能算出总量」。
"""

from __future__ import annotations


def extract_usage(resp) -> dict[str, int]:
    """从 langchain AIMessage 提取 token 用量，缺失时返回 0。

    Args:
        resp: langchain `llm.ainvoke(prompt)` 的返回值（AIMessage），
              带 `.usage_metadata` 属性。

    Returns:
        {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int}
        三个值都是 ≥0 的整数，缺失时回 0，保证调用方可安全累加。
    """
    # 【L4 工程】usage_metadata 可能是 None（provider 不回传），
    # getattr + or {} 双重兜底，绝不 AttributeError。
    usage = getattr(resp, "usage_metadata", None) or {}

    prompt_tokens = int(usage.get("input_tokens") or 0)
    completion_tokens = int(usage.get("output_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or 0)

    # 【L5 决策】total 缺失时 fallback 到 input+output，避免成本面板低估。
    if total_tokens == 0 and (prompt_tokens or completion_tokens):
        total_tokens = prompt_tokens + completion_tokens

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def add_usage(acc: dict[str, int], usage: dict[str, int]) -> dict[str, int]:
    """累加 token 用量到累加器（用于「多次 LLM 调用合并成一条日志」的场景）。

    如 Collector 的 _generate_keywords 对每个竞品调一次 LLM，
    但日志只写一条 generate_keywords，需要把 N 次调用的 token 累加。
    """
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        acc[key] = acc.get(key, 0) + usage.get(key, 0)
    return acc
