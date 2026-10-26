"""单篇文章缓存行为检查：执行实际三个工具，替换外部 CLI，不发网络请求。"""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments.zen43.core.prototype import Prototype
from experiments.zen43.core.source import daily


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
            path = Path(self.folder.name) / "downloaded.png"
            path.write_bytes(b"image")
            return {"data": {"saved_path": str(path), "content_type": "image/png", "size_bytes": 5}}
        content = self.full
        if options["--scope"] == "keyword":
            content = '<p id="body">关键词正文</p>'
        elif options["--doc-format"] == "markdown":
            content = '# 章节\n关键词正文\n![](https://feishu.cn/file/image-token)'
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
        self.assertEqual(result["revision"], 2)
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
        self.assertEqual(located["revision"], 2)
        self.assertEqual(len(self.calls), 3)
        result = self.context().structure("article")
        self.assertEqual(result["revision"], 2)
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

    def test_read_rejects_old_structure_with_new_markdown(self):
        """首次补 Markdown 发现修订变化时明确失败；再次调用取得同修订结构后才成功。"""
        self.context().structure("article")
        self.revision = 2
        with self.assertRaisesRegex(ValueError, "修订不一致"):
            self.context().read("article", unit_ids=["chapter"])
        result = self.context().read("article", unit_ids=["chapter"])
        self.assertEqual(result["revision"], 2)
        self.assertEqual(len(self.calls), 4)


if __name__ == "__main__":
    unittest.main()
