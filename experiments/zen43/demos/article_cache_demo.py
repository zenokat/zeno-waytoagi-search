"""真实只读核对后三工具共用缓存的请求数，不修改前四工具的已确认业务返回。"""

import argparse
from datetime import datetime
import tempfile
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from experiments.zen43.core.prototype import Prototype
from experiments.zen43.core.source import write_json
from experiments.zen43.demos.demo import command_purpose


def run_demo(output):
    """输入新的 output 目录，真实执行定位、结构、正文与再读；返回包含原生计时和结果的账本路径。"""
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / "PROTOTYPE-wipe-me-cache").exists():
        raise ValueError("请使用新的输出目录，避免把已有缓存当作首次空缓存")
    article = "https://waytoagi.feishu.cn/wiki/MiOcwgmQqiCEf7kMUN0c53j2n8f"
    chapter = "Fbu3dBepXo7sQRxL4TncS6GpnPk"
    steps = [
        ("全文检索", "locate", {"article": article, "keywords": ["营销类"]}),
        ("篇章结构获取", "structure", {"article": article}),
        ("正文读取", "read", {"article": article, "unit_ids": [chapter]}),
        ("相同章节再次读取", "read", {"article": article, "unit_ids": [chapter]}),
    ]
    scenarios = []
    for name, method, arguments in steps:
        context = Prototype(output / "PROTOTYPE-wipe-me-cache")
        result = getattr(context, method)(**arguments)
        total = round(time.monotonic() - context.input_started, 3)
        write_json(context.run_dir / "calls.json", context.calls)
        path = write_json(context.run_dir / "result.json", result)
        native = [{"command": call["arguments"], "purpose": command_purpose(call["arguments"], method),
                   "seconds": call["seconds"], "elapsed_seconds": call["elapsed_seconds"]}
                  for call in context.calls]
        scenarios.append({"name": name, "input": arguments, "output": result, "total_seconds": total,
                          "local_result": path, "diagnostics": context.diagnostics, "native": native})
        print(name + "完成：" + str(len(context.calls)) + " 次真实请求", flush=True)
    assert scenarios[1]["native"] == []
    assert len(scenarios[2]["native"]) == 2
    assert scenarios[3]["native"] == []
    assert Path(scenarios[2]["output"]["markdown_path"]).read_text() == Path(scenarios[3]["output"]["markdown_path"]).read_text()
    assert len({s["output"]["revision"] for s in scenarios}) == 1
    ledger = {"recorded_at": datetime.now(ZoneInfo("Asia/Singapore")).isoformat(timespec="seconds"),
              "source": "本轮真实只读链路；首次独立空缓存；其后按输入顺序共用同一文章缓存。",
              "ttl_days": 7, "scenarios": scenarios,
              "checks": {"structure_after_locate_zero_requests": True, "body_no_repeated_full_xml": True,
                         "same_body_again_zero_requests": True, "same_revision_all_steps": True},
              "limits": "缓存按实际格式、范围、关键词复用；新格式或新关键词仍需首次获取。七天内不自动探测更新，遇到新修订才失效旧快照。单机单用户原型。"}
    return write_json(output / "article-cache-checks.json", ledger)


def main():
    """解析可选临时输出目录并执行真实缓存链路；打印账本路径，无返回值。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=tempfile.mkdtemp(prefix="zen43-article-cache-demo-", dir="/tmp"))
    args = parser.parse_args()
    print(run_demo(args.output))


if __name__ == "__main__":
    main()
