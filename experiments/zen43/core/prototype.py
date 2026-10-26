"""五工具统一 Python 入口：协调只读请求、缓存、章节与正文交付。

共享解析位于 source/sections，正文表达位于 markdown；命令行入口位于上级 prototype.py。
source/pool 方法仅供历史整页日志实验保留，不用于当前检索或摘要入口。
"""

from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile
import time
from xml.etree import ElementTree as ET

from .source import SPACE, PARSER_VERSION, daily, log_entries, strip_comments, write_json, xml_root
from .sections import matched_section_tree, section_model, top_blocks
from .markdown import localize_image_reference


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
        from experiments.zen43.core.progressive_search import search
        return search(self, query, since, until, cursor)

    def summaries(self, articles):
        """读取 articles 的原摘要；先复用记录，过期定点刷新，陌生文章才按月查找。"""
        from experiments.zen43.core.summary_reader import summaries
        return summaries(self, articles)

    def article_fetch(self, article, scope="full", format="xml", **options):
        """输入 article、scope、format 与范围选项；返回后三工具共用的七天文章快照内容。"""
        from experiments.zen43.core.article_cache import fetch_article
        return fetch_article(self, article, scope, format, **options)

    def structure(self, article, parent_id=None, depth=1):
        """获取 article 在 parent_id 下 depth 层标题树；返回目录节点及查询起点 parent。

        parent_id 缺省从文章外层开始；指定时不重复基节点，depth=1 只列直接子层。
        普通 unit_id 是原生标题块 ID；@opening/@document 是工具定义的特殊读取单位。
        has_children 根据原文完整结构判断，不受 depth 裁剪影响；整篇查询的 parent 为 None。
        """
        if type(depth) is not int or depth < 1:
            raise ValueError("展开深度须为正整数")
        document = self.article_fetch(article)
        _, units, _ = section_model(document["content"])
        base = next((u for u in units if u["id"] == parent_id), None) if parent_id else None
        if parent_id and not base:
            raise ValueError("不是可选的顶层章节")
        # 在深度裁剪之前收集有直接子章的单位，避免把未展开误标为已到底。
        parents_with_children = {u["parents"][-1] for u in units if u["parents"]}
        visible = [u for u in units if (parent_id in u["parents"] and u["depth"] <= base["depth"] + depth)] if base else [u for u in units if u["depth"] <= depth]
        roots, nodes = [], {}
        # 原文顺序保证父章先出现；局部查询的基节点不在 nodes，其直接子章成为返回树根。
        for unit in visible:
            node = {"unit_id": unit["id"], "kind": unit["kind"],
                    "has_children": unit["id"] in parents_with_children, "children": []}
            if "title" in unit:
                node["title"] = unit["title"]
            nodes[unit["id"]] = node
            parent = nodes.get(unit["parents"][-1]) if unit["parents"] else None
            (parent["children"] if parent else roots).append(node)
        self.diagnostics["structure_units"] = units
        query_parent = {"unit_id": base["id"], "has_children": base["id"] in parents_with_children} if base else None
        return {"article": article, "revision": document["revision_id"], "parent": query_parent, "outline": roots}

    def locate(self, article, keywords):
        """在 article 中以 keywords 的 OR 查询定位；返回按原文排列、合并片段的命中章节树。"""
        hits = self.article_fetch(article, "keyword", keyword="|".join(keywords), context_before=0, context_after=0)
        root = xml_root(hits["content"])
        result = {"article": article, "revision": hits["revision_id"], "keywords": keywords, "match_tree": []}
        # 原生明确返回空内容时没有待补的位置；不为无命中查询再读取全文。
        if not hits["content"].strip() or not len(root):
            return result
        full = self.article_fetch(article)
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
                complete = self.article_fetch(article, "range", start_block_id=container_id, end_block_id=container_id)
                if complete["revision_id"] != full["revision_id"]:
                    raise ValueError("补全容器时文章发生更新")
                content = complete["content"]
            index = positions.get(block_id)
            if index is None:
                raise ValueError("命中块无法映射到当前全文，不猜位置")
            from experiments.zen43.core.markdown import public_snippet
            output.append({"block_id": block_id, "snippet_xml": public_snippet(content), "container_expanded": bool(container_id)})
        result["match_tree"] = matched_section_tree(units, positions, output)
        return result

    def read(self, article, unit_ids=None, start_unit_id=None, end_unit_id=None):
        """按 article 及可选单位列表或起止单位读取正文；返回 Markdown 路径、修订、范围类型和警告。"""
        if unit_ids and (start_unit_id or end_unit_id):
            raise ValueError("章节列表与范围不能同时指定")
        full = self.article_fetch(article)
        blocks, units, _ = section_model(full["content"])
        by_id = {unit["id"]: unit for unit in units}
        if unit_ids:
            intervals = [(by_id[key]["start"], by_id[key]["end"]) for key in unit_ids]
        elif start_unit_id or end_unit_id:
            intervals = [(by_id[start_unit_id]["start"] if start_unit_id else 0, by_id[end_unit_id]["end"] if end_unit_id else len(blocks))]
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
            if not unit_ids and not start_unit_id and not end_unit_id:
                body = self.article_fetch(article, "full", format="markdown")
            else:
                body = self.article_fetch(article, "range", format="markdown", start_block_id=first, end_block_id=last)
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
                            from experiments.zen43.core.article_cache import preview_article_image
                            images[token] = preview_article_image(self, article, token, folder / token)
                        except ValueError as error:
                            images[token] = {"status": "failed", "reason": str(error)}
                            warnings.append("所选正文中有图片获取失败，已在原位置标记。")
                    saved = images[token].get("saved_path")
                    replacement = str(Path(saved).resolve()) if saved else "图片获取失败"
                    content = localize_image_reference(content, token, replacement)
            from experiments.zen43.core.markdown import format_markdown
            markdown.append(format_markdown(content, images, warnings))
            ranges.append({"start_block_id": first, "end_block_id": last, "top_level_blocks": right - left,
                           "unit_ids": [u["id"] for u in units if left <= u["start"] < right]})
        path = folder / "body.md"
        path.write_text("\n\n".join(markdown), encoding="utf-8")
        self.diagnostics["body_ranges"] = ranges
        self.diagnostics["body_images"] = images
        return {"article": article, "revision": full["revision_id"], "markdown_path": str(path),
                "warnings": warnings,
                "scope": "full" if not unit_ids and not start_unit_id and not end_unit_id else "selected_sections"}
