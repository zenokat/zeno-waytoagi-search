"""正文与命中片段的表达转换：优先 Markdown，保留来源内容和图片引用。

此模块只转换已取得的内容；样张命令独立放在 demos/container_preview.py。
当前覆盖已验证样本，不承诺所有嵌套格式无损转换。
"""

import re
from xml.etree import ElementTree as ET

from .source import daily, xml_root


def localize_image_reference(content, token, replacement):
    """将 content 中 token 对应图片引用改为 replacement 本地路径或失败标记；返回新正文，不改其他文字。"""
    content = content.replace("https://feishu.cn/file/" + token, replacement)
    # 容器里的图片仍是 HTML img，src 为原生 token；定点改属性以保留混合 Markdown 的其余内容。
    pattern = r'(<img\b[^>]*?\ssrc=)(["\x27])' + re.escape(token) + r'\2'
    return re.sub(pattern, lambda match: match[1] + match[2] + replacement + match[2], content)


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
    root = xml_root(content)
    for element in root.iter():
        if element.tag == "cite":
            title, links = daily.inline_content(element)
            element.tag, element.text = "a", title
            element.attrib.clear()
            element.set("href", links[0]["url"])
        elif element.tag == "img" and images is not None:
            source = element.get("src")
            saved = images.get(source, {}).get("saved_path")
            # 正文在转换前已本地化 src；HTML 回退只接受本轮图片映射中的已下载路径。
            if not saved:
                saved = next((image["saved_path"] for image in images.values()
                              if image.get("saved_path") and image["saved_path"] == source), None)
            element.attrib = {"alt": element.get("alt", "")}
            if saved:
                element.set("src", saved)
            elif source == "图片获取失败":
                element.tag, element.text = "span", "[图片获取失败]"
                element.attrib.clear()
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
