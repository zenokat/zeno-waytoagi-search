"""默认三条的渐进检索：保存候选与月度匹配进度，按需继续，不以创建月份丢弃文章。

先按候选 token 定向匹配缓存；过期定点刷新，未知记录才逐月查找。
一次提交按需推进候选、过期条目与月份，凑满三条或查完后才交付。
来源与耗时诊断仅留上下文，Agent 只收到业务结果、继续标识及必要状态。
"""

from datetime import date
import hashlib
import json
import re
import time

from experiments.zen43.core.source import SPACE, daily, write_json
from experiments.zen43.core.monthly_logs import HISTORY_TTL_SECONDS, MonthlyLogs, merge_latest, months_between
from experiments.zen43.core.summary_reader import cached_records, refresh_record

PAGE_SIZE = 3


def fetch_candidates(context, state):
    """给 state 增加一页创建日期倒序的原生候选；保留未匹配项，无返回值。"""
    args = ["drive", "+search", "--query", state["query"], "--space-ids", SPACE,
            "--sort", "create_time", "--page-size", "20"]
    if state["native_token"]:
        args += ["--page-token", state["native_token"]]
    data = context.call(args)["data"]
    if data.get("notice"):
        raise ValueError("原生搜索含 notice，需核对")
    for item in data.get("results", []):
        meta = item["result_meta"]
        if "comment_id" in meta.get("url", ""):
            continue
        candidate = {"snippet": item.get("summary_highlighted", "")}
        state["candidates"].setdefault(meta["token"], candidate)
    has_more = bool(data.get("has_more"))
    next_token = data.get("page_token")
    seen_tokens = state.setdefault("native_tokens", [])
    if has_more and (not next_token or next_token == state["native_token"] or next_token in seen_tokens):
        raise ValueError("原生分页未前进")
    if has_more:
        seen_tokens.append(next_token)
    state["native_token"] = next_token if has_more else None
    state["native_done"] = not has_more
    state["native_pages"] += 1


def match_cached_candidates(logs, state, actions):
    """按 state 候选和日期范围匹配本地缓存；写入有效记录及 actions，返回待定点刷新的记录。"""
    if not state["query"]:
        return []
    cached = cached_records(logs, state["since"], state["until"])
    expired = []
    for token in state["candidates"]:
        if token in state["records"] or token not in cached:
            continue
        item = cached[token]
        row, age = item["record"], time.time() - item["fetched_at"]
        if 0 <= age < HISTORY_TTL_SECONDS:
            state["records"][token] = row
            actions.append({"article_id": token, "mode": "cached_record", "date": row["date"]})
        elif row.get("log_url") and row.get("log_block_id"):
            expired.append(row)
    return expired


def fetch_month(logs, state):
    """读取 state 尚未处理的最新月份并合并范围内记录；递增月份进度，无返回值。"""
    month = state["months"][state["month_offset"]]
    records = [r for r in logs.read(month) if state["since"] <= r["date"] <= state["until"]]
    merge_latest(state["records"], records)
    state["month_offset"] += 1


def available_results(state):
    """从 state 生成尚未交付的合格业务结果；返回列表，原生顺序优先，纯日期用日期倒序。"""
    delivered = set(state["delivered"])
    keys = state["candidates"] if state["query"] else sorted(
        state["records"], key=lambda key: (state["records"][key]["date"], key), reverse=True)
    rows = []
    for key in keys:
        record = state["records"].get(key)
        if key in delivered or not record or not record["summary"].strip():
            continue
        row = {field: record[field] for field in ("article_id", "url", "title", "date")}
        if state["query"]:
            row.update(state["candidates"][key])
        rows.append(row)
    return rows


def pending_work(state):
    """判断 state 是否还需取原生页或日志月；返回两个布尔值，不把未匹配当缺摘要。"""
    native = bool(state["query"] and not state["native_done"])
    unresolved = not state["query"] or any(key not in state["records"] for key in state["candidates"])
    month = state["month_offset"] < len(state["months"]) and unresolved
    return native, month


