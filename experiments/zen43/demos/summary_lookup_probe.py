"""只读探索社区摘要的直接定位，不替换摘要工具或引入整年读取兜底。

对固定实测样本比较文章 token、标题、URL、已知摘要文字与日志块定位。
允许检查服务端返回的 excerpt，但只用已保存原文核对样本，不据此承诺一般完整性。
"""

import argparse
import json
from pathlib import Path
import tempfile
import time

from experiments.zen43.core.prototype import Prototype
from experiments.zen43.core.source import daily, write_json, xml_root


def variants(sample, include_controls):
    """按 sample 生成定位参数；include_controls 指是否加入 URL 和已知摘要文字对照，返回列表。"""
    result = [
        ("article_token", "keyword", {"keyword": sample["article_id"]}),
        ("article_title", "keyword", {"keyword": sample["title"]}),
        ("known_log_block", "range", {"start_block_id": sample["log_block_id"],
                                      "end_block_id": sample["log_block_id"]}),
    ]
    if include_controls:
        result += [("article_url", "keyword", {"keyword": sample["url"]}),
                   ("known_summary_text_control", "keyword", {"keyword": "AI协作教练"})]
    return result


def observe(context, sample, label, scope, options):
    """执行一次指定定位，按引用 token 验明文章；返回响应尺寸、计时及原摘要逐字比较结果。"""
    args = ["docs", "+fetch", "--doc", sample["log_url"], "--scope", scope,
            "--doc-format", "xml", "--detail", "with-ids", "--max-depth", "-1"]
    for key, value in options.items():
        args += ["--" + key.replace("_", "-"), value]
    # 使用底层只读封装保留实际 excerpt 供研究；正式工具的拒绝节选规则保持不变。
    document = context.call(args)["data"]["document"]
    content, matches = document.get("content", ""), []
    if content:
        for node in xml_root(content).iter("li"):
            if any(c.get("doc-id") == sample["article_id"] for c in node.iter("cite")):
                text, _ = daily.inline_content(node)
                prefix = "《" + sample["title"] + "》"
                matches.append(text.lstrip()[len(prefix):] if text.lstrip().startswith(prefix) else text)
    return {"variant": label, "article_id": sample["article_id"], "article_date": sample["date"],
            "source": sample["log_url"], "total_seconds": round(time.monotonic() - context.input_started, 3),
            "content_chars": len(content), "target_reference_found": bool(matches),
            "excerpt": "<excerpt" in content, "tips": document.get("tips"),
            "matches": len(matches), "exact_original_summary": sample["summary"] in matches,
            "calls": context.calls, "content_preview": content[:1000]}


def main():
    """读取固定样本并执行 11 项真实只读对照；逐次保存结果，返回输出目录中的证据路径。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=tempfile.mkdtemp(prefix="zen43-summary-lookup-", dir="/tmp"))
    args = parser.parse_args()
    output = Path(args.output).resolve()
    samples = json.loads((Path(__file__).resolve().parents[1] / "data/summary-lookup-samples.json").read_text())
    observations = []
    for index, sample in enumerate(samples):
        for label, scope, options in variants(sample, index == 0):
            row = observe(Prototype(output / "PROTOTYPE-wipe-me-cache"), sample, label, scope, options)
            observations.append(row)
            write_json(output / "results.json", {"samples": samples, "observations": observations})
            print(json.dumps({key: value for key, value in row.items()
                              if key not in {"calls", "content_preview", "source"}}, ensure_ascii=False), flush=True)
    print(str(output / "results.json"))


if __name__ == "__main__":
    main()
