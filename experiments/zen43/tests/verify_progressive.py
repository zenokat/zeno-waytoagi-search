"""不联网的原型行为核对：分页状态不漏候选、历史七天缓存、候选定向复用及共享摘要。

只替代外部 CLI 响应与当前日期；真实解析、月份缓存和续查状态仍由原型执行。
"""

from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments.zen43.core.prototype import Prototype
from experiments.zen43.core.source import daily
from experiments.zen43.core.monthly_logs import HISTORY_TTL_SECONDS, MonthlyLogs


def log_xml(month, tokens):
    """将 month 与 tokens 构造成日期日志 XML；返回保持真实引用格式的小样本。"""
    year, number = map(int, month.split("-"))
    rows = ''.join(f'<li id="entry{token}">《<cite doc-id="{token}" file-type="wiki" title="{token}"/>》原摘要 {token}</li>' for token in tokens)
    return f'<h2>{year} 年 {number} 月</h2><h3>{number} 月 20 日</h3><ul>{rows}</ul>'


class ProgressiveChecks(unittest.TestCase):
    """使用小型外部响应验证真实工具入口的可观察行为。"""

    def setUp(self):
        """建立独立临时缓存、固定日期及外部 CLI 替身；无返回值。"""
        self.folder = tempfile.TemporaryDirectory(prefix="zen43-check-")
        self.addCleanup(self.folder.cleanup)
        self.anchor = json.loads((Path(__file__).resolve().parents[1] / "data/monthly-fixtures.json").read_text())
        self.by_id = {v["block_id"]: k for k, v in self.anchor["months"].items()}
        self.requests = []
        self.clock = patch("experiments.zen43.core.monthly_logs.datetime")
        mocked = self.clock.start()
        self.date_clock = mocked
        mocked.now.return_value.date.return_value = date(2026, 10, 24)
        self.addCleanup(self.clock.stop)
        self.cli = patch.object(daily, "run_cli", side_effect=self.respond)
        self.cli.start()
        self.addCleanup(self.cli.stop)

    def respond(self, args):
        """按 args 返回文档或两页候选；第二页放入创建较早但十月收录的 late，返回 CLI 字典。"""
        self.requests.append(args)
        if args[:2] == ["drive", "+search"]:
            later = "--page-token" in args
            tokens = ["late"] if later else ["a", "b", "c", "d", "september"]
            return {"data": {"results": [{"result_meta": {"token": t, "url": "https://example.com/"+t},
                                          "summary_highlighted": "片段 "+t} for t in tokens],
                             "has_more": not later, "page_token": None if later else "next"}}
        block = args[args.index("--start-block-id") + 1]
        if args[args.index("--scope") + 1] == "range":
            token = block.removeprefix("entry")
            content = f'<fragment><excerpt top-block-id="container"><ul><li id="{block}">《<cite doc-id="{token}" file-type="wiki" title="{token}"/>》刷新摘要 {token}</li></ul></excerpt></fragment>'
            return {"data": {"document": {"content": content, "revision_id": 2}}}
        if block == self.anchor["recent"]["block_id"]:
            content = "<h1>近 7 日更新日志</h1>"
        else:
            month = self.by_id[block]
            tokens = ["a", "b", "c", "d", "late"] if month == "2026-10" else ["september", "peer"] if month == "2026-09" else ["oldarticle"] if month == "2025-06" else []
            content = log_xml(month, tokens)
        return {"data": {"document": {"content": content, "revision_id": 1}}}

    def context(self):
        """返回共用测试缓存的新工具上下文，模拟新提交或新主题。"""
        return Prototype(self.folder.name)

    def test_continuation_fills_three_across_native_pages_and_months(self):
        """首轮和续轮均凑满三条；第二轮自动跨原生页及月份，六篇文章不重复。"""
        first = self.context().search(query="RAG")
        second = self.context().search(cursor=first["next_cursor"])
        self.assertEqual([r["article_id"] for r in first["results"]], ["a", "b", "c"])
        self.assertEqual([r["article_id"] for r in second["results"]], ["d", "september", "late"])
        self.assertEqual(second["status"], "finished")
        self.assertIsNone(second["next_cursor"])

    def test_comment_only_matches_are_filtered_before_filling_page(self):
        """仅评论命中的文章不交付，也不暴露评论状态；其余正常结果补足三条。"""
        def response(arguments):
            """输入原生命令，给 a 设置评论链接；其他响应沿用测试来源。"""
            result = self.respond(arguments)
            if arguments[:2] == ["drive", "+search"]:
                for row in result["data"]["results"]:
                    if row["result_meta"]["token"] == "a":
                        row["result_meta"]["url"] += "?comment_id=comment"
            return result

        with patch.object(daily, "run_cli", side_effect=response):
            result = self.context().search(query="RAG")
        self.assertEqual([r["article_id"] for r in result["results"]], ["b", "c", "d"])
        self.assertNotIn("snippet_status", json.dumps(result))

    def test_progress_keeps_late_candidate_and_snippets(self):
        """首屏只取一页；本地续页后继续推进，较晚出现的十月文章不丢失、不重复。"""
        p = self.context()
        result = p.search(query="RAG")
        self.assertEqual(len(result["results"]), 3)
        self.assertEqual(p.diagnostics["native_pages"], 1)
        self.assertEqual(p.diagnostics["months_read"], ["2026-10"])
        found = list(result["results"])
        before = len(self.requests)
        result = self.context().search(cursor=result["next_cursor"])
        self.assertGreater(len(self.requests), before)
        self.assertEqual(len(result["results"]), 3)
        found.extend(result["results"])
        for _ in range(25):
            if not result["next_cursor"]:
                break
            result = self.context().search(cursor=result["next_cursor"])
            found.extend(result["results"])
        self.assertIsNone(result["next_cursor"])
        self.assertEqual({r["article_id"] for r in found}, {"a", "b", "c", "d", "late", "september"})
        self.assertEqual(len(found), 6)
        self.assertTrue(all(r["snippet"].startswith("片段") for r in found))
        self.assertNotIn("coverage", result)

    def test_history_reused_across_topics_until_seven_days(self):
        """同月份跨主题复用且不查修订；第七天到期后只重新读该月。"""
        with patch("experiments.zen43.core.monthly_logs.time.time", return_value=1000000):
            self.context().search(query="RAG", since="2026-09-01", until="2026-09-30")
        docs_before = len([a for a in self.requests if a[:2] == ["docs", "+fetch"]])
        with patch("experiments.zen43.core.monthly_logs.time.time", return_value=1000000 + HISTORY_TTL_SECONDS - 1):
            self.context().search(query="Agent", since="2026-09-01", until="2026-09-30")
        self.assertEqual(len([a for a in self.requests if a[:2] == ["docs", "+fetch"]]), docs_before)
        with patch("experiments.zen43.core.monthly_logs.time.time", return_value=1000000 + HISTORY_TTL_SECONDS):
            self.context().search(since="2026-09-01", until="2026-09-30")
        self.assertEqual(len([a for a in self.requests if a[:2] == ["docs", "+fetch"]]), docs_before+1)
        self.assertTrue(all("outline" not in a and "full" not in a for a in self.requests))

    def test_empty_native_page_continues_until_exhausted(self):
        """空原生页仍可继续时自动续查，最终耗尽才交付空结果，不泄露中间空页。"""
        responses = [{"data": {"results": [], "has_more": True, "page_token": "next"}},
                     {"data": {"results": [], "has_more": False}}]
        with patch.object(daily, "run_cli", side_effect=responses):
            p = self.context()
            result = p.search(query="RAG")
        self.assertEqual(len(p.calls), 2)
        self.assertEqual(result["results"], [])
        self.assertIsNone(result["next_cursor"])
        self.assertEqual(result["status"], "finished")

    def test_warm_search_only_requests_native_candidates(self):
        """已有当月记录时只重新检索候选；摘要共享缓存，纯日期发现仍重读活跃月份。"""
        self.context().search(query="RAG")
        warm = self.context()
        result = warm.search(query="Agent")
        self.assertEqual(len(result["results"]), 3)
        self.assertEqual(len(warm.calls), 1)
        self.assertEqual(warm.calls[0]["arguments"][:2], ["drive", "+search"])
        self.assertEqual(warm.diagnostics["months_read"], [])
        summary = self.context()
        summary.summaries(["a"])
        self.assertEqual(summary.calls, [])
        discovery = self.context()
        discovery.search(since="2026-10-01")
        self.assertEqual(len(discovery.calls), 2)

    def test_search_fills_short_cached_page_without_rereading_history(self):
        """历史缓存不足三条时继续查当月补足，已匹配历史月份不重复读取。"""
        MonthlyLogs(self.context()).read("2026-09")
        p = self.context()
        result = p.search(query="RAG")
        self.assertEqual([r["article_id"] for r in result["results"]], ["a", "b", "c"])
        self.assertEqual(p.diagnostics["months_read"], ["2026-10"])
        self.assertTrue(any(a["article_id"] == "september" for a in p.diagnostics["record_actions"]))
        self.assertIsNotNone(result["next_cursor"])

    def test_search_refresh_fills_page_and_does_not_renew_month(self):
        """过期候选逐条刷新到满页，末页不足三条仍可完成；不延长整月期限。"""
        with patch("time.time", return_value=1000000):
            logs = MonthlyLogs(self.context())
            logs.read("2026-10")
        before = logs.cache_path("2026-10").read_text()
        with patch("time.time", return_value=1000000 + HISTORY_TTL_SECONDS):
            p = self.context()
            result = p.search(query="RAG")
            self.assertEqual([r["article_id"] for r in result["results"]], ["a", "b", "c"])
            self.assertEqual(len(p.calls), 4)
            self.assertIn("entrya", p.calls[1]["arguments"])
            session, generation = result["next_cursor"].split(":")
            path = p.cache / f"{session}-search-{generation}.json"
            state = json.loads(path.read_text())
            state.update(month_offset=len(state["months"]), native_done=True)
            path.write_text(json.dumps(state))
            continued = self.context()
            next_result = continued.search(cursor=result["next_cursor"])
            self.assertEqual([r["article_id"] for r in next_result["results"]], ["d"])
            self.assertEqual(len(continued.calls), 1)
            self.assertIn("entryd", continued.calls[0]["arguments"])
            self.assertIsNone(next_result["next_cursor"])
        self.assertEqual(logs.cache_path("2026-10").read_text(), before)

    def test_cache_selection_filters_dates_before_deduplication(self):
        """同文章在九月、十月都有记录，九月检索须选九月记录，不被十月覆盖。"""
        logs = MonthlyLogs(self.context())
        october = logs.read("2026-10")[0]
        logs.read("2026-09")
        path = logs.cache_path("2026-09")
        content = json.loads(path.read_text())
        content["records"].append({**october, "date": "2026-09-20"})
        path.write_text(json.dumps(content))
        p = self.context()
        result = p.search(query="RAG", since="2026-09-01", until="2026-09-30")
        self.assertIn("a", [r["article_id"] for r in result["results"]])
        self.assertTrue(all(r["date"].startswith("2026-09") for r in result["results"]))
        self.assertTrue(all(call["arguments"][:2] == ["drive", "+search"] for call in p.calls))
        self.assertTrue(all(source["cache_hit"] for source in p.sources))

    def test_date_search_fills_across_months_and_returns_short_final_page(self):
        """纯日期查询自动跨月凑满，最后一页不足三条时正常结束且不重复文章。"""
        first = self.context().search(since="2026-09-01", until="2026-10-24")
        second = self.context().search(cursor=first["next_cursor"])
        third = self.context().search(cursor=second["next_cursor"])
        self.assertEqual([len(page["results"]) for page in (first, second, third)], [3, 3, 1])
        self.assertEqual(third["status"], "finished")
        self.assertIsNone(third["next_cursor"])
        ids = [row["article_id"] for page in (first, second, third) for row in page["results"]]
        self.assertEqual(len(set(ids)), 7)

    def test_cyclic_native_pages_fail_instead_of_looping_to_fill(self):
        """原生令牌循环时明确失败；凑页循环不能因重复空页一直等待。"""
        responses = [{"data": {"results": [], "has_more": True, "page_token": token}}
                     for token in ("one", "two", "one")]
        with patch.object(daily, "run_cli", side_effect=responses):
            with self.assertRaisesRegex(ValueError, "分页未前进"):
                self.context().search(query="RAG")

    def test_old_cached_article_does_not_scan_newer_months(self):
        """旧文章已经在有效缓存中，即使从未检索它也应零请求返回；重复输入保留对应关系。"""
        MonthlyLogs(self.context()).read("2025-06")
        p = self.context()
        result = p.summaries(["oldarticle", "oldarticle"])
        self.assertEqual([r["summary"] for r in result["results"]], ["原摘要 oldarticle"] * 2)
        self.assertEqual(p.calls, [])

    def test_valid_record_precedes_expired_locator(self):
        """已有有效原摘要时直接返回，不因另一份过期记录日期较新而先发请求。"""
        with patch("time.time", return_value=1000000):
            logs = MonthlyLogs(self.context())
            old = logs.read("2026-10")[0]
        with patch("time.time", return_value=1000000 + HISTORY_TTL_SECONDS):
            logs.read("2026-09")
            path = logs.cache_path("2026-09")
            content = json.loads(path.read_text())
            content["records"].append({**old, "date": "2026-09-20", "summary": "有效范围内摘要"})
            path.write_text(json.dumps(content))
            p = self.context()
            result = p.summaries(["a"])
        self.assertEqual(result["results"][0]["summary"], "有效范围内摘要")
        self.assertEqual(p.calls, [])

    def test_expired_summary_refreshes_only_its_block(self):
        """满七天只刷新目标块，随后零请求复用；不延长整月或其他文章的有效期。"""
        with patch("time.time", return_value=1000000):
            logs = MonthlyLogs(self.context())
            logs.read("2026-09")
        before = logs.cache_path("2026-09").read_text()
        with patch("time.time", return_value=1000000 + HISTORY_TTL_SECONDS):
            p = self.context()
            result = p.summaries(["september", "september"])
            self.assertEqual(result["results"][0]["summary"], "刷新摘要 september")
            self.assertEqual(len(p.calls), 1)
            self.assertIn("range", p.calls[0]["arguments"])
            reused = self.context()
            reused.summaries(["september"])
            self.assertEqual(reused.calls, [])
            other = self.context()
            other.summaries(["peer"])
            self.assertEqual(len(other.calls), 1)
            self.assertIn("entrypeer", other.calls[0]["arguments"])
        self.assertEqual(logs.cache_path("2026-09").read_text(), before)

    def test_unfamiliar_old_article_scans_until_found(self):
        """只有完全陌生 token 才逐月查找；首次需要十八次读取，再次调用零请求。"""
        p = self.context()
        result = p.summaries(["oldarticle"])
        self.assertEqual(result["results"][0]["summary"], "原摘要 oldarticle")
        self.assertEqual(len(p.calls), 18)
        reused = self.context()
        reused.summaries(["oldarticle"])
        self.assertEqual(reused.calls, [])

    def test_wrong_block_identity_is_not_returned_as_summary(self):
        """过期位置返回了别的文章时明确失败，不写入成功缓存或悄悄逐月降级。"""
        with patch("time.time", return_value=1000000):
            MonthlyLogs(self.context()).read("2026-09")
        response = {"data": {"document": {"content": '<fragment><li id="entryseptember">《<cite doc-id="other" file-type="wiki" title="other"/>》别人的摘要</li></fragment>'}}}
        with patch("time.time", return_value=1000000 + HISTORY_TTL_SECONDS), patch.object(daily, "run_cli", return_value=response):
            with self.assertRaisesRegex(ValueError, "身份"):
                self.context().summaries(["september"])

    def test_damaged_cache_is_refetched(self):
        """缓存损坏时重取目标月份，不将损坏当作有效空结果。"""
        logs = MonthlyLogs(self.context())
        logs.cache_path("2026-09").write_text('{"fetched_at":"bad","stable":true,"records":[]}')
        self.assertEqual(logs.read("2026-09")[0]["article_id"], "september")
        self.assertFalse(logs.context.sources[-1]["cache_hit"])

    def test_previous_month_stays_active_until_rollover_finishes(self):
        """月初上月仍读取近期页；滚动期后首次重取才写稳定缓存，避免冻结未归档月份。"""
        self.date_clock.now.return_value.date.return_value = date(2026, 10, 8)
        early = self.context()
        MonthlyLogs(early).read("2026-09")
        self.assertTrue(early.sources[0]["active"])
        self.assertEqual(len(early.calls), 2)
        self.date_clock.now.return_value.date.return_value = date(2026, 10, 9)
        settled = self.context()
        MonthlyLogs(settled).read("2026-09")
        self.assertFalse(settled.sources[0]["cache_hit"])
        self.assertEqual(len(settled.calls), 1)
        reused = self.context()
        MonthlyLogs(reused).read("2026-09")
        self.assertTrue(reused.sources[0]["cache_hit"])
        self.assertEqual(reused.calls, [])


if __name__ == "__main__":
    unittest.main()