def search(context, query=None, since=None, until=None, cursor=None):
    """执行查询或续查；内部凑满三条或耗尽工作，返回业务结果、继续标识和状态。"""
    logs, actions = MonthlyLogs(context), []
    if cursor:
        match = re.fullmatch(r"([a-f0-9]{24}):(\d+)", cursor)
        if not match:
            raise ValueError("无效渐进检索游标")
        session, generation = match[1], int(match[2])
        state = json.loads((context.cache / f"{session}-search-{generation}.json").read_text())
        if state["profile"] != context.profile:
            raise ValueError("续查必须使用原查询配置")
        # 旧游标可能保存过仅评论候选；沿用进度时也按当前交付规则排除。
        state["candidates"] = {key: value for key, value in state["candidates"].items()
                               if value.get("snippet_status") != "comment_match_not_delivered"}
    else:
        if not query and not since and not until:
            raise ValueError("查询语句、日期范围至少一项")
        if query and len(query) > 30:
            raise ValueError("查询最多 30 个字符，不截短")
        for day in (since, until):
            if day and date.fromisoformat(day) < daily.SUPPORTED_FROM:
                raise ValueError("日志支持起点为 2025-06-01")
        if since and until and since > until:
            raise ValueError("开始日期晚于结束日期")
        start, end = since or str(daily.SUPPORTED_FROM), min(until or str(logs.today), str(logs.today))
        state = {"profile": context.profile, "query": query, "since": start, "until": end,
                 "months": months_between(start, end), "month_offset": 0, "records": {},
                 "candidates": {}, "delivered": [], "native_pages": 0,
                 "native_token": None, "native_done": not bool(query), "next_action": "month"}
        session = hashlib.sha256((str(time.time_ns()) + str(context.run_dir)).encode()).hexdigest()[:24]
        generation = 0
        if state["months"]:
            if query:
                fetch_candidates(context, state)
        else:
            state["native_done"] = True
    expired = match_cached_candidates(logs, state, actions)
    rows = available_results(state)
    while len(rows) < PAGE_SIZE:
        native, month = pending_work(state)
        # 两个方向交替推进，避免少数未关联候选迫使首屏扫描全部历史。
        if expired:
            row = refresh_record(context, expired.pop(0))
            state["records"][row["article_id"]] = row
            actions.append({"article_id": row["article_id"], "mode": "refreshed_record", "date": row["date"]})
        elif month and (state["next_action"] == "month" or not native):
            fetch_month(logs, state)
            state["next_action"] = "native"
        elif native:
            fetch_candidates(context, state)
            state["next_action"] = "month"
            expired = match_cached_candidates(logs, state, actions)
        else:
            break
        rows = available_results(state)
    page = rows[:PAGE_SIZE]
    state["delivered"].extend(row["article_id"] for row in page)
    native, month = pending_work(state)
    pending_refresh = any(row["article_id"] not in state["records"] for row in expired)
    continuing = bool(len(rows) > PAGE_SIZE or native or month or pending_refresh)
    next_cursor = None
    if continuing:
        next_cursor = f"{session}:{generation+1}"
        write_json(context.cache / f"{session}-search-{generation+1}.json", state)
    unknown = set(state["candidates"]) - set(state["records"])
    context.diagnostics = {"sources": context.sources, "record_actions": actions, "native_pages": state["native_pages"],
                           "native_candidates": len(state["candidates"]),
                           "months_read": state["months"][:state["month_offset"]],
                           "unmatched_candidates": len(unknown), "delivered": len(state["delivered"])}
    result = {"results": page, "next_cursor": next_cursor,
              "status": "can_continue" if continuing else "finished"}
    if not continuing and unknown:
        result["notice"] = "部分原生候选未关联社区日志，无法确认是否符合收录条件。"
    return result
