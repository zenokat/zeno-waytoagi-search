"""原摘要读取：先匹配已有记录，过期按日志块刷新，陌生文章才逐月查找。

月份缓存与单条刷新分别计时；摘要复用已取得的记录，不证明文章在其他日志是否又有更新。
检索与摘要共用条目索引和定点刷新；单条刷新不能代表整月已更新。
"""

from datetime import date
import hashlib
import json
import time
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

from experiments.zen43.core.source import daily, log_entries, write_json, xml_root
from experiments.zen43.core.monthly_logs import HISTORY_TTL_SECONDS, MONTH_CACHE_VERSION, MonthlyLogs, merge_latest, months_between

RECORD_CACHE_VERSION = 1


def load_cache(path):
    """读取 path 的 JSON 字典；文件不存在或损坏时返回空字典，不触发任何网络请求。"""
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def record_cache_path(context):
    """按 context 的配置返回单条刷新缓存路径；与月级缓存分离，防止错误延长整月期限。"""
    key = hashlib.sha256((context.profile or "default").encode()).hexdigest()[:16]
    return context.cache / f"summary-records-{key}.json"


def cached_records(logs, since=None, until=None):
    """从 logs 的本地缓存建立 token 索引；先按可选 since/until 筛日期，返回记录及获取时间。"""
    candidates = []
    for month in logs.anchors["months"]:
        cached = load_cache(logs.cache_path(month))
        if cached.get("version") == MONTH_CACHE_VERSION:
            candidates.extend({"record": row, "fetched_at": cached.get("fetched_at")}
                              for row in cached.get("records", []))
    refreshed = load_cache(record_cache_path(logs.context))
    if refreshed.get("version") == RECORD_CACHE_VERSION:
        candidates.extend(refreshed.get("entries", {}).values())
    selected, ranks, now = {}, {}, time.time()
    for item in candidates:
        if not isinstance(item.get("fetched_at"), (int, float)):
            continue
        token, row = item["record"]["article_id"], item["record"]
        if (since and row["date"] < since) or (until and row["date"] > until):
            continue
        # 先选有效记录；只有没有有效记录时，才使用过期记录中的位置刷新。
        # 同类记录再按已观测日期选新，避免为了追求范围外的最新更新破坏零请求复用。
        rank = (0 <= now - item["fetched_at"] < HISTORY_TTL_SECONDS, row["date"], item["fetched_at"])
        if token not in ranks or rank > ranks[token]:
            selected[token] = item
            ranks[token] = rank
    return selected


def refresh_record(context, record):
    """按 record 的已知日志位置刷新一条摘要；核对块与文章身份，返回原记录日期下的新内容。"""
    block = record["log_block_id"]
    document = context.call(["docs", "+fetch", "--doc", record["log_url"], "--scope", "range",
        "--doc-format", "xml", "--detail", "with-ids", "--max-depth", "-1",
        "--start-block-id", block, "--end-block-id", block])["data"]["document"]
    if document.get("tips"):
        raise ValueError("摘要条目刷新含不完整提示")
    root = xml_root(document.get("content", ""))
    nodes = [node for node in root.iter() if node.get("id") == block and node.tag in {"li", "p"}]
    if len(nodes) != 1 or any(node.tag == "excerpt" for node in nodes[0].iter()):
        raise ValueError("摘要条目位置或完整性无法确认；需核对失效锚点")
    # 外层 excerpt 可表示容器中的部分条目；仅接受具有精确 ID 的完整目标条目。
    # 用已知批次日期复用原解析器，不把这次读取时间冒充社区更新日期。
    day = date.fromisoformat(record["date"])
    content = f"<h3>{day.year} 年 {day.month} 月 {day.day} 日</h3>" + ET.tostring(nodes[0], encoding="unicode")
    rows, unsupported, _ = log_entries(content, record["log_url"], day)
    if unsupported or len(rows) != 1 or rows[0]["article_id"] != record["article_id"]:
        raise ValueError("摘要条目身份或格式不符；不交付其他文章的内容")
    path = record_cache_path(context)
    cached = load_cache(path)
    entries = cached.get("entries", {}) if cached.get("version") == RECORD_CACHE_VERSION else {}
    entries[record["article_id"]] = {"record": rows[0], "fetched_at": time.time()}
    write_json(path, {"version": RECORD_CACHE_VERSION, "entries": entries})
    return rows[0]


def summaries(context, articles):
    """为 articles 返回对应原摘要；缓存零请求、过期定点刷新，只有未知记录才逐月查找。"""
    logs, tokens, found, actions = MonthlyLogs(context), {}, {}, []
    for article in articles:
        token = urlparse(article).path.rstrip("/").rsplit("/", 1)[-1] if "://" in article else article
        tokens[article] = context.resolve(article)["node_token"] if "/docx/" in article else token
    cached = cached_records(logs)
    for token in dict.fromkeys(tokens.values()):
        item = cached.get(token)
        if item is None:
            continue
        row, age = item["record"], time.time() - item["fetched_at"]
        if 0 <= age < HISTORY_TTL_SECONDS:
            found[token], mode = row, "cached_record"
        elif row.get("log_url") and row.get("log_block_id"):
            found[token], mode = refresh_record(context, row), "refreshed_record"
        else:
            continue
        actions.append({"article_id": token, "mode": mode, "date": found[token]["date"]})
    for month in months_between(str(daily.SUPPORTED_FROM), str(logs.today)):
        if all(token in found for token in tokens.values()):
            break
        current = {}
        merge_latest(current, logs.read(month))
        for token in tokens.values():
            if token in current and token not in found:
                found[token] = current[token]
                actions.append({"article_id": token, "mode": "monthly_lookup", "month": month})
    # 保留既有裸文档别名兼容：未直接命中时解析，并在本次已读日志中重试，不新增日志请求。
    for article, token in list(tokens.items()):
        if token not in found:
            resolved = context.resolve(article)["node_token"]
            tokens[article] = resolved
            for records in logs.loaded.values():
                current = {}
                merge_latest(current, records)
                if resolved in current:
                    found[resolved] = current[resolved]
                    break
    results = []
    for article in articles:
        token = tokens[article]
        row = found.get(token)
        result = {"input": article, "article_id": token}
        if row and row["summary"].strip():
            result.update({field: row[field] for field in ("url", "title", "date", "summary")})
            result["status"] = "found"
        else:
            result["status"] = "no_summary_in_matched_record" if row else "not_found_in_observed_logs"
        results.append(result)
    context.diagnostics = {"sources": context.sources, "months_read": list(logs.loaded), "summary_actions": actions}
    return {"results": results}
