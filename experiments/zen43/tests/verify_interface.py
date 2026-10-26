"""跨工具命名衔接检查：真实解析流程使用构造响应，不发网络请求。

验证检索返回的 unit_id 能直接进入目录和正文选择，以及命令行新参数保持完整章节范围。
"""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from experiments.zen43.core.prototype import Prototype
from experiments.zen43.core.source import daily
from experiments.zen43.prototype import main


class InterfaceChecks(unittest.TestCase):
    """检查三工具之间传递同一标识的行为；无额外测试服务。"""

    def setUp(self):
        """准备跳级章节及命中段落，替换 CLI；无参数和返回值。"""
        self.folder = tempfile.TemporaryDirectory(prefix="zen43-interface-")
        self.addCleanup(self.folder.cleanup)
        self.full = ('<p id="intro">开篇</p><h1 id="install">安装</h1>'
                     '<h3 id="a">安装 A</h3><p id="body_a">Python 要求</p>'
                     '<h3 id="b">安装 B</h3><p id="body_b">B 的正文</p>'
                     '<h1 id="next">下一章</h1>')
        self.calls = []
        cli = patch.object(daily, "run_cli", side_effect=self.respond)
        cli.start()
        self.addCleanup(cli.stop)

    def respond(self, arguments):
        """记录 arguments 并按范围返回构造 XML/Markdown；返回原生信封，未模拟业务转换。"""
        options = dict(zip(arguments[2::2], arguments[3::2]))
        self.calls.append(options)
        content = self.full
        if options["--scope"] == "keyword":
            content = '<p id="body_a">Python 要求</p>'
        elif options["--doc-format"] == "markdown":
            content = "选区正文"
        return {"data": {"document": {"revision_id": 1, "content": content}}}

    def cleanup_body(self, result):
        """登记 result 中正文临时目录的清理；无返回值，不删除其他测试或用户文件。"""
        self.addCleanup(shutil.rmtree, Path(result["markdown_path"]).parent)

    def invoke(self, *arguments):
        """以 arguments 调用实际命令行入口；返回解析后的成功 JSON，失败立即断言。"""
        output = io.StringIO()
        with patch("sys.argv", ["prototype.py", *arguments, "--cache-dir", self.folder.name]), redirect_stdout(output):
            self.assertEqual(main(), 0)
        return json.loads(output.getvalue())

    def test_match_to_outline_to_selected_body(self):
        """命中树中的父章 ID 取目录，再把子章 ID 原样传给正文选择；范围仍包含完整子章。"""
        context = Prototype(self.folder.name)
        located = context.locate("article", ["Python"])
        parent_id = located["match_tree"][0]["unit_id"]
        directory = context.structure("article", parent_id=parent_id)
        unit_id = directory["outline"][0]["unit_id"]
        self.assertEqual(directory["parent"], {"unit_id": parent_id, "has_children": True})
        self.assertEqual(unit_id, located["match_tree"][0]["children"][0]["unit_id"])
        result = context.read("article", unit_ids=[unit_id])
        self.cleanup_body(result)
        self.assertNotIn("actual_ranges", result)
        self.assertEqual(context.diagnostics["body_ranges"][0]["unit_ids"], ["a"])
        self.assertNotIn("images", result)
        self.assertEqual([c["--scope"] for c in self.calls], ["keyword", "full", "range"])

    def test_cli_parent_id_and_unit_ids(self):
        """命令行 --parent-id 与 --unit-ids 可串联，正文输入 ID 数组并按原文读取。"""
        directory = self.invoke("structure", "--parent-id", "install")
        selected = [n["unit_id"] for n in directory["outline"]]
        result = self.invoke("read", "--unit-ids", *selected)
        self.assertNotIn("evidence_dir", result)
        self.cleanup_body(result)
        self.assertNotIn("actual_ranges", result)
        self.assertEqual(self.calls[-1]["--start-block-id"], "a")
        self.assertEqual(self.calls[-1]["--end-block-id"], "body_b")

    def test_cli_range_names_keep_inclusive_end(self):
        """命令行范围新名称仍扩展到终章正文末尾；不误读成原生块端点。"""
        result = self.invoke("read", "--start-unit-id", "a", "--end-unit-id", "b")
        self.cleanup_body(result)
        self.assertNotIn("actual_ranges", result)
        self.assertEqual(self.calls[-1]["--start-block-id"], "a")
        self.assertEqual(self.calls[-1]["--end-block-id"], "body_b")

    def test_cli_failure_does_not_expose_backend_or_evidence_path(self):
        """来源失败只交付产品层错误；底层 CLI、块标识与证据目录留在内部。"""
        output = io.StringIO()
        with patch.object(daily, "run_cli", side_effect=ValueError("CLI_FAILED: block_id invalid")), patch(
            "sys.argv", ["prototype.py", "read", "--cache-dir", self.folder.name]), redirect_stdout(output):
            self.assertEqual(main(), 2)
        result = json.loads(output.getvalue())
        self.assertNotIn("evidence_dir", result)
        self.assertNotIn("CLI", result["error"])
        self.assertNotIn("block_id", result["error"])


if __name__ == "__main__":
    unittest.main()
