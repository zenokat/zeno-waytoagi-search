"""容器阅读形式对照样张：将已取得的真实 XML 选区转成按行字段、分栏顺序展开的 Markdown。

仅覆盖当前表格样本出现的结构，不接入默认正文工具，不承诺通用无损转换。
未知结构直接报错；保留原始证据，便于与样张核对。
"""

import argparse
import json
from pathlib import Path
import re

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
    return text


def render_table(element, images):
    """将 element 表格按数据行展开为带原列名的字段；images 供单元格图片引用，返回 Markdown。"""
    rows = list(element.iter("tr"))
    headers = [inline_markdown(cell) for cell in rows[0]]
    parts = [f"[原表格：{len(rows)} 行、{len(headers)} 列；以下按数据行展开]"]
    for index, row in enumerate(rows[1:], 1):
        if len(row) != len(headers):
            raise ValueError("当前对照样张不支持合并单元格，不能猜测列对应关系")
        parts.append(f"[数据行 {index}]")
        for header, cell in zip(headers, row):
            parts.append("**" + header + "**")
            # 单元格可能含多段引用，逐段保留，不把列表压成连在一起的一句话。
            for child in cell:
                rendered = render_block(child, images)
                if rendered:
                    parts.append(rendered)
    return "\n\n".join(parts)


def render_block(element, images):
    """输入当前样本 XML element 与 token 到图片元数据映射，返回可读 Markdown；未知结构明确拒绝。"""
    tag = element.tag
    if tag == "table":
        return render_table(element, images)
    if tag == "grid":
        columns = []
        for index, column in enumerate(element, 1):
            contents = [render_block(child, images) for child in column]
            columns.append(f"[分栏：第 {index} 列]\n\n" + "\n\n".join(part for part in contents if part))
        return "\n\n".join(columns)
    if tag == "img":
        saved = images[element.attrib["src"]]["saved_path"]
        return "![" + element.get("alt", "") + "](" + saved + ")"
    if tag == "sub-page-list":
        return "[子页面引用：仅保留名称、资源类型与标识，未读取页面正文]\n\n" + "\n".join(
            "- " + child.get("title", "") + "（" + child.get("file-type", "") + "；" + child.get("doc-id", "") + "）"
            for child in element)
    if tag in {"ul", "ol"}:
        return "\n".join((f"{index}. " if tag == "ol" else "- ") + inline_markdown(child)
                         for index, child in enumerate(element, 1))
    if re.fullmatch(r"h[1-6]", tag):
        return "#" * int(tag[1]) + " " + inline_markdown(element)
    if tag in {"p", "li"}:
        return inline_markdown(element)
    if tag == "hr":
        return "---"
    raise ValueError("当前对照样张未覆盖结构：" + tag)


def build_preview(xml_path, result_path, output):
    """输入原生完整 XML 响应、业务结果 JSON 与输出路径；按实际选区生成样张，返回绝对路径。"""
    source = json.loads(Path(xml_path).read_text())["data"]["document"]
    result = json.loads(Path(result_path).read_text())
    if source["revision_id"] != result["revision"] or result["warnings"]:
        raise ValueError("来源修订或素材状态不适合生成本次对照")
    blocks, _, positions = section_model(source["content"])
    parts = ["> 阅读形式对照样张：由同修订已取得的 XML 本地转换，尚未替换正文工具的默认输出。",
             "> 来源：" + result["article"]]
    for actual in result["actual_ranges"]:
        left, right = positions[actual["start_block_id"]], positions[actual["end_block_id"]] + 1
        for block in blocks[left:right]:
            rendered = render_block(block, result["images"])
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
    args = parser.parse_args()
    print(build_preview(args.xml, args.result, args.output))


if __name__ == "__main__":
    main()
