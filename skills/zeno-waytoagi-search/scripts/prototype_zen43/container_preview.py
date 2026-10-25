"""原型正文表达：优先将已取得的容器转成 Markdown，并生成可独立审阅的样张。

覆盖当前文章已观测的结构，供默认正文工具处理混合 Markdown；不承诺通用无损转换。
保留原始证据；未知结构由正文入口在原位置标记并报告警告。
"""

import argparse
import json
from pathlib import Path
import re
from xml.etree import ElementTree as ET

from prototype import daily, section_model


def inline_markdown(element):
    """输入当前样本的行内 XML element，返回带可见引用名称与链接的 Markdown，不读取链接内容。"""
    if element.tag == "cite":
        title, links = daily.inline_content(element)
        return "[" + title + "](" + links[0]["url"] + ")"
    if element.tag == "br":
        return "\n"
    text = element.text or ""
    for child in element:
        text += inline_markdown(child) + (child.tail or "")
    if element.tag == "a":
        return "[" + text + "](" + element.attrib["href"] + ")"
    if element.tag in {"b", "strong"}:
        return "**" + text + "**"
    if element.tag in {"i", "em"}:
        return "*" + text + "*"
    if element.tag in {"s", "del"}:
        return "~~" + text + "~~"
    if element.tag == "code":
        return "`" + text + "`"
    return text


def render_table(element, images):
    """将 element 表格转成 GFM 表格；images 用于单元格图片，返回保留行列关系且转义管道符的文本。"""
    rows = list(element.iter("tr"))
    if not rows:
        return ""
    width = len(rows[0])
    if any(len(row) != width for row in rows) or any(
        cell.get("rowspan", "1") != "1" or cell.get("colspan", "1") != "1" for row in rows for cell in row
    ):
        # GFM 没有合并单元格语法；保留语义 HTML，但剥除内部定位标识，不猜行列关系。
        return public_snippet(ET.tostring(element, encoding="unicode"), images)
    lines = []
    for index, row in enumerate(rows):
        cells = []
        for cell in row:
            parts = [cell.text or ""]
            for child in cell:
                parts.append(inline_markdown(child) if re.fullmatch(r"h[1-6]", child.tag) else render_block(child, images))
                if child.tail:
                    parts.append(child.tail)
            text = "\n".join(part for part in parts if part).strip().replace("\n", "<br>")
            cells.append(re.sub(r"(?<!\\)\|", r"\\|", text))
        lines.append("| " + " | ".join(cells) + " |")
        if index == 0:
            lines.append("| " + " | ".join(["---"] * width) + " |")
    return "\n".join(lines)


def render_block(element, images):
    """输入当前样本 XML element 与 token 到图片元数据映射，返回可读 Markdown；未知结构明确拒绝。"""
    tag = element.tag
    if tag == "table":
        return render_table(element, images)
    if tag == "grid":
        columns = []
        for index, column in enumerate(element, 1):
            contents = [render_block(child, images) for child in column]
            columns.append(f"**第 {index} 栏**\n\n" + "\n\n".join(part for part in contents if part))
        return "\n\n".join(columns)
    if tag == "img":
        saved = images.get(element.get("src"), {}).get("saved_path") or element.get("src", "")
        if saved == "图片获取失败":
            return "[图片获取失败]"
        return "![" + element.get("alt", "") + "](" + saved + ")"
    if tag == "sub-page-list":
        paths = {"docx": "docx", "wiki": "wiki", "bitable": "base", "sheets": "sheets"}
        return "**子页面引用（未读取正文）**\n\n" + "\n".join(
            "- [" + child.get("title", "") + "](https://waytoagi.feishu.cn/" + paths[child.get("file-type")] + "/" + child.get("doc-id", "") + ")"
            if child.get("file-type") in paths else "- " + child.get("title", "") for child in element)
    if tag in {"callout", "blockquote"}:
        text = "\n\n".join(render_block(child, images) for child in element)
        return "\n".join("> " + line for line in text.split("\n"))
    if tag == "pre":
        code = "".join(element.itertext()).rstrip("\n")
        fence = "`" * max(3, max((len(match[0]) + 1 for match in re.finditer(r"`+", code)), default=3))
        return fence + element.get("lang", "") + "\n" + code + "\n" + fence
    if tag in {"ul", "ol"}:
        return "\n".join((f"{index}. " if tag == "ol" else "- ") + inline_markdown(child)
                         for index, child in enumerate(element, 1))
    if re.fullmatch(r"h[1-6]", tag):
        return "#" * int(tag[1]) + " " + inline_markdown(element)
    if tag in {"p", "li", "a", "cite", "b", "strong", "em", "i", "code", "span", "s", "del"}:
        return inline_markdown(element)
    if tag == "br":
        return "\n"
    if tag == "hr":
        return "---"
    raise ValueError("当前对照样张未覆盖结构：" + tag)


