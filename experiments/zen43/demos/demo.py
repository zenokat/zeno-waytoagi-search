"""运行 ZEN-43 真实只读演示与针对性核对，生成可双击的自包含 HTML 结果页。

一个命令运行五工具主要路径。所有断言只核对本次实际样本，不替代 ZEN-44。
原生证据与正文文件留在临时目录；审阅页保留完整业务 JSON，并内嵌正文与图片审阅快照。
"""

import argparse
from datetime import datetime
import json
from pathlib import Path
import tempfile
import time

from experiments.zen43.core.prototype import Prototype
from experiments.zen43.core.source import SAMPLE, write_json, xml_root
from experiments.zen43.core.sections import section_model
from experiments.zen43.core.monthly_logs import MonthlyLogs, HISTORY_TTL_SECONDS


def assert_images(result, diagnostics):
    """按 result 正文与内部 diagnostics 核对图片已落盘并嵌入；返回图片数，不向业务返回补字段。"""
    content = Path(result["markdown_path"]).read_text()
    assert not result["warnings"], result["warnings"]
    for item in diagnostics["body_images"].values():
        path = Path(item["saved_path"]).resolve()
        assert path.is_file() and path.stat().st_size > 0
        assert str(path) in content
    assert "https://feishu.cn/file/" not in content
    return len(diagnostics["body_images"])


def compact_result(result):
    """缩短用于便携 HTML 展示的 result，保留结构和计数；返回副本，完整结果仍存于本地证据。"""
    result = json.loads(json.dumps(result))
    if "results" in result:
        for row in result["results"]:
            if "summary" in row:
                row["summary_chars"] = len(row.pop("summary"))
    return result


def section_nodes(sections):
    """将 sections 命中树按原文顺序展开，返回节点列表；仅供演示核对，不改变业务 JSON。"""
    return [node for section in sections for node in [section, *section_nodes(section["children"])]]


def scenario_revision(scenario):
    """从 scenario 内部诊断取得修订；兼容旧证据中的业务字段，返回来源修订值。"""
    diagnostics = scenario.get("diagnostics", {})
    return diagnostics["revision"] if "revision" in diagnostics else scenario["output"]["revision"]


def command_purpose(arguments, method):
    """根据真实 arguments 与工具 method 生成中文命令作用；返回说明字符串，不改变命令。"""
    options = dict(zip(arguments[2::2], arguments[3::2]))
    command = arguments[:2]
    if command == ["drive", "+search"]:
        return "按需获取下一批创建日期倒序候选，保留未匹配文章供后续继续。" if "--page-token" in options else "在 WaytoAGI 知识库执行原查询，按创建日期倒序取得第一批候选。"
    if command == ["wiki", "+node-get"]:
        return "将文章链接或标识解析为 Wiki 节点和底层文档标识，并核对所属知识库。"
    if command == ["docs", "+media-preview"]:
        return "下载所选正文中引用的这一张图片，取得可嵌入 Markdown 的本地绝对路径。"
    scope = options.get("--scope", "full")
    if scope == "outline":
        return "读取浅层目录与修订号，检查日志是否更新；近期入口还用它定位日志板块。"
    if scope == "section":
        return "直接按已知章节 ID 读取所需日志板块，解析月份记录、文章引用和原始摘要。"
    if scope == "keyword":
        return "按 OR 关键词查找文内命中块或容器节选，随后补齐所属章节链。"
    if scope == "range":
        if method in {"search", "summaries"}:
            return "按已保存的日志条目位置刷新这一条原摘要，核对块 ID 与文章身份；不延长整月缓存有效期。"
        return "读取计算后的完整章节区间，取得 Markdown 正文。" if options.get("--doc-format") == "markdown" else "读取指定块范围的完整 XML。"
    if options.get("--doc-format") == "markdown":
        return "读取全文 Markdown，保留文章标题与正文结构，随后处理文内图片。"
    if method in {"search", "summaries"}:
        return "完整读取历史日志页，解析日期、文章引用、原始摘要及年度归档入口。"
    return "读取完整 XML 与块标识，建立真实章节层级、块归属及完整章节边界。"


