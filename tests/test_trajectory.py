# -*- coding: utf-8 -*-
"""轨迹评估守卫测试。

守的是三条「轨迹一旦读错就全盘错」的性质：

  1. 分类只认 action：think / observe / tool_call / 其余，不能靠猜；
  2. 缺数据必须回 n/a 而不是 0 —— 老任务没有 tool_call 日志是常态，
     把「没记上」当「没发生」会把健康任务判成有病；
  3. 根因能落到具体那一步（第几轮重复、哪类抓取失败），只给分数等于没诊断。

全部构造 dict 喂给纯函数，不连库、不调 LLM。
"""

from __future__ import annotations

from src.evaluation import trajectory as tj

# ── 构造样本 ────────────────────────────────────────────────────────────


def _row(action: str, *, agent: str = "supervisor", request=None, response=None,
         error=None, duration_ms=None, seq: int = 0) -> dict:
    return {
        "task_id": "t-1",
        "created_at": f"2026-10-19T10:00:{seq:02d}",
        "agent_name": agent,
        "action": action,
        "request": request or {},
        "response": response or {},
        "error": error,
        "duration_ms": duration_ms,
    }


def _think(round_: int, agent: str, args: dict | None = None, seq: int | None = None) -> dict:
    """一条 think 日志：日志 action='think'，决策结果在 response 里。"""
    return _row(
        "think",
        request={"round": round_},
        response={
            "round": round_,
            "action": agent,
            "agent": agent,
            "thought": f"round {round_}",
            "reason": "test",
            "arguments": args or {},
        },
        seq=seq if seq is not None else round_ * 3,
    )


def _observe(round_: int, agent: str, status: str = "completed", obs: str = "ok",
             seq: int | None = None) -> dict:
    return _row(
        "observe",
        request={"round": round_, "agent": agent, "status": status},
        response={"observation": obs},
        seq=seq if seq is not None else round_ * 3 + 1,
    )


