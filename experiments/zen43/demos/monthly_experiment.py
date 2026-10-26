"""按月检索的可丢弃实测：比较首月等待、连续查询累计时间与提前停止的漏检。

只用现有 Prototype 的只读调用；固定样本锚点来自已保存的真实日志。
旧方案读取全部日志，新方案复用已知锚点逐月读取；两者均保留 drive 搜索。
每轮独立空内容缓存，候选只在本轮复用；不计用户在两次提交间的等待。
"""

import argparse
from datetime import date, datetime
import json
from pathlib import Path
import statistics
import tempfile
import time

from experiments.zen43.core.prototype import Prototype
from experiments.zen43.core.source import SPACE, daily, log_entries, write_json
from experiments.zen43.demos.demo import command_purpose


def search_candidates(context, query):
    """用 context 获取 query 的全部原生候选页；返回逐页候选，保留创建时间倒序设置。"""
    pages, token, seen = [], None, set()
    while True:
        args = ["drive", "+search", "--query", query, "--space-ids", SPACE,
                "--sort", "create_time", "--page-size", "20"]
        if token:
            args += ["--page-token", token]
        data = context.call(args)["data"]
        if data.get("notice"):
            raise ValueError("搜索返回 notice，不能把本轮作为完整对照")
        pages.append(data.get("results", []))
        if not data.get("has_more"):
            return pages
        token = data.get("page_token")
        if not token or token in seen:
            raise ValueError("原生续页令牌缺失或重复")
        seen.add(token)


def month_results(records, candidates, month):
    """把 records 限定到 month，再按文章保留最近记录；返回有摘要且命中 candidates 的业务字段。"""
    latest = {}
    for row in records:
        if row["date"][:7] != month:
            continue
        old = latest.get(row["article_id"])
        if old is None or row["date"] > old["date"]:
            latest[row["article_id"]] = row
    results = []
    for key, row in latest.items():
        if not row["summary"].strip() or key not in candidates:
            continue
        result = {field: row[field] for field in ("article_id", "url", "title", "date")}
        candidate = candidates[key]
        if "comment_id" in candidate["result_meta"].get("url", ""):
            result["snippet_status"] = "comment_match_not_delivered"
        else:
            result["snippet"] = candidate.get("summary_highlighted", "")
        results.append(result)
    return sorted(results, key=lambda row: (row["date"], row["article_id"]), reverse=True)


def candidate_index(pages):
    """将 pages 中的原生搜索页按文章标识合并；返回候选映射，不改变原查询含义。"""
    return {item["result_meta"]["token"]: item for page in pages for item in page}


def read_section(context, anchor, today, expected_month=None):
    """按 anchor 读取实际章节并解析；返回日志记录，月份不符或解析有遗漏则停止本次实验。"""
    document = context.fetch(anchor["url"], "section", start_block_id=anchor["block_id"])
    records, unsupported, _ = log_entries(document["content"], anchor["url"], today)
    if unsupported or not records:
        raise ValueError("章节解析不完整或为空，不计为成功实验")
    if expected_month and any(row["date"][:7] != expected_month for row in records):
        raise ValueError("月份锚点已改变或读取范围跨月，需更新实验输入")
    return records


def diagnostic_calls(context):
    """把 context 的调用转换为可交付诊断；返回作用与计时，原生续页令牌仅留本地证据。"""
    calls = []
    for call in context.calls:
        args = list(call["arguments"])
        if "--page-token" in args:
            args[args.index("--page-token") + 1] = "<本地原生续页令牌>"
        purpose = command_purpose(call["arguments"], "search")
        if args[:2] == ["drive", "+search"]:
            purpose = "按创建时间倒序获取一页原生候选；完整候选将在后续月份复用。"
        elif "--scope" in args and args[args.index("--scope") + 1] == "section":
            purpose = "直接读取已知锚点对应的日志章节，解析当前需要的月份记录。"
        calls.append({"command": args, "purpose": purpose, "seconds": call["seconds"],
                      "elapsed_seconds": call["elapsed_seconds"], "content_chars": call["content_chars"],
                      "revision": call["revision"]})
    return calls