def build_html(path, scenarios, checks):
    """把 scenarios 真实输入输出和 checks 核对结果嵌入单文件 HTML；返回文件绝对路径。"""
    payload = json.dumps({"scenarios": scenarios, "checks": checks}, ensure_ascii=False).replace("<", "\\u003c")
    page = Path(__file__).with_name("demo_template.html").read_text(encoding="utf-8").replace("PAYLOAD", payload)
    Path(path).write_text(page, encoding="utf-8")
    return str(Path(path).resolve())


def run_demo(output):
    """在 output 目录执行真实五入口和正常语义核对；返回 HTML、证据路径和核对结论。"""
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    cache = output / "PROTOTYPE-wipe-me-cache"
    scenarios, checks = [], {}

    def capture(name, explanation, arguments, method, cache_override=None, **kwargs):
        """用独立原型上下文执行 method；输入说明、显示参数和函数参数，返回结果与上下文。"""
        p = Prototype(cache_override or cache)
        result = getattr(p, method)(**kwargs)
        total_seconds = round(time.monotonic() - p.input_started, 3)
        write_json(p.run_dir / "calls.json", p.calls)
        result_path = write_json(p.run_dir / "result.json", result)
        native = []
        for call in p.calls:
            argv = list(call["arguments"])
            if "--page-token" in argv:
                argv[argv.index("--page-token") + 1] = "<原生续页令牌，仅本地保留>"
            native.append({"command": argv, "purpose": command_purpose(call["arguments"], method),
                           "revision": call["revision"], "seconds": call["seconds"],
                           "elapsed_seconds": call["elapsed_seconds"],
                           "content_chars": call["content_chars"]})
        scenario = {"name": name, "explain": explanation, "input": arguments,
                          "output": result, "exact_output": True, "diagnostics": p.diagnostics,
                          "native": native, "local_result": result_path,
                          "total_seconds": total_seconds}
        if method == "read":
            # 按需载入正文审阅核对，前四工具不读取或内嵌正文文件。
            from experiments.zen43.demos.body_demo import review_body
            scenario["body_review"] = review_body(result, p, arguments)
        scenarios.append(scenario)
        print(name + "完成", flush=True)
        return result, p

    search, first = capture("知识库检索", "凑满 3 条或查完后返回；内部按需推进原生候选与日志月份，不承诺全局日期严格排序。",
        {"query": "RAG"}, "search", query="RAG")
    assert first.diagnostics["native_pages"] >= 1
    assert len(search["results"]) <= 3 and search["next_cursor"]
    assert all("snippet" in r and "snippet_status" not in r for r in search["results"])
    second, continued = capture("检索下一页", "接续同一次查询；先交付已匹配但尚未返回的文章，必要时才继续取候选或读取月份。",
        {"cursor": "上一页返回的游标"}, "search", cursor=search["next_cursor"])
    assert not set(x["article_id"] for x in search["results"]) & set(x["article_id"] for x in second["results"])
    checks["首屏工作量"] = "按需取得候选与月份，凑满 3 条或查完后返回"
    checks["渐进分页"] = "每页最多 3 条，前两页无重复；未匹配项保留，跨月不作为排除依据"

    warm_search, warm_context = capture("检索定向复用缓存", "重新执行原生搜索取得当前候选，随后按 token 命中已有记录；不重新读取当月日志。",
        {"query": "RAG", "演示前置": "已执行过一次检索"}, "search", query="RAG")
    assert warm_search["results"] == search["results"]
    assert len(warm_context.calls) == 1 and warm_context.diagnostics["months_read"] == []
    assert warm_context.calls[0]["arguments"][:2] == ["drive", "+search"]
    checks["检索缓存直达"] = "相同查询首屏结果相等；仅 1 次 drive 搜索，0 次日志请求"

    # 单独复制已有月份并模拟过期，不影响后续摘要演示的缓存前置。
    stale_cache = output / "PROTOTYPE-wipe-me-search-expired"
    current_month = str(MonthlyLogs(first).today)[:7]
    stale_path = MonthlyLogs(Prototype(stale_cache)).cache_path(current_month)
    stale_month = json.loads(MonthlyLogs(first).cache_path(current_month).read_text())
    stale_month["fetched_at"] = time.time() - HISTORY_TTL_SECONDS - 1
    write_json(stale_path, stale_month)
    stale_before = stale_path.read_text()
    stale_search, stale_context = capture("检索过期定点刷新", "原生候选命中过期记录，按需逐条刷新直到凑满一页；不一次刷新整批候选。",
        {"query": "RAG", "演示前置": "独立复制当月缓存并将年龄设为七天以前"}, "search",
        cache_override=stale_cache, query="RAG")
    assert len(stale_search["results"]) == 3 and stale_search["next_cursor"]
    assert stale_search["results"][0] == search["results"][0]
    assert len(stale_context.calls) == 4 and all("range" in c["arguments"] for c in stale_context.calls[1:])
    assert stale_path.read_text() == stale_before
    checks["检索定点刷新"] = "1 次 drive 搜索 + 3 次条目 range，未重读整月；整月期限保持不变"

    dates_only, cached = capture("按日期发现", "先按日期选择月份，只读取 2026 年 9 月；月内实际遇到重复文章才保留最近一次。不附摘要。",
        {"since": "2026-09-01", "until": "2026-09-30"}, "search", since="2026-09-01", until="2026-09-30")
    assert [source["month"] for source in cached.sources] == ["2026-09"]
    assert len(cached.calls) == 1
    assert all("summary" not in item for item in dates_only["results"])
    cache_path = MonthlyLogs(cached).cache_path("2026-09")
    cached_month = json.loads(cache_path.read_text())
    repeated = [r for r in cached_month["records"] if r["article_id"] == "YTt3wPPIeixEK5kbJPnc0YL7nTf"]
    assert len(repeated) > 1
    latest = max(repeated, key=lambda row: row["date"])
    checks["重复更新"] = f"该月 {len(repeated)} 次记录取 {latest['date']} 及该次原摘要，不额外检查范围外月份"

    _, cross_topic = capture("跨主题复用历史缓存", "更换查询词仍要执行 drive 搜索；9 月日志使用刚取得的缓存，不请求修订号或正文。",
        {"query": "Agent", "since": "2026-09-01", "until": "2026-09-30"}, "search",
        query="Agent", since="2026-09-01", until="2026-09-30")
    assert cross_topic.diagnostics["record_actions"] or any(source["cache_hit"] for source in cross_topic.sources)
    assert all(c["arguments"][:2] == ["drive", "+search"] for c in cross_topic.calls)
    # 只模拟缓存年龄，不模拟外部响应；过期后章节仍通过真实只读请求获取。
    cached_month["fetched_at"] = time.time() - HISTORY_TTL_SECONDS - 1
    write_json(cache_path, cached_month)
    _, expired = capture("七天到期再读取", "将本地缓存时间人为置为七天以前，验证用到该月时真实重读；不先请求整年修订号。",
        {"since": "2026-09-01", "until": "2026-09-30", "演示前置": "仅将缓存时间改为已过期"}, "search",
        since="2026-09-01", until="2026-09-30")
    assert len(expired.calls) == 1 and not expired.sources[0]["cache_hit"]
    checks["历史缓存"] = "跨主题命中时 0 次日志请求；模拟七天到期后真实重读 1 次目标月份"

    summary, summary_context = capture("摘要读取", "按文章 token 直接匹配检索已取得的原摘要；包括当月记录，无须重新读取日志。",
        {"articles": [search["results"][0]["article_id"], latest["article_id"]]}, "summaries",
        articles=[search["results"][0]["article_id"], latest["article_id"]])
    assert all(r["status"] == "found" for r in summary["results"])
    assert summary["results"][1]["summary"] == latest["summary"]
    assert summary_context.calls == [] and summary_context.diagnostics["months_read"] == []
    checks["摘要缓存直达"] = "当月和历史原摘要均按 token 直接命中，0 次请求；原摘要逐字相等"

    cached_month = json.loads(cache_path.read_text())
    cached_month["fetched_at"] = time.time() - HISTORY_TTL_SECONDS - 1
    write_json(cache_path, cached_month)
    month_before_refresh = cache_path.read_text()
    refreshed, refreshed_context = capture("摘要过期定点刷新", "仅模拟缓存年龄过期；按保存的日志块直接读取这一条摘要，不读取整月，不更新整月期限。",
        {"articles": [latest["article_id"]], "演示前置": "9 月缓存年龄设为七天以前"}, "summaries",
        articles=[latest["article_id"]])
    assert len(refreshed_context.calls) == 1 and "range" in refreshed_context.calls[0]["arguments"]
    assert refreshed["results"][0]["summary"] == latest["summary"]
    assert cache_path.read_text() == month_before_refresh
    _, refreshed_reuse = capture("刷新后再读摘要", "只复用刚刷新的这一条摘要，零请求；同月其他旧记录仍维持原到期时间。",
        {"articles": [latest["article_id"]]}, "summaries", articles=[latest["article_id"]])
    assert refreshed_reuse.calls == []
    checks["摘要定点刷新"] = "过期后 1 次 range；整月缓存未改动；再读 0 次请求"

    old_article = json.loads((Path(__file__).resolve().parents[1] / "data/summary-lookup-samples.json").read_text())[0]
    cold_cache = output / "PROTOTYPE-wipe-me-summary-cold"
    cold_summary, cold_context = capture("陌生旧文章摘要", "独立空缓存，仅输入文章 token；没有任何日志位置，只能逐月向前查找。这是仍存在的性能缺口。",
        {"articles": [old_article["article_id"]], "演示前置": "独立空缓存，不提供已知日期或日志块"}, "summaries",
        cache_override=cold_cache, articles=[old_article["article_id"]])
    assert cold_summary["results"][0]["summary"] == old_article["summary"]
    assert cold_context.diagnostics["months_read"][-1] == old_article["date"][:7]
    _, old_reused = capture("旧文章再次读摘要", "同一文章已经查到，直接按 token 取缓存，跳过所有较新的月份。",
        {"articles": [old_article["article_id"]]}, "summaries", cache_override=cold_cache,
        articles=[old_article["article_id"]])
    assert old_reused.calls == []
    old_search, old_search_context = capture("检索直达旧记录", "之前为摘要取得的历史日志也能被检索复用；重新搜索候选后直接匹配，不再逐月寻找这篇旧文章。",
        {"query": "鸡同鸭讲", "演示前置": "共享刚才旧文章摘要查找的缓存"}, "search", cache_override=cold_cache,
        query="鸡同鸭讲")
    assert old_article["article_id"] in [row["article_id"] for row in old_search["results"]]
    assert all(c["arguments"][:2] == ["drive", "+search"] for c in old_search_context.calls)
    checks["跨工具复用"] = "摘要找到的旧文章可供检索直接匹配；仅 1 次 drive 搜索，0 次日志请求"
    checks["陌生文章缺口"] = f"旧文章首次 {len(cold_context.calls)} 次读取，再次 0 次；首次没有假装已知日志位置"

    # 沿同一真实会话继续，确认原生下一页确实在需要时才发生，而非仅做本地切片。
    cursor, seen, continuation_evidence = second["next_cursor"], {r["article_id"] for r in search["results"] + second["results"]}, []
    for _ in range(30):
        if not cursor:
            break
        p = Prototype(cache)
        result = p.search(cursor=cursor)
        assert not seen & {r["article_id"] for r in result["results"]}
        seen.update(r["article_id"] for r in result["results"])
        continuation_evidence.append({"result": result, "diagnostics": p.diagnostics, "calls": p.calls})
        cursor = result["next_cursor"]
        if p.diagnostics["native_pages"] > 1:
            checks["按需原生续页"] = "已通过后续提交触发原生第二批候选，交付文章未重复"
            break
    assert "按需原生续页" in checks
    write_json(output / "continuation-checks.json", continuation_evidence)

    structure, structure_context = capture("篇章结构获取", "原文 H1 下直接接 H3；工具按真实父子关系返回第二层，而非用 H 数字当展开深度。",
        {"article": SAMPLE, "depth": 2}, "structure", article=SAMPLE, depth=2)
    structure_units = structure_context.diagnostics["structure_units"]
    children = [u for u in structure_units if u.get("rank") == 3 and u["depth"] == 2]
    assert children and all(len(u["parents"]) == 1 for u in children)
    checks["章节层级"] = "H1 → H3 识别为父子两层"

    located, located_context = capture("全文检索", "按原文顺序展示命中章节树：共享祖先只出现一次，同一章节的片段合并为数组。只保留命中章节及祖先，不补未命中分支；OR 是任一关键词命中。",
        {"article": SAMPLE, "keywords": ["MarkItDown", "MinerU"]}, "locate", article=SAMPLE, keywords=["MarkItDown", "MinerU"])
    located_nodes = section_nodes(located["match_tree"])
    located_hits = [h for node in located_nodes for h in node["hits"]]
    assert all(not h["container_expanded"] for h in located_hits)
    assert len({node["unit_id"] for node in located_nodes}) == len(located_nodes)
    assert all(node["hits"] or node["children"] for node in located_nodes)
    assert any("<excerpt" in h["snippet_xml"] for h in located_hits)
    # 原生 OR 可能合并同容器的节选，因此比较命中单位并集，不要求节选 XML 字节相同。
    single_a, single_a_context = capture("单词 MarkItDown", "单独查询 MarkItDown；可与 OR 场景对照，普通段落、代码与标题保留原生命中块。",
        {"article": SAMPLE, "keywords": ["MarkItDown"]}, "locate", article=SAMPLE, keywords=["MarkItDown"])
    single_b, single_b_context = capture("单词 MinerU", "单独查询 MinerU；同一容器的不同命中节选均保留，仅精确重复节选去重。",
        {"article": SAMPLE, "keywords": ["MinerU"]}, "locate", article=SAMPLE, keywords=["MinerU"])
    assert located_context.diagnostics["revision"] == single_a_context.diagnostics["revision"] == single_b_context.diagnostics["revision"]
    single_units = {node["unit_id"] for result in (single_a, single_b)
                    for node in section_nodes(result["match_tree"]) if node["hits"]}
    assert {node["unit_id"] for node in located_nodes if node["hits"]} == single_units
    assert all(any(word in h["snippet_xml"] for word in ("MarkItDown", "MinerU")) for h in located_hits)
    checks["OR 与容器"] = f"{len(located_hits)} 个片段；命中读取单位并集相等，保留原生容器节选"
    checks["命中章节快照"] = f"{len(located_hits)} 个片段归入 {sum(bool(node['hits']) for node in located_nodes)} 个直接命中读取单位；保留祖先共 {len(located_nodes)} 个节点，共享路径只出现一次"

    heading, _ = capture("仅命中标题", "输入本篇唯一的章节标题；只返回命中标题，保留父章到子章的完整链，不自动展开整章正文。",
        {"article": SAMPLE, "keywords": ["安装 MarkItDown"]}, "locate", article=SAMPLE, keywords=["安装 MarkItDown"])
    heading_nodes = section_nodes(heading["match_tree"])
    assert len(heading_nodes) == 2 and heading_nodes[0]["hits"] == []
    assert len(heading_nodes[1]["hits"]) == 1
    assert xml_root(heading_nodes[1]["hits"][0]["snippet_xml"])[0].tag == "h3"
    assert "Python" not in heading_nodes[1]["hits"][0]["snippet_xml"]
    checks["标题与开篇定位"] = "标题只返回自身且保留两级章节链；OR 样本的开篇命中可用 @opening 读取"
    assert any(node["unit_id"] == "@opening" for node in located_nodes)

    local, _ = capture("仅局部正文命中", "关注 Python 版本要求，只呈现“动手安装吧 → 安装 MarkItDown”及其中命中段落；不附其他安装章节或全文目录。需要更多背景时，再用读取标识获取局部结构或正文。",
        {"article": SAMPLE, "keywords": ["Python 3.10"]}, "locate", article=SAMPLE, keywords=["Python 3.10"])
    local_nodes = section_nodes(local["match_tree"])
    assert [node["unit_id"] for node in local_nodes] == [node["unit_id"] for node in heading_nodes]
    assert local_nodes[0]["hits"] == [] and len(local_nodes[1]["hits"]) == 1
    assert xml_root(local_nodes[1]["hits"][0]["snippet_xml"])[0].tag == "p"
    checks["局部命中不扩目录"] = "Python 版本查询只保留父章与安装 MarkItDown 子章，不附安装 MinerU 等未命中章节"

    table, _ = capture("表格单行命中", "仅匹配一行中的文字，保留原生表格节选；完整表格需要通过 read 读取所在章节。",
        {"article": SAMPLE, "keywords": ["轻量、快速，适合批量转换"]}, "locate", article=SAMPLE, keywords=["轻量、快速，适合批量转换"])
    table_hits = [h for node in section_nodes(table["match_tree"]) for h in node["hits"]]
    assert len(table_hits) == 1 and not table_hits[0]["container_expanded"]
    table_xml = xml_root(table_hits[0]["snippet_xml"])
    assert len(list(table_xml.iter("tr"))) == 1 and "MinerU" not in table_hits[0]["snippet_xml"]
    checks["表格单行节选"] = "保留原生单行表格结构，不补取表头、其他工具行或表外正文"

    empty, empty_context = capture("全文检索无命中", "使用特定不存在的关键词，真实服务返回空内容后即结束；只做一次 keyword 请求，不再读取全文。",
        {"article": SAMPLE, "keywords": ["ZEN43_NO_MATCH_20261024"]}, "locate", article=SAMPLE, keywords=["ZEN43_NO_MATCH_20261024"])
    assert empty["match_tree"] == [] and len(empty_context.calls) == 1
    checks["全文检索无命中"] = "真实空结果只发起 1 次 keyword 请求，没有全文预读"

    parent = next(u for u in structure_units if u["id"] == children[0]["parents"][0])
    body, body_context = capture("正文读取", "选择父章和其中子章，重叠只输出一次；只获取选区里的图片并嵌入本地路径。",
        {"article": SAMPLE, "unit_ids": [parent["id"], children[0]["id"]]}, "read",
        article=SAMPLE, unit_ids=[parent["id"], children[0]["id"]])
    assert len(body_context.diagnostics["body_ranges"]) == 1
    assert body_context.diagnostics["body_ranges"][0]["top_level_blocks"] == parent["end"] - parent["start"]
    checks["多章合并与图片"] = f"重叠合并为一段，{assert_images(body, body_context.diagnostics)} 张图片已落盘并嵌入"

    bounded, bounded_context = capture("完整章节范围", "终点虽然是章节标题，内部端点扩到该章末尾；不是读到终点标题就停止。",
        {"article": SAMPLE, "start_unit_id": children[0]["id"], "end_unit_id": children[1]["id"]}, "read",
        article=SAMPLE, start_unit_id=children[0]["id"], end_unit_id=children[1]["id"])
    assert bounded_context.diagnostics["body_ranges"][0]["top_level_blocks"] == children[1]["end"] - children[0]["start"]
    assert bounded_context.diagnostics["body_ranges"][0]["end_block_id"] != children[1]["id"]
    assert_images(bounded, bounded_context.diagnostics)
    checks["范围终点"] = "包括终章正文和子章节"

    complete, complete_context = capture("全文读取", "无章节参数读取全部正文，并自动获取文内图片；得到文件不代表 Agent 已读完。",
        {"article": SAMPLE}, "read", article=SAMPLE)
    checks["全文与图片"] = f"{complete_context.diagnostics['body_ranges'][0]['top_level_blocks']} 个顶层块，{assert_images(complete, complete_context.diagnostics)} 张图片"
    checks["验证时间"] = datetime.now().astimezone().isoformat(timespec="seconds")
    manifest = {"checks": checks, "scenarios": scenarios}
    write_json(output / "evidence.json", manifest)
    timing_path = write_json(output / "timings.json", {
        "recorded_at": checks["验证时间"],
        "unit": "seconds",
        "definition": "每个场景从本次工具接收输入起独立计时；elapsed_seconds 截止该命令返回，total_seconds 截止工具完成本地整理。",
        "scenarios": [{"name": item["name"], "input": item["input"],
                       "total_seconds": item["total_seconds"], "commands": item["native"]} for item in scenarios],
    })
    page = build_html(output / "demo.html", scenarios, checks)
    return {"html": page, "evidence": str(output / "evidence.json"), "timings": timing_path, "checks": checks}


def main():
    """接收可选输出目录并运行演示；输出最终文件路径，失败保留已有临时证据。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=tempfile.mkdtemp(prefix="zen43-demo-", dir="/tmp"))
    args = parser.parse_args()
    print(json.dumps(run_demo(args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
