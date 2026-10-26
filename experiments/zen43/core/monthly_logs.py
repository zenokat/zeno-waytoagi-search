"""原型共用的月份日志读取：活跃月份正常请求，历史月份按需使用七天缓存。

复用已观测章节锚点，避免用整年修订号使所有月份失效。
新月份或锚点结构变化明确报错，自动重新定位由 ZEN-44 验证。
"""

from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import time

from experiments.zen43.core.source import daily, log_entries, write_json

HISTORY_TTL_SECONDS = 7 * 24 * 60 * 60
MONTH_CACHE_VERSION = 1


def months_between(since, until):
    """返回 since 至 until 覆盖的自然月，按新到旧排列；两个输入均为 ISO 日期字符串。"""
    first, last = date.fromisoformat(since), date.fromisoformat(until)
    month = date(last.year, last.month, 1)
    result = []
    while month >= date(first.year, first.month, 1):
        result.append(month.strftime("%Y-%m"))
        month = (month - timedelta(days=1)).replace(day=1)
    return result


def merge_latest(target, records):
    """将 records 按文章最近日期合并进 target；原地更新，无返回值，保留空摘要以免回退到旧摘要。"""
    for row in records:
        old = target.get(row["article_id"])
        if old is None or row["date"] > old["date"]:
            target[row["article_id"]] = row


class MonthlyLogs:
    """单次工具调用的月份读取器：共用 context 的跨主题磁盘缓存及调用证据。"""

    def __init__(self, context):
        """输入 Prototype 调用上下文；初始化当天日期、锚点及单次调用内的读取去重状态。"""
        self.context = context
        self.today = datetime.now(daily.reading_timezone()).date()
        self.anchors = json.loads((Path(__file__).resolve().parents[1] / "data/monthly-fixtures.json").read_text())
        self.loaded = {}
        self.recent = None

    def cache_path(self, month):
        """按配置、来源及 month 返回月级缓存路径；不包含查询词，可跨主题复用。"""
        source = self.anchors["months"].get(month, {}).get("url", "")
        key = hashlib.sha256(((self.context.profile or "default") + source).encode()).hexdigest()[:16]
        return self.context.cache / f"month-{key}-{month}.json"

    def section(self, anchor):
        """直接读取 anchor 对应日志章节；返回解析记录，未覆盖格式报错而不假装空结果。"""
        document = self.context.fetch(anchor["url"], "section", start_block_id=anchor["block_id"])
        records, unsupported, _ = log_entries(document["content"], anchor["url"], self.today)
        if unsupported:
            raise ValueError("月份日志存在未覆盖格式，需 ZEN-44 核对")
        return records

    def read(self, month):
        """取得 month 的全部已观测日志；返回记录并写内部来源诊断，历史缓存到期才实际读取。"""
        if month in self.loaded:
            return self.loaded[month]
        anchor = self.anchors["months"].get(month)
        if anchor is None:
            raise ValueError("原型尚无该月份的已验证锚点；新月份发现与锚点恢复交 ZEN-44")
        current = self.today.strftime("%Y-%m")
        # 月初上月可能仍有内容留在近期页。保守覆盖前八天，避免把尚未归档完的内容冻住。
        previous = (self.today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        active = month == current or (month == previous and self.today.day <= 8)
        path, now = self.cache_path(month), time.time()
        cached = None
        if not active and path.exists():
            try:
                cached = json.loads(path.read_text())
            except (OSError, ValueError):
                cached = None
        fetched_at = cached.get("fetched_at") if isinstance(cached, dict) else None
        age = now - fetched_at if isinstance(fetched_at, (int, float)) else None
        hit = bool(isinstance(cached, dict) and cached.get("version") == MONTH_CACHE_VERSION
                   and cached.get("stable") and isinstance(cached.get("records"), list)
                   and age is not None and 0 <= age < HISTORY_TTL_SECONDS)
        if hit:
            records = cached["records"]
        else:
            records = self.section(anchor)
            if any(row["date"][:7] != month for row in records):
                raise ValueError("月份锚点读取范围不符；不能将错误板块缓存为该月")
            if active:
                if self.recent is None:
                    self.recent = self.section(self.anchors["recent"])
                records += [row for row in self.recent if row["date"][:7] == month]
            write_json(path, {"version": MONTH_CACHE_VERSION, "stable": not active,
                              "fetched_at": time.time(), "records": records})
        self.loaded[month] = records
        self.context.sources.append({"month": month, "url": anchor["url"], "cache_hit": hit,
                                     "active": active, "records": len(records),
                                     "cache_age_seconds": round(age, 3) if hit else None})
        return records