def public_snippet(content, images=None):
    """输入原生 XML 片段，返回去除内部标识的内容 XML；引用用可见名称与链接，读取入口仍是外层 unit_id。"""
    from prototype import xml_root
    root = xml_root(content)
    for element in root.iter():
        if element.tag == "cite":
            title, links = daily.inline_content(element)
            element.tag, element.text = "a", title
            element.attrib.clear()
            element.set("href", links[0]["url"])
        elif element.tag == "img" and images is not None:
            saved = images.get(element.get("src"), {}).get("saved_path")
            element.attrib = {"alt": element.get("alt", "")}
            if saved:
                element.set("src", saved)
        else:
            element.attrib = {key: value for key, value in element.attrib.items() if key in {"href", "alt", "rowspan", "colspan"}}
    return "".join(ET.tostring(element, encoding="unicode") for element in root)


def format_markdown(content, images, warnings):
    """输入原生混合 Markdown、图片映射和 warnings 列表，返回标准语法优先的正文；代码围栏原样保留。"""
    def convert(text):
        """转换非代码 text 内的容器及范围包装；返回 Markdown，未覆盖内容明确标记并追加警告。"""
        text = re.sub(r"(?m)^</?fragment\b[^>]*>\s*\n?", "", text)
        text = re.sub(r"<title\b[^>]*>(.*?)</title>", lambda match: "**" + match[1] + "**", text, flags=re.S)
        text = re.sub(r"!\[[^\]]*\]\(图片获取失败\)", "[图片获取失败]", text)
        pattern = r"<(?P<tag>[a-z][\w-]*)\b[^>]*>.*?</(?P=tag)>|<[a-z][\w-]*\b[^>]*/>"

        def replace(match):
            """将匹配到的独立 XML 容器转成 Markdown；返回转换内容或明确的未支持提示。"""
            if match[0] in {"<br>", "<br/>", "<br />"}:
                return match[0]
            try:
                return render_block(ET.fromstring(match[0]), images)
            except (ValueError, KeyError, ET.ParseError):
                warnings.append("部分嵌入内容暂无法转换，已在正文原位置标记。")
                return "[此处嵌入内容暂无法读取]"

        return re.sub(pattern, replace, text, flags=re.S)

    output, buffered, fence = [], [], None
    for line in content.splitlines(keepends=True):
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker and fence is None:
            output.append(convert("".join(buffered)))
            buffered = []
            fence = marker[1]
            output.append(line)
        elif fence is not None:
            output.append(line)
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                fence = None
        else:
            buffered.append(line)
    output.append(convert("".join(buffered)))
    return "".join(output)


def build_preview(xml_path, result_path, output, diagnostics_path=None):
    """输入原生响应、业务结果、output 和可选内部诊断；按选区生成样张，返回绝对路径，兼容旧证据。"""
    source = json.loads(Path(xml_path).read_text())["data"]["document"]
    result = json.loads(Path(result_path).read_text())
    diagnostics = json.loads(Path(diagnostics_path).read_text()) if diagnostics_path else result
    if source["revision_id"] != result["revision"] or result["warnings"]:
        raise ValueError("来源修订或素材状态不适合生成本次对照")
    blocks, _, positions = section_model(source["content"])
    parts = ["> 阅读形式对照样张：由同修订已取得的 XML 本地转换，采用正文工具相同的 Markdown 优先规则。",
             "> 来源：" + result["article"]]
    for actual in diagnostics.get("body_ranges", result["actual_ranges"]):
        left, right = positions[actual["start_block_id"]], positions[actual["end_block_id"]] + 1
        for block in blocks[left:right]:
            rendered = render_block(block, diagnostics.get("body_images", result.get("images", {})))
            if rendered:
                parts.append(rendered)
    path = Path(output).resolve()
    path.write_text("\n\n".join(parts) + "\n", encoding="utf-8")
    return str(path)


def main():
    """解析本地证据与输出路径，打印样张位置；不发起网络调用，无返回值。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xml", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--diagnostics")
    args = parser.parse_args()
    print(build_preview(args.xml, args.result, args.output, args.diagnostics))


if __name__ == "__main__":
    main()