def run_strategy(output, label, query, months, fixture, today):
    """实测 label 策略的一整次探索；返回逐月时间、业务结果、调用及内部对照记录。"""
    context = Prototype(output / (label + "-PROTOTYPE-wipe-me"))
    started = time.monotonic()
    records = []
    if label == "all_logs":
        context.pool()
        records = context.log_records
    pages = search_candidates(context, query)
    candidates = candidate_index(pages)
    search_seconds = sum(c["seconds"] for c in context.calls if c["arguments"][:2] == ["drive", "+search"])
    cumulative, stages = 0, []
    recent = []
    if label == "monthly":
        recent = read_section(context, fixture["recent"], today)
    for index, month in enumerate(months):
        # 首月包含初始化、候选检索与近期日志；以后每月仅计该次提交的处理时间。
        if index:
            started = time.monotonic()
            context.input_started = started
        before_calls = 0 if index == 0 else len(context.calls)
        if label == "monthly":
            records = read_section(context, fixture["months"][month], today, month) + recent
        results = month_results(records, candidates, month)
        elapsed = time.monotonic() - started
        cumulative += elapsed
        stages.append({"month": month, "seconds": round(elapsed, 4),
                       "cumulative_seconds": round(cumulative, 4), "results": results,
                       "new_calls": len(context.calls) - before_calls,
                       "cumulative_calls": len(context.calls)})
        print(f"{query} {label} {month}: {cumulative:.3f}s, {len(results)} hits", flush=True)
    write_json(output / (label + "-raw-calls.json"), context.calls)
    write_json(output / (label + "-search-pages.json"), pages)
    result = {"strategy": label, "search_seconds": round(search_seconds, 3),
              "native_pages": len(pages), "native_candidates": len(candidates),
              "stages": stages, "calls": diagnostic_calls(context)}
    write_json(output / (label + "-result.json"), result)
    return result, pages, records


def early_stop_check(pages, records, months):
    """回放按已匹配社区日期跨月即停的假设；返回每月漏掉的基准命中，不把模拟时间当实测。"""
    candidates = candidate_index(pages)
    latest_dates = {}
    for row in records:
        key = row["article_id"]
        latest_dates[key] = max(latest_dates.get(key, ""), row["date"])
    checks = []
    for month in months:
        expected = {r["article_id"]: r for r in month_results(records, candidates, month)}
        observed, stopped_at, boundary = set(), len(pages), None
        for number, page in enumerate(pages, 1):
            observed.update(item["result_meta"]["token"] for item in page)
            boundary = next((item["result_meta"]["token"] for item in page
                             if latest_dates.get(item["result_meta"]["token"], "9999")[:7] < month), None)
            if boundary:
                stopped_at = number
                break
        missed = sorted(set(expected) - observed)
        checks.append({"month": month, "stop_page": stopped_at, "total_pages": len(pages),
                       "expected": len(expected), "found_before_stop": len(expected) - len(missed),
                       "missed": [expected[key] for key in missed],
                       "boundary_article": boundary, "boundary_date": latest_dates.get(boundary)})
    return checks


def summarize(trials, months):
    """聚合 trials 每月累计时间及配对差异；返回中位数、范围与各轮首次反超月份数。"""
    rows = []
    for index, month in enumerate(months):
        old = [t["all_logs"]["stages"][index]["cumulative_seconds"] for t in trials]
        new = [t["monthly"]["stages"][index]["cumulative_seconds"] for t in trials]
        progressive = [progressive_estimate(t, index) for t in trials]
        rows.append({"months_searched": index + 1, "through_month": month,
                     "all_logs_median": round(statistics.median(old), 3),
                     "monthly_median": round(statistics.median(new), 3),
                     "progressive_estimate_median": round(statistics.median(progressive), 3),
                     "all_logs_range": [min(old), max(old)], "monthly_range": [min(new), max(new)],
                     "paired_saving_median": round(statistics.median(a-b for a,b in zip(old,new)), 3)})
    crossover = []
    for trial in trials:
        crossover.append(next((i+1 for i in range(len(months)) if
            trial["monthly"]["stages"][i]["cumulative_seconds"] > trial["all_logs"]["stages"][i]["cumulative_seconds"]), None))
    return {"rows": rows, "first_slower_month_count_per_trial": crossover}


def progressive_estimate(trial, index):
    """按 trial 已测调用估算渐进扫描到 index 月的成本；保留候选，仅扣除尚不取的原生页耗时。"""
    # 这是成本拆分回放：完整候选的本地处理耗时仍保留，不冒充独立运行的端到端实测。
    calls = [c for c in trial["monthly"]["calls"] if c["command"][:2] == ["drive", "+search"]]
    pages_needed = max(c["stop_page"] for c in trial["early_stop"][:index+1])
    remaining_seconds = sum(c["seconds"] for c in calls[pages_needed:])
    return trial["monthly"]["stages"][index]["cumulative_seconds"] - remaining_seconds