def _tool(name: str, args: dict | None = None, *, ok: bool = True, n=None,
          chars=None, error=None, scores=None, seq: int = 90) -> dict:
    response: dict = {"ok": ok}
    if n is not None:
        response["n"] = n
    if chars is not None:
        response["chars"] = chars
    if scores:
        ordered = sorted(scores, reverse=True)
        response["score_top1"] = ordered[0]
        response["score_median"] = ordered[len(ordered) // 2]
        response["score_min"] = ordered[-1]
    return _row(
        "tool_call",
        agent="collector",
        request={"tool": name, "args": args or {}},
        response=response,
        error=error,
        duration_ms=12.0,
        seq=seq,
    )


# ── 分类与重建 ──────────────────────────────────────────────────────────


def test_按action分类_四类事件各归各位():
    rows = [
        _think(1, "collector", {"competitors": "list(len=2)"}),
        _observe(1, "collector", obs="collected 2 competitors"),
        _tool("web_search", {"query": "飞书 定价"}, n=8),
        _row("dim_analysis", agent="analyzer", request={"dimension": "定价"}),
    ]
    trace = tj.build_trace("t-1", rows)

    assert len(trace.thinks) == 1
    assert len(trace.observes) == 1
    assert len(trace.tools) == 1
    assert len(trace.business) == 1
    assert trace.is_supervisor is True
    assert trace.raw_count == 4


def test_没有think日志时判为非supervisor():
    trace = tj.build_trace("t-2", [_row("collect_pages", agent="collector")])
    assert trace.is_supervisor is False


def test_jsonb被回成字符串也能解析():
    row = _row("think", request={"round": 1})
    row["response"] = '{"round": 1, "action": "finish", "agent": "finish", "arguments": {}}'
    trace = tj.build_trace("t-3", [row])
    assert trace.thinks[0]["agent"] == "finish"


# ── L1 指标 ─────────────────────────────────────────────────────────────


def test_正常完成_终止原因是finish且轮次效率满():
    rows = [
        _think(1, "collector"), _observe(1, "collector"),
        _think(2, "analyzer"), _observe(2, "analyzer"),
        _think(3, "finish"), _observe(3, "finish", obs=""),
    ]
    l1 = tj.l1_metrics(tj.build_trace("t", rows))

    assert l1["终止原因"]["value"] == "finish"
    # finish 轮不产生新业务状态 → 有效轮次 = 2（collector/analyzer）/ 总轮次 3
    assert l1["轮次效率"]["value"] == round(2 / 3, 3)
    assert l1["非法转移"]["value"] == 0


def test_轮次耗尽识别为max_rounds():
    rows = []
    for i in range(1, tj.DEFAULT_MAX_ROUNDS + 1):
        rows += [_think(i, "collector"), _observe(i, "collector")]
    l1 = tj.l1_metrics(tj.build_trace("t", rows))
    assert l1["终止原因"]["value"] == "max_rounds"


def test_analyzer在collector之前记为非法转移():
    rows = [_think(1, "analyzer"), _observe(1, "analyzer"), _think(2, "finish")]
    l1 = tj.l1_metrics(tj.build_trace("t", rows))
    assert l1["非法转移"]["value"] == 1
    assert "analyzer" in l1["非法转移"]["note"]


def test_连续同agent同参数算重复动作():
    same = {"competitors": "list(len=2)"}
    rows = [
        _think(1, "collector", same), _observe(1, "collector"),
        _think(2, "collector", same), _observe(2, "collector"),
        _think(3, "finish"),
    ]
    l1 = tj.l1_metrics(tj.build_trace("t", rows))
    assert l1["重复动作率"]["value"] == 0.5          # 2 次相邻比较里 1 次重复


def test_失败后原样重试计入观察响应性():
    same = {"competitors": "list(len=2)"}
    rows = [
        _think(1, "collector", same), _observe(1, "collector", status="failed", obs="collector failed"),
        _think(2, "collector", same), _observe(2, "collector"),
        _think(3, "finish"),
    ]
    l1 = tj.l1_metrics(tj.build_trace("t", rows))
    assert l1["观察响应性"]["value"] == 1.0


def test_必填参数缺键会计入不完整():
    rows = [
        _think(1, "collector", {}),          # 缺 competitors/dimensions
        _observe(1, "collector"),
        _think(2, "finish"),
    ]
    l1 = tj.l1_metrics(tj.build_trace("t", rows))
    assert l1["参数完整率"]["value"] == 0.0


# ── L2 指标 ─────────────────────────────────────────────────────────────


def test_搜索空结果率():
    rows = [
        _tool("web_search", {"query": "a"}, n=0, seq=1),
        _tool("web_search", {"query": "b"}, n=5, seq=2),
        _tool("web_search", {"query": "c"}, n=3, seq=3),
        _tool("web_search", {"query": "d"}, n=0, seq=4),
    ]
    l2 = tj.l2_metrics(tj.build_trace("t", rows))
    assert l2["搜索空结果率"]["value"] == 0.5


def test_抓取失败按错误类型分桶():
    rows = [
        _tool("web_fetch", {"url": "https://a"}, ok=False, error="TimeoutError: read timeout", seq=1),
        _tool("web_fetch", {"url": "https://b"}, ok=False, error="HTTPStatusError: 403 Forbidden", seq=2),
        _tool("web_fetch", {"url": "https://c"}, ok=True, chars=1200, seq=3),
    ]
    l2 = tj.l2_metrics(tj.build_trace("t", rows))
    note = l2["抓取失败率"]["note"]
    assert l2["抓取失败率"]["value"] == round(2 / 3, 3)
    assert "timeout" in note and "403" in note


def test_重复抓取率():
    rows = [
        _tool("web_fetch", {"url": "https://a"}, chars=100, seq=1),
        _tool("web_fetch", {"url": "https://a"}, chars=100, seq=2),
    ]
    l2 = tj.l2_metrics(tj.build_trace("t", rows))
    assert l2["重复抓取率"]["value"] == 0.5


def test_embedding调用次数与批大小():
    rows = [
        _tool("embed_query", {"query": "定价"}, n=None, seq=1),
        _tool("embed_texts", {"texts": "list(len=12)"}, n=12, seq=2),
        _tool("embed_texts", {"texts": "list(len=8)"}, n=8, seq=3),
    ]
    l2 = tj.l2_metrics(tj.build_trace("t", rows))
    assert l2["embedding 调用"]["value"] == 3
    assert "20 条" in l2["embedding 调用"]["note"]


def test_rerank区分度取top1减中位():
    rows = [
        _tool("rerank", {"query": "飞书 定价", "top_k": 15}, n=15,
              scores=[0.91, 0.90, 0.90, 0.89, 0.89], seq=1),
    ]
    l2 = tj.l2_metrics(tj.build_trace("t", rows))
    # 排序后 [0.91, 0.90, 0.90, 0.89, 0.89]，median 取 index 2 = 0.90
    assert l2["rerank 区分度"]["value"] == 0.01


def test_采集漏斗串起搜索抓取入库():
    rows = [
        _tool("web_search", {"query": "a"}, n=10, seq=1),
        _tool("web_fetch", {"url": "https://a"}, chars=3000, seq=2),
        _tool("web_fetch", {"url": "https://b"}, chars=0, seq=3),
        _tool("embed_texts", {"texts": "list(len=4)"}, n=4, seq=4),
    ]
    l2 = tj.l2_metrics(tj.build_trace("t", rows))
    assert l2["采集漏斗"]["value"] == "10 → 2 → 1 → 4"


def test_没有工具日志时全部n_a而不是0():
    trace = tj.build_trace("t", [_think(1, "finish")])
    l2 = tj.l2_metrics(trace)
    assert all(item["value"] == "n/a" for item in l2.values())


# ── 渲染 ────────────────────────────────────────────────────────────────


def test_单任务报告包含三层结构与结论():
    rows = [
        _think(1, "collector", {"competitors": "list(len=2)", "dimensions": "list(len=2)"}),
        _observe(1, "collector", obs="collected 2 competitors"),
        _think(2, "finish"),
    ]
    text = tj.render_case(tj.build_trace("t-9", rows))
    assert "L1 编排级" in text and "L2 工具级" in text
    assert "结论" in text
    assert "t-9" in text


def test_pipeline任务结论提示去看采集侧():
    trace = tj.build_trace("t", [_row("collect_pages", agent="collector")])
    text = tj.render_case(trace)
    assert "Pipeline" in text


def test_批量报告不崩且给出分布():
    traces = [
        tj.build_trace("a", [_think(1, "finish")]),
        tj.build_trace("b", [_think(1, "collector"), _observe(1, "collector")]),
    ]
    text = tj.render_batch(traces)
    assert "终止原因分布" in text
    assert "阈值未定" in text


def test_parse_since支持d_h_和ISO():
    assert tj._parse_since("7d").year >= 2020
    assert tj._parse_since("24h").year >= 2020
    assert tj._parse_since("2026-10-01").month == 10


# ── 落库宽表 ────────────────────────────────────────────────────────────


def _full_rows() -> list[dict]:
    return [
        _think(1, "collector", {"competitors": "list(len=2)", "dimensions": "list(len=2)"}),
        _observe(1, "collector", obs="collected 2 competitors"),
        _tool("web_search", {"query": "a"}, n=0, seq=2),
        _tool("web_search", {"query": "b"}, n=9, seq=3),
        _tool("web_fetch", {"url": "https://a"}, ok=False,
              error="TimeoutError: read timeout", seq=4),
        _tool("web_fetch", {"url": "https://b"}, chars=100, seq=5),
        _tool("embed_texts", {"texts": "list(len=5)"}, n=5, seq=6),
        _think(2, "finish"),
    ]


def test_to_row把指标压成宽表并把漏斗拆成四列():
    row = tj.to_row(tj.build_trace("t-1", _full_rows()))

    assert row["task_id"] == "t-1"
    assert row["is_supervisor"] is True
    assert row["termination"] == "finish"
    assert row["rounds"] == 2 and row["rounds_effective"] == 1
    assert row["search_empty_rate"] == 0.5
    assert row["fetch_fail_rate"] == 0.5
    assert row["fetch_fail_detail"] == {"timeout": 1}
    # 9 → 2 → 1 → 5，四个数必须各占一列，面板才能画漏斗
    assert (row["funnel_search"], row["funnel_fetch"],
            row["funnel_pages"], row["funnel_chunks"]) == (9, 2, 1, 5)
    assert row["embed_calls"] == 1 and row["embed_chunks"] == 5


def test_to_row的n_a落成None而不是0():
    # 只有 think 日志、没有任何工具调用：算不出来 ≠ 发生了 0 次
    trace = tj.build_trace("t-2", [_think(1, "finish")])
    row = tj.to_row(trace)

    assert row["tool_calls"] is None
    assert row["search_empty_rate"] is None
    assert row["fetch_fail_detail"] is None
    assert row["rerank_gap"] is None
    assert row["funnel_search"] is None
    # 编排级的 0 是真 0（没有非法转移），必须保留
    assert row["illegal_transitions"] == 0


def test_pipeline任务的termination记为n_a且无轮次():
    trace = tj.build_trace("t-3", [_row("collect_pages", agent="collector")])
    row = tj.to_row(trace)

    assert row["is_supervisor"] is False
    assert row["termination"] == "n/a"
    assert row["rounds"] is None and row["rounds_effective"] is None


def test_to_row与报告显示值一致():
    trace = tj.build_trace("t-4", _full_rows())
    l1, l2 = tj.l1_metrics(trace), tj.l2_metrics(trace)
    row = tj.to_row(trace)

    assert row["repeat_rate"] == l1["重复动作率"]["value"]
    assert row["arg_completeness"] == l1["参数完整率"]["value"]
    assert row["tool_calls"] == l2["工具调用次数"]["value"]
    assert row["dup_fetch_rate"] == l2["重复抓取率"]["value"]
    # 没有 rerank 调用：报告显示 n/a，落库是 NULL —— 这是有意的映射，不是不一致
    assert l2["rerank 区分度"]["value"] == "n/a" and row["rerank_gap"] is None


def test_row_params把失败分桶序列化成json():
    row = tj.to_row(tj.build_trace("t-5", _full_rows()))
    params = tj._row_params(row)

    assert params[0] == "t-5"
    index = tj._ROW_COLUMNS.index("fetch_fail_detail") + 1
    assert isinstance(params[index], str)
    assert tj.json.loads(params[index]) == {"timeout": 1}
    assert len(params) == len(tj._ROW_COLUMNS) + 1


def test_upsert语句幂等且覆盖全部列():
    sql = tj._UPSERT_SQL
    assert "ON CONFLICT (task_id) DO UPDATE" in sql
    assert "$28" in sql and len(tj._ROW_COLUMNS) == 28
    for column in tj._ROW_COLUMNS:
        assert f"{column} = EXCLUDED.{column}" in sql


def test_writer和quality不参与参数完整率判定():
    # 实测：Supervisor 给 writer 只传 title、给 quality 传 {}，
    # analysis_results / report_markdown 走 state 流转。
    # 把它们算作缺键会让健康任务恒为 0.5 —— 假警报。
    rows = [
        _think(1, "collector", {"competitors": "list(len=2)", "dimensions": "list(len=2)"}),
        _think(2, "writer", {"title": "x"}),
        _think(3, "quality", {}),
        _think(4, "finish"),
    ]
    l1 = tj.l1_metrics(tj.build_trace("t", rows))
    assert l1["参数完整率"]["value"] == 1.0
    assert l1["参数完整率"]["note"] == "1/1 轮键齐全"


def test_指标口径版本随定义变更提升():
    # v4：新增 L3 检索侧 5 列。改口径必须升版本，否则旧行被误读。
    assert tj.METRICS_VERSION == 4


# ── 阶段 3：强制结束与 JSON 降级的识别 ──────────────────────────────────


def test_强制结束优先于最后一条think的决策():
    # 实测场景：LLM 第 5 轮仍选 writer，act 硬拦截改成 finish。
    # 只看 think 的话最后一条还是 writer → 会被误判成 incomplete。
    rows = [
        _think(1, "collector", {"competitors": "list(len=2)", "dimensions": "list(len=2)"}),
        _think(2, "analyzer", {"competitors": "list(len=2)", "dimensions": "list(len=2)"}),
        _think(3, "writer", {"title": "x"}),
        _row("forced_finish",
             request={"round": 3, "agent": "writer", "rewrite_count": 2},
             response={"message": "重写已达上限，接受当前报告", "max_rewrites": 2}),
    ]
    l1 = tj.l1_metrics(tj.build_trace("t", rows))

    assert l1["终止原因"]["value"] == "forced_finish"
    assert "rewrite_count=2" in l1["终止原因"]["note"]


def test_json降级不被当成LLM主动完成():
    # 两次解析失败 → think 落一条 action=finish + degraded=json_error。
    # 若不加这个标记，这种故障退出会混进 finish，健康度看起来是满分。
    rows = [
        _think(1, "collector", {"competitors": "list(len=2)", "dimensions": "list(len=2)"}),
        _row("think",
             request={"round": 2},
             response={"round": 2, "action": "finish", "agent": "finish",
                       "thought": "JSON 解析失败，安全退出", "reason": "两次 JSON 解析失败",
                       "arguments": {}, "degraded": "json_error"}),
    ]
    l1 = tj.l1_metrics(tj.build_trace("t", rows))

    assert l1["终止原因"]["value"] == "json_error"
    assert "json_error" in l1["终止原因"]["note"]


def test_强制结束同时存在降级时以强制结束为准():
    # 优先级：act 的硬拦截是事实，think 的 deferred 只是决策层的降级
    rows = [
        _think(1, "writer", {"title": "x"}),
        _row("forced_finish", request={"round": 1, "rewrite_count": 2}, response={}),
        _row("think", request={"round": 2},
             response={"round": 2, "action": "finish", "agent": "finish",
                       "degraded": "json_error"}),
    ]
    assert tj.l1_metrics(tj.build_trace("t", rows))["终止原因"]["value"] == "forced_finish"


def test_终止原因四类都能进落库行():
    for reason, rows in {
        "finish": [_think(1, "finish")],
        "max_rounds": [_think(10, "collector")],
        "incomplete": [_think(1, "collector")],
        "json_error": [_row("think", request={"round": 1},
                            response={"round": 1, "action": "finish",
                                      "agent": "finish", "degraded": "json_error"})],
        "forced_finish": [_think(1, "writer"),
                          _row("forced_finish", request={"round": 1}, response={})],
    }.items():
        assert tj.to_row(tj.build_trace("t", rows))["termination"] == reason


# ── 阶段 4：L3 检索侧（数据源 retrieval_snapshot）────────────────────────


def _snap(dim, comp, recalled, reranked):
    return {"dimension": dim, "competitor": comp,
            "recalled_urls": recalled, "reranked_urls": reranked}


def test_检索侧三项指标按快照计算():
    snaps = [
        _snap("功能", "A", ["u1", "u2", "u3", "u4"], ["u1", "u2"]),
        _snap("功能", "B", ["u5", "u6"], ["u5"]),
        _snap("定价", "A", ["u1", "u2"], ["u1", "u3"]),
        _snap("定价", "B", ["u5"], ["u5"]),
    ]
    data = tj.compute_retrieval(snaps, ["A", "B"])

    assert data["snapshots"] == 4
    assert data["recall_avg"] == 2.2          # (4+2+2+1)/4
    assert data["rerank_avg"] == 1.5          # (2+1+2+1)/4
    # 竞品去重页数 A=4、B=2 → 最小/中位 = 2/3
    assert data["coverage_skew"] == 0.667
    # 功能 {u1,u2,u5}=3、定价 {u1,u3,u5}=3，去重后 4 → 1 - 4/6
    assert data["cross_dim_dup"] == 0.333


def test_没落快照的竞品按0页计入偏斜():
    # analyzer 对「找不到料」的竞品是 skip、不落快照。
    # 只按快照里的竞品算，最该报警的那个空缺会隐身，skew 还会显示很健康。
    snaps = [
        _snap("功能", "A", ["u1", "u2", "u3", "u4"], ["u1"]),
        _snap("功能", "B", ["u5", "u6"], ["u5"]),
    ]
    data = tj.compute_retrieval(snaps, ["A", "B", "C"])

    assert data["coverage_skew"] == 0.0       # C 0 页 / 中位 2 页


def test_单竞品单维度时偏斜与重复率不可比():
    snaps = [_snap("功能", "A", ["u1", "u2"], ["u1"])]
    data = tj.compute_retrieval(snaps, ["A"])

    assert data["coverage_skew"] is None      # 没有第二个竞品可比较
    assert data["cross_dim_dup"] is None      # 没有第二个维度可比较
    assert data["recall_avg"] == 2.0


def test_无快照返回None而不是全0():
    assert tj.compute_retrieval([]) is None


def test_jsonb数组回成字符串也能算():
    # asyncpg 未注册 codec 时 jsonb 回 str；真实库里已经踩过这个坑
    snaps = [{
        "dimension": "功能", "competitor": "A",
        "recalled_urls": '["u1","u2"]', "reranked_urls": '["u1"]',
    }]
    data = tj.compute_retrieval(snaps, ["A"])
    assert data["recall_avg"] == 2.0 and data["rerank_avg"] == 1.0


def test_L3落库五列随检索数据走():
    trace = tj.build_trace("t-l3", [_think(1, "finish")])
    assert all(
        tj.to_row(trace)[c] is None
        for c in ("retrieval_cells", "recall_avg", "rerank_avg",
                  "coverage_skew", "cross_dim_dup")
    )

    trace.retrieval = tj.compute_retrieval(
        [_snap("功能", "A", ["u1", "u2"], ["u1"]),
         _snap("定价", "A", ["u1"], ["u1"])],
        ["A"],
    )
    row = tj.to_row(trace)
    assert row["retrieval_cells"] == 2
    assert row["recall_avg"] == 1.5
    assert row["coverage_skew"] is None       # 单竞品
    assert row["cross_dim_dup"] == 0.5        # 功能{u1} + 定价{u1} → 1 - 1/2


def test_L3展示与落库同源():
    trace = tj.build_trace("t-l3b", [_think(1, "finish")])
    trace.retrieval = tj.compute_retrieval(
        [_snap("功能", "A", ["u1", "u2", "u3"], ["u1"]),
         _snap("功能", "B", ["u4"], ["u4"])],
        ["A", "B"],
    )
    l3 = tj.l3_metrics(trace)
    row = tj.to_row(trace)

    assert l3["检索快照"]["value"] == row["retrieval_cells"]
    assert l3["竞品覆盖偏斜"]["value"] == row["coverage_skew"]


def test_无检索数据的任务L3全为n_a():
    l3 = tj.l3_metrics(tj.build_trace("t-none", [_think(1, "finish")]))
    assert all(item["value"] == "n/a" for item in l3.values())
