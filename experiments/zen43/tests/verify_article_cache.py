"""单篇文章缓存行为检查：执行实际三个工具，替换外部 CLI，不发网络请求。"""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments.zen43.core.prototype import Prototype
from experiments.zen43.core.source import daily
from experiments.zen43.demos.body_demo import review_body


class ArticleCacheChecks(unittest.TestCase):
    """验证跨工具、跨实例复用，七天到期、修订切换及图片临时文件失效。"""

    def setUp(self):
        """准备一篇含章节、正文、图片的文章与独立缓存；无输入和返回值。"""
        self.folder = tempfile.TemporaryDirectory(prefix="zen43-article-cache-", dir="/tmp")
        self.addCleanup(self.folder.cleanup)
        self.calls = []
        self.revision = 1
        self.now = 1000000
        self.full = '<h1 id="chapter">章节</h1><p id="body">关键词正文</p><img id="image" src="image-token"/>'
        cli = patch.object(daily, "run_cli", side_effect=self.respond)
        cli.start()
        self.addCleanup(cli.stop)
        clock = patch("time.time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        original = tempfile.mkdtemp

        def body_folder(*args, **kwargs):
            """将测试正文目录放在统一临时根，测试退出时一起清理；返回新目录路径。"""
            if kwargs.get("prefix") == "zen43-body-":
                kwargs["dir"] = self.folder.name
            return original(*args, **kwargs)

        folder_patch = patch("experiments.zen43.core.prototype.tempfile.mkdtemp", side_effect=body_folder)
        folder_patch.start()
        self.addCleanup(folder_patch.stop)

    def respond(self, arguments):
        """记录 arguments，返回当前修订原生响应；下载图片到测试目录，返回原生信封。"""
        self.calls.append(arguments)
        options = dict(zip(arguments[2::2], arguments[3::2]))
        if arguments[:2] == ["docs", "+media-preview"]:
            path = (Path(self.folder.name) / "downloaded.png").resolve()
            path.write_bytes(b"image")
            return {"data": {"saved_path": str(path), "content_type": "image/png", "size_bytes": 5}}
        content = self.full.replace("章节", f"章节 {self.revision}")
        if options["--scope"] == "keyword":
            content = f'<p id="body">关键词正文 {self.revision}</p>'
        elif options["--doc-format"] == "markdown":
            content = f'# 章节 {self.revision}\n关键词正文\n![](https://feishu.cn/file/image-token)'
        return {"data": {"document": {"revision_id": self.revision, "content": content}}}

    def context(self, profile=None):
        """输入可选认证配置，返回使用同一磁盘缓存的独立工具上下文。"""
        return Prototype(Path(self.folder.name) / "cache", profile)

    def test_locate_structure_read_chain_reuses_fetched_content(self):
        """定位后的结构查询零请求，首次正文仅取 Markdown 和图片，再读同章全部复用。"""
        first = self.context()
        first.locate("article", ["关键词"])
        self.assertEqual(len(self.calls), 2)
        directory = self.context().structure("article")
        self.assertEqual(directory["outline"][0]["unit_id"], "chapter")
        self.assertEqual(len(self.calls), 2)
        body = self.context().read("article", unit_ids=["chapter"])
        self.assertEqual(len(self.calls), 4)
        again_context = self.context()
        again = again_context.read("article", unit_ids=["chapter"])
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(Path(body["markdown_path"]).read_text(), Path(again["markdown_path"]).read_text())
        self.assertEqual({key: value for key, value in body.items() if key != "markdown_path"},
                         {key: value for key, value in again.items() if key != "markdown_path"})
        self.assertEqual(again_context.calls, [])

    def test_seven_day_expiry_refetches_only_when_needed(self):
        """七天内不预查修订号；满七天后用到才重新请求，刷新后继续复用。"""
        self.context().structure("article")
        self.now += 7 * 24 * 60 * 60 - 1
        self.context().structure("article")
        self.assertEqual(len(self.calls), 1)
        self.now += 1
        self.revision = 2
        result = self.context().structure("article")
        self.assertEqual(result["outline"][0]["title"], "章节 2")
        self.assertEqual(len(self.calls), 2)
        self.context().structure("article")
        self.assertEqual(len(self.calls), 2)

    def test_new_representation_does_not_extend_snapshot_lifetime(self):
        """临近到期时取得另一种格式，不延长旧 XML 的有效期。"""
        self.context().structure("article")
        self.now += 7 * 24 * 60 * 60 - 1
        self.context().read("article", unit_ids=["chapter"])
        self.now += 1
        self.context().structure("article")
        fetches = [call for call in self.calls if call[:2] == ["docs", "+fetch"]]
        self.assertEqual(len(fetches), 3)

    def test_new_revision_invalidates_old_full_before_next_tool(self):
        """新关键词请求观察到更新时，丢弃旧修订全文，随后结构重读且不混合版本。"""
        self.context().structure("article")
        self.revision = 2
        located = self.context().locate("article", ["新关键词"])
        self.assertIn("关键词正文 2", located["match_tree"][0]["hits"][0]["snippet_xml"])
        self.assertEqual(len(self.calls), 3)
        result = self.context().structure("article")
        self.assertEqual(result["outline"][0]["title"], "章节 2")
        self.assertEqual(len(self.calls), 3)

    def test_url_token_aliases_share_cache_but_profiles_do_not(self):
        """Wiki URL 去查询参数后与 token 共用内容，不同认证配置分别取得。"""
        self.context().structure("https://waytoagi.feishu.cn/wiki/article?from=test")
        self.context().structure("article")
        self.assertEqual(len(self.calls), 1)
        self.context("other").structure("article")
        self.assertEqual(len(self.calls), 2)

    def test_missing_local_image_is_downloaded_again(self):
        """缓存正文仍有效但图片文件已清理时，只重新下载图片，不重读 XML 或 Markdown。"""
        first = self.context()
        first.read("article", unit_ids=["chapter"])
        Path(first.diagnostics["body_images"]["image-token"]["saved_path"]).unlink()
        self.context().read("article", unit_ids=["chapter"])
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.calls[-1][:2], ["docs", "+media-preview"])

    def test_read_refreshes_old_structure_with_new_markdown(self):
        """补 Markdown 发现更新后，同一次调用自动重取结构并交付新正文。"""
        self.context().structure("article")
        self.revision = 2
        result = self.context().read("article", unit_ids=["chapter"])
        self.assertIn("# 章节 2", Path(result["markdown_path"]).read_text())
        self.assertEqual(len(self.calls), 4)

    def test_read_refresh_recomputes_range_endpoints(self):
        """结构缓存过期于来源更新时，重算新块端点，不复用旧范围 Markdown。"""
        self.context().structure("article")
        self.full = '<h1 id="chapter">章节</h1><p id="new-end">新增末尾</p>'
        self.revision = 2
        result = self.context().read("article", unit_ids=["chapter"])
        options = dict(zip(self.calls[-1][2::2], self.calls[-1][3::2]))
        self.assertEqual(options["--end-block-id"], "new-end")
        self.assertIn("# 章节 2", Path(result["markdown_path"]).read_text())

    def test_read_refresh_discards_earlier_selection_of_old_revision(self):
        """第二个不连续选区更新后，先前选区也重取；交付文件不得残留旧版内容。"""
        self.full = '<h1 id="a">甲</h1><h1 id="b">乙</h1><h1 id="c">丙</h1>'
        def response(arguments):
            """读到第二个选区时将来源升到版本二，返回带版本文字的选区正文。"""
            options = dict(zip(arguments[2::2], arguments[3::2]))
            if options.get("--start-block-id") == "c":
                self.revision = 2
            result = self.respond(arguments)
            if options.get("--doc-format") == "markdown":
                result["data"]["document"]["content"] = f'{options["--start-block-id"]} 版本 {self.revision}'
            return result

        with patch.object(daily, "run_cli", side_effect=response):
            result = self.context().read("article", unit_ids=["a", "c"])
        content = Path(result["markdown_path"]).read_text()
        self.assertIn("a 版本 2", content)
        self.assertIn("c 版本 2", content)
        self.assertNotIn("版本 1", content)

    def test_continuous_revision_changes_stop_without_partial_body(self):
        """每次来源请求都更新时仅重试一次，不生成正文或下载图片。"""
        def response(arguments):
            """每次外部请求推进版本后返回文档；模拟持续编辑。"""
            self.revision += 1
            return self.respond(arguments)

        with patch.object(daily, "run_cli", side_effect=response):
            with self.assertRaisesRegex(ValueError, "自动刷新后仍"):
                self.context().read("article", unit_ids=["chapter"])
        self.assertEqual(len(self.calls), 4)
        self.assertTrue(all(call[:2] == ["docs", "+fetch"] for call in self.calls))
        self.assertEqual(list(Path(self.folder.name).rglob("body.md")), [])

    def test_body_demo_verifies_labeled_markdown_using_internal_revision(self):
        """演示核对新正文标记和内部修订，业务返回不再提供修订号也能完成检查。"""
        context = self.context()
        arguments = {"article": "article", "unit_ids": ["chapter"]}
        result = context.read(**arguments)
        review = review_body(result, context, arguments)
        self.assertTrue(review["markdown"].startswith('> unit_ids: ["chapter"]\n\n'))
        self.assertEqual(len(review["images"]), 1)


if __name__ == "__main__":
    unittest.main()
