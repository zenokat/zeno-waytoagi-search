"""第四工具的离线语义检查：实际层数、局部范围与特殊读取单位。

仅替换外部 CLI 响应，执行实际解析代码；人工构造的标题和容器不冒充真实样本。
"""

from contextlib import redirect_stdout
import io
import json
import tempfile
import unittest
from unittest.mock import patch

from experiments.zen43.core.prototype import Prototype
from experiments.zen43.core.source import daily
from experiments.zen43.prototype import main
from experiments.zen43.demos.demo import section_nodes


class StructureChecks(unittest.TestCase):
    """以跳级标题和容器内标题核对已确认的章节定义。"""

    def setUp(self):
        """准备最小 XML 和独立临时目录；无参数及返回值。"""
        self.folder = tempfile.TemporaryDirectory(prefix="zen43-structure-check-")
        self.addCleanup(self.folder.cleanup)
        self.content = ('<p id="opening">开篇</p><h2 id="parent">父章</h2>'
                        '<h4 id="child">子章</h4><h6 id="leaf">孙章</h6>'
                        '<table id="table"><tr><td><h1 id="nested">容器标题</h1></td></tr></table>'
                        '<h2 id="other">下一章</h2>')
        self.calls = []
        cli = patch.object(daily, "run_cli", side_effect=self.respond)
        cli.start()
        self.addCleanup(cli.stop)

    def respond(self, arguments):
        """记录 arguments 并返回当前 XML；返回原生响应字典，不发起网络请求。"""
        self.calls.append(arguments)
        return {"data": {"document": {"content": self.content, "revision_id": 1}}}

    def test_default_starts_at_outermost_titles(self):
        """默认一层保留开篇与 H2，不把 H2 当作第二层。"""
        result = Prototype(self.folder.name).structure("article")
        self.assertEqual([u["unit_id"] for u in section_nodes(result["outline"])], ["@opening", "parent", "other"])

    def test_depth_counts_parent_child_relationships(self):
        """H2、H4、H6 分别算三层，容器内 H1 不成为章节。"""
        result = Prototype(self.folder.name).structure("article", depth=2)
        self.assertEqual([u["unit_id"] for u in section_nodes(result["outline"])], ["@opening", "parent", "child", "other"])
        deep = Prototype(self.folder.name).structure("article", depth=3)
        self.assertEqual([u["unit_id"] for u in section_nodes(deep["outline"])], ["@opening", "parent", "child", "leaf", "other"])

    def test_local_depth_excludes_base_and_siblings(self):
        """局部默认一层从所选章节的子层开始；不重复基节点或泄漏兄弟章节。"""
        result = Prototype(self.folder.name).structure("article", parent_id="parent")
        self.assertEqual([u["unit_id"] for u in section_nodes(result["outline"])], ["child"])
        deep = Prototype(self.folder.name).structure("article", parent_id="parent", depth=2)
        self.assertEqual([u["unit_id"] for u in section_nodes(deep["outline"])], ["child", "leaf"])

    def test_leaf_and_opening_have_no_descendants(self):
        """叶子章节与开篇单位查询成功但无子标题；不把自身重复作子项。"""
        for section in ["leaf", "@opening"]:
            self.assertEqual(Prototype(self.folder.name).structure("article", section)["outline"], [])

    def test_no_headings_exposes_document_unit_id(self):
        """无标题正文提供一个全文读取单位，选择该单位时没有下级标题。"""
        self.content = '<p id="body">无标题正文</p>'
        result = Prototype(self.folder.name).structure("article")
        self.assertEqual([u["unit_id"] for u in section_nodes(result["outline"])], ["@document"])
        self.assertEqual(result["outline"][0]["kind"], "document")
        self.assertNotIn("title", result["outline"][0])
        self.assertEqual(Prototype(self.folder.name).structure("article", "@document")["outline"], [])

    def test_container_heading_is_not_selectable(self):
        """容器标题不能作独立章节入口；预期拒绝，避免把表格节选误作完整章节。"""
        with self.assertRaisesRegex(ValueError, "不是可选"):
            Prototype(self.folder.name).structure("article", "nested")

    def test_business_tree_does_not_leak_positions(self):
        """树关系直接表达父子层级；读取标识可复用，内部块位置不交给调用方。"""
        result = Prototype(self.folder.name).structure("article", depth=3)
        parent = result["outline"][1]
        self.assertEqual(parent["children"][0]["children"][0]["unit_id"], "leaf")
        for node in section_nodes(result["outline"]):
            fields = {"unit_id", "kind", "has_children", "children"}
            self.assertEqual(set(node), fields | ({"title"} if node["kind"] == "section" else set()))
        self.assertEqual(result["outline"][0]["kind"], "opening")
        self.assertEqual(parent["kind"], "section")
        self.assertEqual(parent["title"], "父章")

    def test_cli_returns_exact_business_json(self):
        """命令行直接交付业务树，证据目录只保存在内部；无参数或返回值。"""
        output = io.StringIO()
        with patch("sys.argv", ["prototype.py", "structure", "--cache-dir", self.folder.name]), redirect_stdout(output):
            status = main()
        self.assertEqual(status, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(set(result), {"article", "revision", "parent", "outline"})

    def test_children_flag_uses_full_structure_not_visible_children(self):
        """同一父章在浅层被截断时仍标有子章，真实叶子及开篇始终标无子章。"""
        shallow = Prototype(self.folder.name).structure("article")
        self.assertIsNone(shallow["parent"])
        parent = shallow["outline"][1]
        self.assertTrue(parent["has_children"])
        self.assertEqual(parent["children"], [])
        deep = Prototype(self.folder.name).structure("article", depth=3)
        states = {n["unit_id"]: n["has_children"] for n in section_nodes(deep["outline"])}
        self.assertEqual(states, {"@opening": False, "parent": True, "child": True, "leaf": False, "other": False})
        # 叶子中含容器内 H1，但它不是可独立选择的子章节。
        self.assertEqual(deep["outline"][1]["children"][0]["children"][0]["children"], [])
        self.assertEqual(len(self.calls), 1)  # 改变目录深度仍复用同一个完整文章快照。

    def test_parent_metadata_describes_selected_unit(self):
        """局部查询单独说明起点有无子章，起点不混入树、不占展开层数。"""
        for selected, expected in [("parent", True), ("child", True), ("leaf", False), ("@opening", False)]:
            result = Prototype(self.folder.name).structure("article", selected)
            self.assertEqual(result["parent"], {"unit_id": selected, "has_children": expected})
            self.assertNotIn(selected, [n["unit_id"] for n in section_nodes(result["outline"])])
        self.content = '<p id="body">无标题正文</p>'
        result = Prototype(self.folder.name).structure("untitled-article", "@document")
        self.assertEqual(result["parent"], {"unit_id": "@document", "has_children": False})
        self.assertEqual(result["outline"], [])

    def test_depth_is_positive_integer_before_fetch(self):
        """小数、布尔、字符串与非正整数不符合深度规格，应在请求前拒绝。"""
        for depth in [0, -1, 1.5, True, "2"]:
            with self.subTest(depth=depth), self.assertRaisesRegex(ValueError, "正整数"):
                Prototype(self.folder.name).structure("article", depth=depth)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
