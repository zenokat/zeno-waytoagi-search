"""正文与命中片段的产品层表达检查：Markdown 容器优先，内部块标识不交付调用方。"""

import unittest
from xml.etree import ElementTree as ET

from experiments.zen43.core.markdown import render_block, render_table, format_markdown, public_snippet


class MarkdownChecks(unittest.TestCase):
    """核对表格、引用、代码和图片使用 Markdown，并保留内容对应关系。"""

    def test_table_uses_markdown_columns_and_escapes_pipes(self):
        """表格保留两行两列，多段单元格用换行，文字管道符转义，保留引用标题和链接。"""
        table = ET.fromstring('<table><tr><td>类别</td><td>资料</td></tr><tr><td>A|B</td>'
                              '<td><p>第一段</p><p><cite title="资料名称" doc-id="target" file-type="wiki"/></p></td></tr></table>')
        text = render_table(table, {})
        self.assertTrue(text.startswith("| 类别 | 资料 |\n| --- | --- |"))
        self.assertIn(r"A\|B", text)
        self.assertIn("<br>", text)
        self.assertIn("[资料名称](https://waytoagi.feishu.cn/wiki/target)", text)
        self.assertNotIn("[数据行", text)

    def test_callout_uses_markdown_quote(self):
        """提示容器使用已有的引用语法，不泄漏容器 ID 或样式参数。"""
        node = ET.fromstring('<callout id="internal"><p>第一段</p><p>第二段</p></callout>')
        text = render_block(node, {})
        self.assertEqual(text, "> 第一段\n> \n> 第二段")

    def test_code_uses_fence_and_keeps_literal_markup(self):
        """代码块中的标签、换行与管道符原样保留，不作为正文容器再次解析。"""
        node = ET.fromstring('<pre lang="python"><code>print("&lt;table&gt;|&lt;/table&gt;")\nnext()</code></pre>')
        self.assertEqual(render_block(node, {}), '```python\nprint("<table>|</table>")\nnext()\n```')

    def test_body_hides_range_metadata_but_keeps_code_literal(self):
        """正文移除内部范围包装；代码示例中的相同标签仍按原文保留。"""
        content = '<fragment requested-start="hidden">\n正文\n```xml\n<table id="example"/>\n```\n</fragment>'
        warnings = []
        rendered = format_markdown(content, {}, warnings)
        self.assertNotIn("requested-start", rendered)
        self.assertIn('<table id="example"/>', rendered)
        self.assertEqual(warnings, [])

    def test_snippet_has_content_and_unit_id_is_only_external_locator(self):
        """片段保留结构、引用名称与来源链接，不包含块 ID、文档 token 或范围属性。"""
        source = '<fragment mode="range" requested-start="hidden"><table id="hidden"><tr><td><cite doc-id="target" file-type="wiki" title="资料"/></td></tr></table></fragment>'
        rendered = public_snippet(source)
        self.assertIn('<a href="https://waytoagi.feishu.cn/wiki/target">资料</a>', rendered)
        for attribute in ["id=", "doc-id=", "file-type=", "requested-start=", "mode="]:
            self.assertNotIn(attribute, rendered)


if __name__ == "__main__":
    unittest.main()
