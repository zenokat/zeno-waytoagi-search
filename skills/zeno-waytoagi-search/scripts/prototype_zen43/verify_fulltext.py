"""全文检索的离线行为核对：命中章节快照、原文顺序、空结果成本及修订一致性。

只替换外部 CLI 响应；实际解析和定位仍执行原型代码，不代表线上扩样验收。
"""

from contextlib import redirect_stdout
import io
import json
import tempfile
import unittest
from unittest.mock import patch

from demo import compact_result, section_nodes
from prototype import Prototype, daily, main, xml_root


class FulltextChecks(unittest.TestCase):
    """用最小文档核对定位结果及外部请求，避免把演示精简误当业务返回。"""

    def setUp(self):
        """建立独立临时缓存和响应替身；无参数及返回值。"""
        self.folder = tempfile.TemporaryDirectory(prefix="zen43-fulltext-check-")
        self.addCleanup(self.folder.cleanup)
        self.keyword = '<fragment><p id="opening">开篇关键词</p><h3 id="child">子章关键词</h3></fragment>'
        self.full = '<p id="opening">开篇关键词</p><h1 id="parent">父章</h1><h3 id="child">子章关键词</h3><p id="body">正文</p>'
        self.revision = 1
        self.scopes = []
        cli = patch.object(daily, "run_cli", side_effect=self.respond)
        cli.start()
        self.addCleanup(cli.stop)

    def respond(self, arguments):
        """根据 arguments 的读取范围返回 XML 和修订号；返回 CLI 响应字典。"""
        scope = arguments[arguments.index("--scope") + 1]
        self.scopes.append(scope)
        return {"data": {"document": {"content": self.keyword if scope == "keyword" else self.full,
                                       "revision_id": 1 if scope == "keyword" else self.revision}}}

    def test_empty_result_needs_only_keyword(self):
        """无命中时仅查关键词，不为不存在的命中读取全文；返回空 match_tree。"""
        self.keyword = ""
        result = Prototype(self.folder.name).locate("article", ["不存在"])
        self.assertEqual(result["match_tree"], [])
        self.assertEqual(self.scopes, ["keyword"])

    def test_opening_and_heading_keep_unit_ids(self):
        """开篇映射到开篇单位；跳级标题保留父子链，标题片段不附送正文。"""
        result = Prototype(self.folder.name).locate("article", ["关键词"])
        self.assertEqual(result["match_tree"][0]["unit_id"], "@opening")
        parent = result["match_tree"][1]
        self.assertEqual(parent["unit_id"], "parent")
        self.assertEqual(parent["hits"], [])
        child = parent["children"][0]
        self.assertEqual(child["unit_id"], "child")
        self.assertNotIn("正文", child["hits"][0]["snippet_xml"])

    def test_grouped_snapshot_sorts_and_prunes(self):
        """原生乱序命中按原文排序；共用祖先只出现一次，不显示无关兄弟章，不重复子章片段。"""
        self.full = ('<h1 id="parent">父章</h1><p id="intro">父章命中</p>'
                     '<h3 id="a">第一节</h3><p id="a1">早片段</p><p id="a2">晚片段</p>'
                     '<h3 id="b">无关节</h3><p id="b1">不相关</p>'
                     '<h3 id="c">第三节</h3><p id="c1">第三节命中</p>'
                     '<h1 id="other">无关章</h1>')
        self.keyword = '<fragment><p id="c1">第三节命中</p><p id="a2">晚片段</p><p id="intro">父章命中</p><p id="a1">早片段</p></fragment>'
        result = Prototype(self.folder.name).locate("article", ["关键词"])
        self.assertEqual(len(result["match_tree"]), 1)
        parent = result["match_tree"][0]
        self.assertEqual([xml_root(h["snippet_xml"])[0].get("id") for h in parent["hits"]], ["intro"])
        self.assertEqual([s["unit_id"] for s in parent["children"]], ["a", "c"])
        self.assertEqual([xml_root(h["snippet_xml"])[0].get("id") for h in parent["children"][0]["hits"]], ["a1", "a2"])
        self.assertNotIn("section_chain", json.dumps(result))

    def test_no_heading_has_one_reading_unit(self):
        """无标题文章的多处命中归入同一个全文读取单位，原文顺序保持。"""
        self.full = '<p id="first">前文</p><p id="last">后文</p>'
        self.keyword = '<fragment><p id="last">后文</p><p id="first">前文</p></fragment>'
        result = Prototype(self.folder.name).locate("article", ["文"])
        self.assertEqual(len(result["match_tree"]), 1)
        section = result["match_tree"][0]
        self.assertEqual(section["unit_id"], "@document")
        self.assertEqual([xml_root(h["snippet_xml"])[0].get("id") for h in section["hits"]], ["first", "last"])

    def test_changed_revision_is_rejected(self):
        """关键词与结构修订不一致时拒绝拼接；预期抛出 ValueError。"""
        self.revision = 2
        with self.assertRaisesRegex(ValueError, "文章发生更新"):
            Prototype(self.folder.name).locate("article", ["关键词"])

    def test_demo_does_not_remove_snippets(self):
        """演示保留定位业务 JSON 原样，包括片段，不添加只用于展示的计数。"""
        result = Prototype(self.folder.name).locate("article", ["关键词"])
        self.assertEqual(compact_result(result), result)

    def test_cli_returns_business_json_without_diagnostics(self):
        """命令行成功输出与页面所示业务字段一致，不夹带内部证据目录；返回码为零。"""
        output = io.StringIO()
        with patch("sys.argv", ["prototype.py", "locate", "--cache-dir", self.folder.name]), redirect_stdout(output):
            status = main()
        result = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertIn("match_tree", result)
        self.assertNotIn("evidence_dir", result)
        self.assertNotIn("structure_scope", result)
        self.assertNotIn("completeness", result)
        for node in section_nodes(result["match_tree"]):
            for hit in node["hits"]:
                self.assertNotIn("block_id", hit)
        self.assertIn("snippet_xml", result["match_tree"][0]["hits"][0])


if __name__ == "__main__":
    unittest.main()
