"""原型共享的来源解析与证据保存：不依赖五工具入口。

复用正式 daily 脚本的来源常量、认证调用和行内解析；正式脚本保持不变。
月份、文章缓存与演示从此处直接导入共享函数，避免反向依赖入口。
"""

from datetime import date
import json
from pathlib import Path
import re
import sys
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "skills/zeno-waytoagi-search/scripts"))
import waytoagi_daily as daily

SPACE = "7226178700923011075"
SAMPLE = "https://waytoagi.feishu.cn/wiki/HwMqw2TTpirl1Ykh5MhcfMEXnxd"
PARSER_VERSION = 2


def write_json(path, value):
    """把 value 写入 path 指定的 UTF-8 JSON 文件；返回绝对路径，写入失败直接报错。"""
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def xml_root(content):
    """把 CLI 的多根 XML 字符串 content 包为根节点；返回可遍历树，解析失败报错。"""
    root = ET.fromstring("<root>" + content + "</root>")
    if len(root) == 1 and root[0].tag == "fragment":
        return root[0]
    return root


def strip_comments(response):
    """原地剥除 response 中评论 sidecar、评论正文和锚点；返回剥除数量，不保留评论文本。"""
    count = 0
    document = response.get("data", {}).get("document", {})
    references = document.get("reference_map", {})
    removed = references.pop("comments", {})
    count += len(removed)
    content = document.get("content", "")
    content, n = re.subn(r"<comment\b[^>]*>.*?</comment>", "", content, flags=re.S)
    count += n
    content = re.sub(r"</?(?:comment-ref|comment-anchor|comment-start|comment-end)\b[^>]*>", "", content)
    content = re.sub(r'\s+comment(?:-ids?|-refs?)="[^"]*"', "", content)
    document["content"] = content
    return count


def log_entries(content, source, current_day):
    """从日志 content 按日期标题和引用关联原摘要；输入来源与当天日期，返回记录、未解析项、年份路由。"""
    root = xml_root(content)
    year, batch = None, None
    records, unsupported, routes = [], [], {}
    for element in root.iter():
        if element.tag == "cite":
            route = re.fullmatch(r"\s*(\d{4})\s*年历史更新[（(]归档[）)]\s*", element.get("title", ""))
            if route:
                routes[int(route[1])] = daily.inline_content(element)[1][0]["url"]
        if re.fullmatch(r"h[1-6]", element.tag):
            label = daily.inline_content(element)[0].strip()
            month = re.fullmatch(r"(\d{4})\s*年\s*\d{1,2}\s*月", label)
            if month:
                year = int(month[1])
                batch = None
            day = re.fullmatch(r"(?:(\d{4})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*日", label)
            if day:
                y = int(day[1]) if day[1] else year or current_day.year
                batch = date(y, int(day[2]), int(day[3]))
                if not day[1] and year is None and batch > current_day:
                    batch = date(y - 1, int(day[2]), int(day[3]))
        if element.tag not in {"li", "p"} or not batch or batch < daily.SUPPORTED_FROM:
            continue
        # 只处理本块的行内内容；容器中的子列表由遍历单独处理，避免重复。
        text, links = daily.inline_content(element)
        if not text.strip():
            continue
        references = [link for link in links if link["kind"] == "document_reference"]
        if len(references) != 1:
            unsupported.append({"date": str(batch), "block_id": element.get("id"), "reason": "非单篇文章引用"})
            continue
        ref = references[0]
        if ref["url"] == daily.HISTORY_URL and text.startswith("阅读更多："):
            continue
        prefix = "《" + ref["title"] + "》"
        if not text.lstrip().startswith(prefix):
            unsupported.append({"date": str(batch), "block_id": element.get("id"), "reason": "标题包裹格式未覆盖"})
            continue
        summary = text.lstrip()[len(prefix):]
        records.append({"article_id": ref["url"].rsplit("/", 1)[-1], "url": ref["url"],
                        "title": ref["title"], "date": str(batch), "summary": summary,
                        "log_url": source, "log_block_id": element.get("id")})
    return records, unsupported, routes
