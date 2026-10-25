"""ZEN-43 可丢弃技术原型：五个工具的真实只读 lark-cli 组合。

复用相邻 daily 脚本的 CLI 校验和行内文字解析；仅依赖 Python 标准库。
缓存、去评论后的原生响应、正文和图片只写入执行环境临时目录。
本文件验证主要路径，不是已发布接口；异常产品规则由 ZEN-44 决定。
"""

import argparse
import copy
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
import time
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
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
        units.insert(0, {"id": "@opening" if units else "@document", "title": "开篇正文" if units else "全文（无标题）",
                         "depth": 1, "parents": [], "start": 0, "end": first, "kind": "reading_unit"})
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
        node = {"read_unit": unit["id"], "title": unit["title"],
                "hits": grouped.get(unit["id"], []), "children": []}
        nodes[unit["id"]] = node
        target = nodes[unit["parents"][-1]]["children"] if unit["parents"] else roots
        target.append(node)
    return roots


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


class Prototype:
    """本次运行的只读调用上下文：保存临时缓存、调用证据及五个工具的中间状态。"""

    def __init__(self, cache_dir, profile=None):
        """以 cache_dir 和可选 profile 初始化原型；返回实例，目录仅保存可清除实验数据。"""
        self.input_started = time.monotonic()
        self.cache = Path(cache_dir).resolve()
        self.cache.mkdir(parents=True, exist_ok=True)
        self.run_dir = Path(tempfile.mkdtemp(prefix="zen43-run-"))
        self.profile = profile
        self.calls = []
        self.sources = []
        self.diagnostics = {}

    def call(self, arguments):
        """执行 arguments 中的只读 CLI，立即去评论；返回响应并在临时目录记录去评论后的证据。"""
        if arguments[:2] not in [["docs", "+fetch"], ["drive", "+search"], ["wiki", "+node-get"], ["docs", "+media-preview"]]:
            raise ValueError("原型只允许指定的四类只读 CLI 调用")
        args = [*arguments, "--as", "user", "--format", "json"]
        if self.profile:
            args += ["--profile", self.profile]
        started = time.monotonic()
        response = daily.run_cli(args)
        # 记录真实返回时刻；累积时间包含此前调用及其间的本地解析，不用单次耗时求和替代。
        completed = time.monotonic()
        removed = strip_comments(response)
        path = write_json(self.run_dir / f"call-{len(self.calls) + 1:03}.json", response)
        document = response.get("data", {}).get("document", {})
        self.calls.append({"arguments": args, "seconds": round(completed - started, 3),
                           "elapsed_seconds": round(completed - self.input_started, 3),
                           "evidence_path": path, "revision": document.get("revision_id"),
                           "content_chars": len(document.get("content", "")), "comments_removed": removed})
        return response

    def fetch(self, article, scope="full", format="xml", **options):
        """按 article、scope、format 及 CLI 选项读取最新内容；返回 document，显式提示或节选不得当全文。"""
        max_depth = options.pop("max_depth", -1)
        args = ["docs", "+fetch", "--doc", article, "--scope", scope, "--doc-format", format,
                "--detail", "with-ids" if format == "xml" else "simple", "--max-depth", str(max_depth)]
        for name, value in options.items():
            args += ["--" + name.replace("_", "-"), str(value)]
        document = self.call(args)["data"]["document"]
        if document.get("tips"):
            raise ValueError("原生读取含 tips；需 ZEN-44 核对，不输出完整成功")
        if scope in {"full", "section", "range"} and "<excerpt" in document.get("content", ""):
            raise ValueError("原生读取仍为 excerpt，当前原型不能证明选区完整")
        return document

    def source(self, url, recent=False):
        """检查 url 的最新修订，未变则复用解析缓存；recent 指入口日志，返回来源记录与年份路由。"""
        check = self.fetch(url, "outline", max_depth=1)
        key = hashlib.sha256(((self.profile or "default") + url).encode()).hexdigest()[:20]
        path = self.cache / (key + ".json")
        cached = json.loads(path.read_text()) if path.exists() else None
        revision = check.get("revision_id")
        hit = bool(cached and cached.get("parser_version") == PARSER_VERSION
                   and revision is not None and cached["revision"] == revision)
        if not hit:
            if recent:
                headings = [h for h in xml_root(check["content"]).iter("h1") if daily.SECTION in daily.inline_content(h)[0]]
                if len(headings) != 1:
                    raise ValueError("无法唯一定位近期日志")
                document = self.fetch(url, "section", start_block_id=headings[0].get("id"))
            else:
                document = self.fetch(url)
            if revision != document.get("revision_id"):
                raise ValueError("日志在检查与读取间发生更新；不混用修订")
            records, unsupported, routes = log_entries(document["content"], url, datetime.now(daily.reading_timezone()).date())
            cached = {"parser_version": PARSER_VERSION, "revision": revision,
                      "records": records, "unsupported": unsupported, "routes": routes}
            write_json(path, cached)
        self.sources.append({"url": url, "revision": revision, "cache_hit": hit,
                             "records": len(cached["records"]), "unsupported": cached["unsupported"]})
        return cached

    def pool(self):
        """按需检查近期页及支持期年度归档；返回按文章保留最近记录的索引，空摘要最后排除。"""
        recent = self.source(daily.SOURCE_URL, recent=True)
        archive = self.source(daily.HISTORY_URL)
        records = recent["records"] + archive["records"]
        for year, url in archive["routes"].items():
            if int(year) >= daily.SUPPORTED_FROM.year:
                records += self.source(url)["records"]
        latest = {}
        for record in records:
            old = latest.get(record["article_id"])
            if old is None or record["date"] > old["date"]:
                latest[record["article_id"]] = record
        self.latest_records = latest
        self.log_records = records
        return {key: record for key, record in latest.items() if record["summary"].strip()}

    def resolve(self, article):
        """把 article URL/token 解析成固定社区的 Wiki 与文档标识；返回最小身份映射，失败不猜别名。"""
        data = self.call(["wiki", "+node-get", "--node-token", article, "--space-id", SPACE])["data"]
        return {key: data.get(key) for key in ("node_token", "obj_token", "obj_type", "space_id", "title")}

    def search(self, query=None, since=None, until=None, cursor=None):
        """执行一次默认三条的渐进检索；输入查询、日期或继续标识，返回精简业务结果。"""
        from progressive_search import search
        return search(self, query, since, until, cursor)

    def summaries(self, articles):
        """读取 articles 的原摘要；先复用记录，过期定点刷新，陌生文章才按月查找。"""
        from summary_reader import summaries
        return summaries(self, articles)

    def structure(self, article, section=None, depth=1):
        """获取 article 在 section 下 depth 层标题树；返回目录节点及查询起点 parent。

        section 缺省从文章外层开始；指定时不重复基节点，depth=1 只列直接子层。
        普通 read_unit 是原生标题块 ID；@opening/@document 是工具定义的特殊读取单位。
        has_children 根据原文完整结构判断，不受 depth 裁剪影响；整篇查询的 parent 为 None。
        """
        if type(depth) is not int or depth < 1:
            raise ValueError("展开深度须为正整数")
        document = self.fetch(article)
        _, units, _ = section_model(document["content"])
        base = next((u for u in units if u["id"] == section), None) if section else None
        if section and not base:
            raise ValueError("不是可选的顶层章节")
        # 在深度裁剪之前收集有直接子章的单位，避免把未展开误标为已到底。
        parents_with_children = {u["parents"][-1] for u in units if u["parents"]}
        visible = [u for u in units if (section in u["parents"] and u["depth"] <= base["depth"] + depth)] if base else [u for u in units if u["depth"] <= depth]
        roots, nodes = [], {}
        # 原文顺序保证父章先出现；局部查询的基节点不在 nodes，其直接子章成为返回树根。
        for unit in visible:
            node = {"title": unit["title"], "read_unit": unit["id"], "kind": unit["kind"],
                    "has_children": unit["id"] in parents_with_children, "children": []}
            nodes[unit["id"]] = node
            parent = nodes.get(unit["parents"][-1]) if unit["parents"] else None
            (parent["children"] if parent else roots).append(node)
        self.diagnostics["structure_units"] = units
        query_parent = {"read_unit": base["id"], "has_children": base["id"] in parents_with_children} if base else None
        return {"article": article, "revision": document["revision_id"], "parent": query_parent, "sections": roots}

    def locate(self, article, keywords):
        """在 article 中以 keywords 的 OR 查询定位；返回按原文排列、合并片段的命中章节树。"""
        hits = self.fetch(article, "keyword", keyword="|".join(keywords), context_before=0, context_after=0)
        root = xml_root(hits["content"])
        result = {"article": article, "revision": hits["revision_id"], "keywords": keywords, "sections": []}
        # 原生明确返回空内容时没有待补的位置；不为无命中查询再读取全文。
        if not hits["content"].strip() or not len(root):
            return result
        full = self.fetch(article)
        blocks, units, positions = section_model(full["content"])
        if hits["revision_id"] != full["revision_id"]:
            raise ValueError("定位时文章发生更新")
        output, seen = [], set()
        for element in top_blocks(root):
            container_id = element.get("top-block-id") if element.tag == "excerpt" else None
            block_id = container_id or element.get("id")
            if not block_id or block_id in seen:
                continue
            seen.add(block_id)
            content = ET.tostring(element, encoding="unicode")
            if container_id:
                complete = self.fetch(article, "range", start_block_id=container_id, end_block_id=container_id)
                if complete["revision_id"] != full["revision_id"]:
                    raise ValueError("补全容器时文章发生更新")
                content = complete["content"]
            index = positions.get(block_id)
            if index is None:
                raise ValueError("命中块无法映射到当前全文，不猜位置")
            output.append({"block_id": block_id, "snippet_xml": content, "container_expanded": bool(container_id)})
        result["sections"] = matched_section_tree(units, positions, output)
        return result

    def read(self, article, sections=None, start=None, end=None):
        """读取 article 全文/多章/完整章节范围；返回 Markdown 绝对路径、实际选区和本地图片信息。"""
        if sections and (start or end):
            raise ValueError("章节列表与范围不能同时指定")
        full = self.fetch(article)
        blocks, units, _ = section_model(full["content"])
        by_id = {unit["id"]: unit for unit in units}
        if sections:
            intervals = [(by_id[key]["start"], by_id[key]["end"]) for key in sections]
        elif start or end:
            intervals = [(by_id[start]["start"] if start else 0, by_id[end]["end"] if end else len(blocks))]
        else:
            intervals = [(0, len(blocks))]
        merged = []
        for left, right in sorted(intervals):
            if left >= right:
                raise ValueError("章节范围顺序不正确")
            if merged and left <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(right, merged[-1][1]))
            else:
                merged.append((left, right))
        markdown, images, ranges, warnings = [], {}, [], []
        # macOS 的默认临时目录不在 lark-cli 输出白名单；使用它明确支持的 /tmp。
        folder = Path(tempfile.mkdtemp(prefix="zen43-body-", dir="/tmp")).resolve()
        for left, right in merged:
            first, last = blocks[left].get("id"), blocks[right - 1].get("id")
            if not first or not last:
                raise ValueError("选区端点缺少块标识")
            if not sections and not start and not end:
                body = self.fetch(article, "full", format="markdown")
            else:
                body = self.fetch(article, "range", format="markdown", start_block_id=first, end_block_id=last)
            if body["revision_id"] != full["revision_id"]:
                raise ValueError("正文与章节结构修订不一致")
            content = body["content"]
            for block in blocks[left:right]:
                for image in block.iter("img"):
                    token = image.get("src")
                    if not token:
                        continue
                    if token not in images:
                        try:
                            result = self.call(["docs", "+media-preview", "--token", token, "--output", str(folder / token)])
                            images[token] = result["data"]
                        except ValueError as error:
                            images[token] = {"status": "failed", "reason": str(error)}
                            warnings.append("图片获取失败：" + token + "；" + str(error))
                    saved = images[token].get("saved_path")
                    if saved:
                        content = content.replace("https://feishu.cn/file/" + token, str(Path(saved).resolve()))
                    else:
                        content = content.replace("https://feishu.cn/file/" + token, "图片获取失败:" + token)
            markdown.append(content)
            ranges.append({"start_block_id": first, "end_block_id": last, "top_level_blocks": right - left,
                           "units": [u["id"] for u in units if left <= u["start"] < right]})
        path = folder / "body.md"
        path.write_text("\n\n".join(markdown), encoding="utf-8")
        return {"article": article, "revision": full["revision_id"], "markdown_path": str(path),
                "actual_ranges": ranges, "images": images, "warnings": warnings,
                "scope": "full" if not sections and not start and not end else "selected_sections"}


