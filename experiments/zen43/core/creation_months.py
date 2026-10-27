"""为未知文章选择日志月份：批量补取创建时间，创建月优先，再逐月向现在查。

搜索响应的 create_time 不可用；元数据通过 request_doc_info 映射回 Wiki token。
时间不可用时保留原来的由近到远路径。只决定读取顺序，不改变社区日期过滤或公开返回。
"""

from datetime import datetime
import json

from .monthly_logs import months_between
from .source import daily


def month_routes(context, tokens, since, until):
    """为 tokens 批量取得创建月，返回 token 到查找月份列表；范围由 ISO 日期 since/until 限定。

    输入去重，每批最多 200 篇；响应顺序及底层文档 token 均不能用于对应输入。
    单篇失败或时间无效时退回范围内由近到远；整个接口失败由调用层报错。
    """
    unique = list(dict.fromkeys(tokens))
    descending = months_between(since, until)
    routes = {token: list(descending) for token in unique}
    diagnostics = context.diagnostics.setdefault("creation_months", {})
    for offset in range(0, len(unique), 200):
        batch = unique[offset:offset + 200]
        payload = {"request_docs": [{"doc_token": token, "doc_type": "wiki"} for token in batch]}
        data = context.call(["drive", "metas", "batch_query", "--data", json.dumps(payload)])["data"]
        failed = {item["token"]: item.get("code") for item in data.get("failed_list", [])}
        for token in batch:
            diagnostics[token] = {"month": None, "fallback": "metadata_missing"}
            if token in failed:
                diagnostics[token] = {"month": None, "fallback": "metadata_failed", "code": failed[token]}
        for meta in data.get("metas", []):
            token = meta.get("request_doc_info", {}).get("doc_token")
            if token not in batch or token in failed:
                continue
            try:
                seconds = int(meta.get("create_time", 0))
                if seconds <= 0:
                    raise ValueError("创建时间无效")
                month = datetime.fromtimestamp(seconds, daily.reading_timezone()).strftime("%Y-%m")
            except (TypeError, ValueError, OverflowError, OSError):
                diagnostics[token] = {"month": None, "fallback": "invalid_create_time"}
                continue
            # 日期窗口只约束社区日期；创建较早的文章仍从窗口首月开始向后查。
            routes[token] = [value for value in reversed(descending) if value >= month]
            diagnostics[token] = {"month": month, "fallback": None}
    return routes


def next_month(routes, found, read_months):
    """根据 routes、已找到文章 found 和已读月份 read_months 返回下一月份；无工作时返回 None。

    先处理各文章的首选月，再推进后续月；共享月份只读一次，已找到的文章立即停止查找。
    """
    options = [(depth, order, month) for order, (token, months) in enumerate(routes.items())
               if token not in found for depth, month in enumerate(months) if month not in read_months]
    return min(options)[2] if options else None
