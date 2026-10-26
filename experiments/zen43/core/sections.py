"""由来源 XML 建立章节、读取单位及命中归属；不请求网络或读取缓存。"""

import copy
import re
from xml.etree import ElementTree as ET

from .source import daily, xml_root


def top_blocks(root):
    """从 root 还原文档顶层块序列；展开标题包装与列表包装，保留表格等完整容器。"""
    blocks = []
    for element in root:
        if element.tag in {"ul", "ol"}:
            blocks.extend(top_blocks(element))
        elif re.fullmatch(r"h[1-6]", element.tag):
            heading = copy.deepcopy(element)
            for child in list(heading):
                if child.tag not in daily.INLINE_TAGS:
                    heading.remove(child)
            blocks.append(heading)
            wrapper = ET.Element("root")
            wrapper.extend(child for child in element if child.tag not in daily.INLINE_TAGS)
            blocks.extend(top_blocks(wrapper))
        elif element.tag != "title":
            blocks.append(element)
    return blocks


def section_model(content):
    """由完整 content 建立顶层块、实际章节树和块归属；返回 blocks、units、positions 三元组。"""
    blocks = top_blocks(xml_root(content))
    units, stack, positions = [], [], {}
    for index, block in enumerate(blocks):
        for nested in block.iter():
            if nested.get("id"):
                positions[nested.get("id")] = index
        if not re.fullmatch(r"h[1-6]", block.tag):
            continue
        rank = int(block.tag[1:])
        while stack and stack[-1]["rank"] >= rank:
            stack.pop()["end"] = index
        unit = {"id": block.get("id"), "title": daily.inline_content(block)[0],
                "rank": rank, "depth": len(stack) + 1, "start": index, "end": len(blocks),
                "parents": [ancestor["id"] for ancestor in stack], "kind": "section"}
        units.append(unit)
        stack.append(unit)
    first = units[0]["start"] if units else len(blocks)
    if first:
        # 无标题单位只标类型，不生成可能被误当作原文标题的文字。
        units.insert(0, {"id": "@opening" if units else "@document",
                         "depth": 1, "parents": [], "start": 0, "end": first, "kind": "opening" if units else "document"})
    return blocks, units, positions


def matched_section_tree(units, positions, hits):
    """将 hits 合并为命中章节树；输入完整 units 和块 positions，返回按原文排列的根节点数组。

    每个片段只放在最具体的所属章节；祖先只保留一份标题和读取标识。
    不含命中也不是祖先的章节被省略，避免将局部检索变成全文目录。
    """
    grouped, retained = {}, set()
    for hit in sorted(hits, key=lambda item: positions[item["block_id"]]):
        index = positions[hit["block_id"]]
        chain = [unit for unit in units if unit["start"] <= index < unit["end"]]
        if not chain:
            raise ValueError("命中块没有对应的章节或特殊读取单位")
        # 块 ID 只供内部定位与排序；业务片段不重复暴露独立 block_id 字段。
        grouped.setdefault(chain[-1]["id"], []).append({key: value for key, value in hit.items() if key != "block_id"})
        retained.update(unit["id"] for unit in chain)
    roots, nodes = [], {}
    # units 已按文章位置排列，父章必然先于子章；片段排序不依赖服务端返回顺序。
    for unit in units:
        if unit["id"] not in retained:
            continue
        node = {"unit_id": unit["id"],
                "hits": grouped.get(unit["id"], []), "children": []}
        if "title" in unit:
            node["title"] = unit["title"]
        nodes[unit["id"]] = node
        target = nodes[unit["parents"][-1]]["children"] if unit["parents"] else roots
        target.append(node)
    return roots
