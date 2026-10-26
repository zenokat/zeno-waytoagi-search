"""全文检索的离线行为核对：命中章节快照、原文顺序、空结果成本及修订一致性。

只替换外部 CLI 响应；实际解析和定位仍执行原型代码，不代表线上扩样验收。
"""

from contextlib import redirect_stdout
import io
import json
import tempfile
import unittest
from unittest.mock import patch

from experiments.zen43.demos.demo import compact_result, section_nodes
from experiments.zen43.core.prototype import Prototype
from experiments.zen43.core.source import daily, xml_root
from experiments.zen43.prototype import main


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
        self.assertNotIn("title", result["match_tree"][0])
        parent = result["match_tree"][1]
        self.assertEqual(parent["title"], "父章")
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
        self.assertEqual(["".join(xml_root(h["snippet_xml"]).itertext()) for h in parent["hits"]], ["父章命中"])
        self.assertEqual([s["unit_id"] for s in parent["children"]], ["a", "c"])
        self.assertEqual(["".join(xml_root(h["snippet_xml"]).itertext()) for h in parent["children"][0]["hits"]], ["早片段", "晚片段"])
        self.assertNotIn("section_chain", json.dumps(result))

    def test_no_heading_has_one_reading_unit(self):
        """无标题文章的多处命中归入同一个全文读取单位，原文顺序保持。"""
        self.full = '<p id="first">前文</p><p id="last">后文</p>'
        self.keyword = '<fragment><p id="last">后文</p><p id="first">前文</p></fragment>'
        result = Prototype(self.folder.name).locate("article", ["文"])
        self.assertEqual(len(result["match_tree"]), 1)
        section = result["match_tree"][0]
        self.assertEqual(section["unit_id"], "@document")
        self.assertNotIn("title", section)
        self.assertEqual(["".join(xml_root(h["snippet_xml"]).itertext()) for h in section["hits"]], ["前文", "后文"])

    def test_changed_revision_is_rejected(self):
        """关键词与结构修订不一致时拒绝拼接；预期抛出 ValueError。"""
        self.revision = 2
        with self.assertRaisesRegex(ValueError, "自动刷新后仍"):
            Prototype(self.folder.name).locate("article", ["关键词"])
        self.assertEqual(self.scopes, ["keyword", "full", "keyword", "full"])

    def test_locate_refreshes_after_revision_changes(self):
        """关键词与结构跨版本后，整次定位自动重做，只交付新版本片段；无额外用户调用。"""
        def response(arguments):
            """首次关键词为旧版，其后来源稳定到新版；返回原生文档信封。"""
            scope = arguments[arguments.index("--scope") + 1]
            self.scopes.append(scope)
            revision = 1 if len(self.scopes) == 1 else 2
            content = f'<p id="opening">版本 {revision}</p>' if scope == "keyword" else self.full
            return {"data": {"document": {"content": content, "revision_id": revision}}}

        with patch.object(daily, "run_cli", side_effect=response):
            result = Prototype(self.folder.name).locate("article", ["版本"])
        self.assertIn("版本 2", result["match_tree"][0]["hits"][0]["snippet_xml"])
        self.assertEqual(self.scopes, ["keyword", "full", "keyword"])

    def test_container_excerpts_keep_distinct_hits_without_expansion(self):
        """同一表格的不同原生节选均保留，精确重复节选合并；不补取整表或无关行。"""
        self.full = '<h1 id="chapter">表格章</h1><table id="table"><tr><td>未命中行</td></tr></table>'
        first = '<excerpt top-block-id="table"><table><tr><td>命中一</td></tr></table></excerpt>'
        second = '<excerpt top-block-id="table"><table><tr><td>命中二</td></tr></table></excerpt>'
        self.keyword = '<fragment>' + first + second + first + '</fragment>'
        result = Prototype(self.folder.name).locate("article", ["命中"])
        hits = result["match_tree"][0]["hits"]
        self.assertEqual(len(hits), 2)
        self.assertEqual(["".join(xml_root(hit["snippet_xml"]).itertext()) for hit in hits], ["命中一", "命中二"])
        self.assertTrue(all(not hit["container_expanded"] for hit in hits))
        self.assertTrue(all(len(list(xml_root(hit["snippet_xml"]).iter("tr"))) == 1 for hit in hits))
        self.assertNotIn("top-block-id", json.dumps(hits))
        self.assertEqual(self.scopes, ["keyword", "full"])

    def test_container_excerpts_sort_by_inner_source_blocks(self):
        """同表格节选逆序返回时，使用已有子块 ID 恢复原文顺序，不补读或扩展片段。"""
        self.full = ('<h1 id="chapter">表格章</h1><table id="table">'
                     '<tr><td><p id="early">前行</p></td></tr>'
                     '<tr><td><p id="late">后行</p></td></tr></table>')
        self.keyword = ('<fragment><excerpt top-block-id="table"><table><tr><td>'
                        '<p id="late">后行</p></td></tr></table></excerpt>'
                        '<excerpt top-block-id="table"><table><tr><td>'
                        '<p id="early">前行</p></td></tr></table></excerpt></fragment>')
        result = Prototype(self.folder.name).locate("article", ["行"])
        hits = result["match_tree"][0]["hits"]
        self.assertEqual(["".join(xml_root(hit["snippet_xml"]).itertext()) for hit in hits], ["前行", "后行"])
        self.assertEqual(self.scopes, ["keyword", "full"])
        self.assertTrue(all(set(hit) == {"snippet_xml", "container_expanded"} for hit in hits))

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
                self.assertNotIn('id="', hit["snippet_xml"])
        self.assertIn("snippet_xml", result["match_tree"][0]["hits"][0])


if __name__ == "__main__":
    unittest.main()
