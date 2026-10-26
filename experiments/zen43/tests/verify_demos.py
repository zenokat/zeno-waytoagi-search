"""演示运行器的离线回归检查：验证真实工具与共享缓存能完成整轮演示。

仅替换外部 CLI 响应；生成新的临时 HTML，不覆盖已保存的真实证据。
"""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments.zen43.core.source import daily
from experiments.zen43.demos.structure_demo import NO_HEADINGS, run_demo


class DemoChecks(unittest.TestCase):
    """覆盖冷缓存及暖缓存的八个结构场景，防止把缓存零请求误判为失败。"""

    def respond(self, arguments):
        """按 arguments 返回最小章节样本；返回原生信封，不模拟工具内部结构计算。"""
        options = dict(zip(arguments[2::2], arguments[3::2]))
        section = '<h1 id="parent">动手安装吧</h1><h3 id="a">安装 A</h3><p id="pa">A</p><h3 id="b">安装 B</h3><p id="pb">B</p>'
        content = '<p id="intro">开篇</p>' + section
        if options["--doc"] == NO_HEADINGS:
            content = "" if options["--scope"] == "outline" else '<p id="body">无标题正文</p>'
        elif options["--scope"] == "outline":
            content = '<h1 id="parent">动手安装吧</h1>'
            if int(options["--max-depth"]) >= 3:
                content += '<h3 id="a">安装 A</h3><h3 id="b">安装 B</h3>'
        elif options["--scope"] == "section":
            content = section
        return {"data": {"document": {"revision_id": 1, "content": content}}}

    def test_structure_demo_accepts_cold_and_warm_article_cache(self):
        """完整运行两轮结构演示；首轮两篇各读取一次，第二轮复用，其他工具证据逐项保持。"""
        with tempfile.TemporaryDirectory(prefix="zen43-demo-check-") as folder:
            output = Path(folder)
            original = {"name": "仅局部正文命中", "input": {}, "total_seconds": 0, "native": [], "output": {"revision": 1, "match_tree": [
                {"unit_id": "parent", "children": [{"unit_id": "a", "children": []}]}]}}
            base = output / "base.html"
            base.write_text("const evidence=" + json.dumps({"scenarios": [original], "checks": {"验证时间": "原始时间"}}))
            with patch.object(daily, "run_cli", side_effect=self.respond), redirect_stdout(io.StringIO()):
                first = run_demo(output / "run", base)
                cold = json.loads((output / "run/evidence.json").read_text())
                run_demo(output / "run", first["html"])
                warm = json.loads((output / "run/evidence.json").read_text())
            self.assertEqual(cold["scenarios"][0], original)
            self.assertEqual(warm["scenarios"][0], original)
            self.assertEqual([len(s["native"]) for s in cold["scenarios"][1:]], [1, 0, 0, 0, 0, 0, 1, 0])
            self.assertTrue(all(not s["native"] for s in warm["scenarios"][1:]))
            self.assertEqual([s["output"] for s in cold["scenarios"]], [s["output"] for s in warm["scenarios"]])


if __name__ == "__main__":
    unittest.main()