def main():
    """解析命令行五入口与参数，打印完整 JSON 状态；返回退出码，执行证据写入临时目录。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tool", choices=["search", "summaries", "locate", "structure", "read"])
    parser.add_argument("--article", default=SAMPLE)
    parser.add_argument("--articles", nargs="+")
    parser.add_argument("--query")
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--cursor")
    parser.add_argument("--keywords", nargs="+", default=["MarkItDown", "MinerU"])
    parser.add_argument("--section")
    parser.add_argument("--sections", nargs="+")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--depth", type=int, default=1)
    parser.add_argument("--cache-dir", default=str(Path(tempfile.gettempdir()) / "zen43-PROTOTYPE-wipe-me"))
    parser.add_argument("--profile")
    args = parser.parse_args()
    prototype = Prototype(args.cache_dir, args.profile)
    try:
        if args.tool == "search":
            result = prototype.search(args.query, args.since, args.until, args.cursor)
        elif args.tool == "summaries":
            result = prototype.summaries(args.articles or [args.article])
        elif args.tool == "structure":
            result = prototype.structure(args.article, args.section, args.depth)
        elif args.tool == "locate":
            result = prototype.locate(args.article, args.keywords)
        else:
            result = prototype.read(args.article, args.sections, args.start, args.end)
        write_json(prototype.run_dir / "calls.json", prototype.calls)
        write_json(prototype.run_dir / "diagnostics.json", prototype.diagnostics)
        # 已迭代的四个入口直接交付业务 JSON，诊断留本地，避免占用 Agent 阅读预算。
        output = result if args.tool in {"search", "summaries", "locate", "structure"} else {"result": result, "evidence_dir": str(prototype.run_dir)}
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, KeyError, OSError, ET.ParseError) as error:
        write_json(prototype.run_dir / "calls.json", prototype.calls)
        print(json.dumps({"status": "prototype_error", "error": str(error), "evidence_dir": str(prototype.run_dir)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
