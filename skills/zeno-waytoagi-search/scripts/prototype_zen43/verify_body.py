"""正文图片引用回归检查：构造原生响应，验证 Markdown 与容器 HTML 两种表示。

测试实际 read 流程，图片仅写测试临时目录；不发网络请求。
"""

from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from prototype import Prototype, daily


class BodyImageChecks(unittest.TestCase):
    """核对容器内图片和普通图片共享下载结果，并在原引用处交付本地路径或失败标记。"""

    def setUp(self):
        """准备同时含分栏图片和普通图片的最小原生响应；无输入和返回值。"""
        self.folder = tempfile.TemporaryDirectory(prefix="zen43-body-check-", dir="/tmp")
        self.addCleanup(self.folder.cleanup)
        self.failed = False
        self.downloads = 0
        self.full = ('<h1 id="chapter">章节</h1><grid id="grid"><column id="column">'
                     '<img id="image" src="image-token"/></column></grid>')
        self.markdown = ('# 章节\n<grid><column><img alt="原图片" src="image-token"/></column></grid>\n'
                         '![](https://feishu.cn/file/image-token)\n正文提到 image-token。')
        cli = patch.object(daily, "run_cli", side_effect=self.respond)
        cli.start()
        self.addCleanup(cli.stop)

    def respond(self, arguments):
        """按 arguments 返回构造响应或模拟下载失败；成功下载写本地文件，返回原生信封。"""
        options = dict(zip(arguments[2::2], arguments[3::2]))
        if arguments[:2] == ["docs", "+media-preview"]:
            self.downloads += 1
            if self.failed:
                raise ValueError("测试图片暂不可读取")
            path = Path(options["--output"] + ".png")
            path.write_bytes(b"image fixture")
            return {"data": {"saved_path": str(path), "content_type": "image/png", "size_bytes": 13}}
        content = self.markdown if options["--doc-format"] == "markdown" else self.full
        return {"data": {"document": {"revision_id": 1, "content": content}}}

    def read_body(self):
        """调用真实正文读取流程并登记临时文件清理；返回业务结果与文件原文。"""
        result = Prototype(self.folder.name).read("article", unit_ids=["chapter"])
        self.addCleanup(shutil.rmtree, Path(result["markdown_path"]).parent)
        return result, Path(result["markdown_path"]).read_text()

    def test_container_image_and_markdown_image_share_local_file(self):
        """相同图片的 HTML src 与 Markdown URL 都指向本地文件，正文提到的 token 保持不变。"""
        result, body = self.read_body()
        saved = result["images"]["image-token"]["saved_path"]
        self.assertTrue(Path(saved).is_file())
        self.assertIn('src="' + saved + '"', body)
        self.assertIn("![](" + saved + ")", body)
        self.assertIn("正文提到 image-token。", body)
        self.assertEqual(self.downloads, 1)
        self.assertEqual(result["warnings"], [])

    def test_container_image_failure_is_marked_in_place(self):
        """下载失败时两种引用在原位置明确标记，并保留警告，不返回无法读取的原生 src。"""
        self.failed = True
        result, body = self.read_body()
        self.assertIn('src="图片获取失败:image-token"', body)
        self.assertIn("![](图片获取失败:image-token)", body)
        self.assertNotIn('src="image-token"', body)
        self.assertEqual(result["images"]["image-token"]["status"], "failed")
        self.assertEqual(len(result["warnings"]), 1)


if __name__ == "__main__":
    unittest.main()