def build_page(path, report):
    """将 report 的实测状态嵌入独立 HTML；返回文件路径，页面只回放数据、不发网络请求。"""
    payload = json.dumps(report, ensure_ascii=False).replace("<", "\\u003c")
    html = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>按月检索耗时实验</title><style>body{font:16px/1.7 system-ui;color:#22313b;background:#f5f7f8;max-width:1100px;margin:36px auto;padding:0 24px}section{background:white;padding:24px;margin:18px 0;border-radius:12px}button,select{font:inherit;padding:8px 14px;margin:4px;border:1px solid #9fb5be;border-radius:6px;background:white}table{border-collapse:collapse;width:100%}th,td{text-align:left;border-bottom:1px solid #dce5e8;padding:8px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f2f5f6;padding:12px;max-height:450px;overflow:auto;font-size:13px}.accent{color:#215b6c;font-weight:600}.muted{color:#61717b}</style>
<body><h1>按月检索耗时实验</h1><p>验证：只查头一两个月能省多少等待？连续查到第几个月，逐月读取的累计时间反而更长？</p>
<p class="muted">可丢弃实验 · 真实只读调用回放。保留 drive +search 原查询，按月日志筛选；不设对外分页。按创建时间排序仅用于实验，不作为社区日期边界。</p>
<section><p id="method"></p><button onclick="setMonth(0)">只查当月</button><button onclick="setMonth(1)">查前两个月</button><button onclick="setMonth(selected+1)">再往前查一个月</button><button onclick="jumpCross()">查看首次反超</button><label>实测轮次 <select id="trial" onchange="render()"></select></label><h2 id="state"></h2><p id="verdict" class="accent"></p><div id="table"></div></section>
<section><h2>该月结果与提前停止检查</h2><p id="recall"></p><p id="stop"></p><details><summary>实际业务结果（含命中片段）</summary><pre id="results"></pre></details><details><summary>提前停止漏掉的文章</summary><pre id="missed"></pre></details></section>
<section><h2>本次逐月提交的内部动作</h2><p>逐命令说明、单次耗时与从本次输入起累计时间；与 Agent 业务结果分开。</p><div id="commands"></div></section>
<script>const evidence=PAYLOAD;let selected=0;
/** 选择累计探索到的月份；输入零起点索引，无返回值。 */
function setMonth(index){selected=Math.max(0,Math.min(index,evidence.months.length-1));render();}
/** 跳到中位数首次反超的位置；无参数，无返回值。 */
function jumpCross(){const index=evidence.summary.rows.findIndex(r=>r.monthly_median>r.all_logs_median);setMonth(index<0?evidence.months.length-1:index);}
/** 根据已选月份和轮次渲染实际证据；无参数，无返回值，不执行网络请求。 */
function render(){const trial=evidence.trials[Number(document.getElementById('trial').value)||0], row=evidence.summary.rows[selected], stage=trial.monthly.stages[selected], stop=trial.early_stop[selected];document.getElementById('state').textContent='已查 '+(selected+1)+' 个月，当前查看 '+stage.month;document.getElementById('verdict').textContent='累计中位数：一次读全 '+row.all_logs_median.toFixed(3)+' 秒；逐月读取 '+row.monthly_median.toFixed(3)+' 秒。配对节省 '+row.paired_saving_median.toFixed(3)+' 秒（负数表示更慢）。';const table=document.createElement('table');const head=document.createElement('tr');['累计月份','一次读全 / 秒','逐月读取 / 秒','提前停（估算）/ 秒'].forEach(v=>{const th=document.createElement('th');th.textContent=v;head.append(th);});table.append(head);evidence.summary.rows.slice(0,selected+1).forEach(r=>{const tr=document.createElement('tr');[r.months_searched+'（至 '+r.through_month+'）',r.all_logs_median.toFixed(3),r.monthly_median.toFixed(3),r.progressive_estimate_median.toFixed(3)].forEach(v=>{const td=document.createElement('td');td.textContent=v;tr.append(td);});table.append(tr);});document.getElementById('table').replaceChildren(table);document.getElementById('recall').textContent='本轮两方案月内结果一致：'+stage.results.length+' 条；本月新增 '+stage.new_calls+' 次调用。完整性以本次完整原生候选和已解析日志交集为基准，不代表全库召回保证。';document.getElementById('stop').textContent='假设遇到较早社区日期便停止：第 '+stop.stop_page+' / '+stop.total_pages+' 页停下，取得 '+stop.found_before_stop+' / '+stop.expected+' 条，漏掉 '+stop.missed.length+' 条。此项为真实候选序列回放。表中提前停耗时由实测命令成本拆分估算，非独立端到端实测；本样本未漏检也不证明规则可靠。';document.getElementById('results').textContent=JSON.stringify({month:stage.month,results:stage.results},null,2);document.getElementById('missed').textContent=JSON.stringify(stop.missed,null,2);const commands=trial.monthly.calls.slice(selected?trial.monthly.stages[selected-1].cumulative_calls:0,stage.cumulative_calls);const panel=document.getElementById('commands');panel.replaceChildren();commands.forEach((c,i)=>{const div=document.createElement('div'),p=document.createElement('p'),pre=document.createElement('pre');p.textContent='命令 '+(i+1)+'：'+c.purpose+' 单次 '+c.seconds.toFixed(3)+' 秒；本次输入起累计 '+c.elapsed_seconds.toFixed(3)+' 秒。';pre.textContent=JSON.stringify({command:c.command,revision:c.revision,content_chars:c.content_chars},null,2);div.append(p,pre);panel.append(div);});}
evidence.trials.forEach((t,i)=>{const o=document.createElement('option');o.value=i;o.textContent='第 '+(i+1)+' 轮（'+t.order.join(' → ')+')';document.getElementById('trial').append(o);});document.getElementById('method').textContent=evidence.method;render();</script></body></html>'''.replace("PAYLOAD", payload)
    Path(path).write_text(html, encoding="utf-8")
    return str(Path(path).resolve())


def main():
    """读取实验参数，交替实测两方案并输出 JSON/HTML；返回无值，失败保留已有原始证据。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", default="RAG")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--months", type=int, default=17)
    parser.add_argument("--output", default=tempfile.mkdtemp(prefix="zen43-monthly-", dir="/tmp"))
    args = parser.parse_args()
    if args.rounds < 1 or args.months < 1:
        parser.error("轮数和月份数必须为正整数")
    today = datetime.now(daily.reading_timezone()).date()
    fixture = json.loads((Path(__file__).resolve().parents[1] / "data/monthly-fixtures.json").read_text())
    months = sorted((m for m in fixture["months"] if m <= today.strftime("%Y-%m")), reverse=True)[:args.months]
    if not months or months[0] != today.strftime("%Y-%m"):
        raise ValueError("实验月份锚点不含当月，请重新采集样本")
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    trials = []
    for number in range(args.rounds):
        folder = output / f"round-{number+1}"
        folder.mkdir()
        order = ["all_logs", "monthly"] if number % 2 == 0 else ["monthly", "all_logs"]
        trial = {"order": order}
        for label in order:
            result, pages, records = run_strategy(folder, label, args.query, months, fixture, today)
            trial[label] = result
            if label == "all_logs":
                baseline_records = records
            else:
                monthly_pages = pages
        trial["early_stop"] = early_stop_check(monthly_pages, baseline_records, months)
        for old, new in zip(trial["all_logs"]["stages"], trial["monthly"]["stages"]):
            assert old["results"] == new["results"], "两方案结果不同，可能来源或搜索索引变化；不发布可比耗时"
        trials.append(trial)
        write_json(output / "partial-trials.json", trials)
    report = {"recorded_at": datetime.now(daily.reading_timezone()).isoformat(), "query": args.query,
              "months": months, "trials": trials, "summary": summarize(trials, months),
              "method": f"查询 {args.query}，{args.rounds} 轮交替次序。每轮两方案各用空的本地内容缓存；逐月方案复用预先观测到的章节锚点，首月完整获取原生候选，后续月份复用本轮候选。未清除或控制飞书服务端缓存。计时包含本地解析，不含人等待。"}
    write_json(output / "monthly-results.json", report)
    print(json.dumps({"page": build_page(output / "monthly-experiment.html", report),
                      "results": str(output / "monthly-results.json"), "summary": report["summary"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
