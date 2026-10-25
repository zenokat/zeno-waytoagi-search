"""第五工具的真实只读演示：核对实际范围、Markdown 原文及原位置图片。

仅重新执行已有三个正文场景，完整复用前四工具的证据及计时。
审阅材料嵌入独立 HTML；业务 JSON 仍只返回正文文件路径、范围与素材状态。
"""

import argparse
import base64
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import tempfile
import time
from zoneinfo import ZoneInfo

from demo import assert_images, build_html, command_purpose
from article_cache import load_article_cache
from prototype import Prototype, section_model, write_json

NAMES = ["正文读取", "完整章节范围", "全文读取"]


def markdown_headings(content):
    """输入 Markdown 原文，返回代码围栏之外的标题文字列表；仅用于本次真实样本核对。"""
    headings, fence = [], None
    for line in content.splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            token = marker[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        heading = re.match(r"^#{1,6} (.+)$", line)
        if heading and fence is None:
            headings.append(heading[1].replace("**", "").replace("\\`", "`").strip())
    return headings


def review_body(result, context, arguments):
    """核对 result 文件与 context 原生响应，按 arguments 标示选择；返回仅供演示的正文与内嵌图片。"""
    _, snapshot = load_article_cache(context, arguments["article"])

    def saved_document(scope, format, **options):
        """按 scope、format、options 读取本次已用过的原生缓存响应；返回文档，不追加网络或诊断动作。"""
        key = json.dumps({"scope": scope, "format": format, "max_depth": -1, **options}, sort_keys=True)
        return snapshot["responses"][key]

    full = saved_document("full", "xml")
    blocks, units, _ = section_model(full["content"])
    positions = {block.get("id"): index for index, block in enumerate(blocks)}
    selected_units, image_tokens = [], []
    for actual in result["actual_ranges"]:
        left, right = positions[actual["start_block_id"]], positions[actual["end_block_id"]] + 1
        included = [unit for unit in units if left <= unit["start"] < right]
        assert actual["unit_ids"] == [unit["id"] for unit in included]
        assert actual["top_level_blocks"] == right - left
        selected_units.extend(included)
        image_tokens.extend(image.get("src") for block in blocks[left:right] for image in block.iter("img"))
    # 先按实际 XML 选区确定图片，避免用“文件存在”代替选区正确性核对。
    expected_tokens = list(dict.fromkeys(image_tokens))
    assert list(result["images"]) == expected_tokens
    assert_images(result)
    markdown = Path(result["markdown_path"]).read_text(encoding="utf-8")
    raw_parts = []
    for actual in result["actual_ranges"]:
        body = saved_document("full", "markdown") if result["scope"] == "full" else saved_document(
            "range", "markdown", start_block_id=actual["start_block_id"], end_block_id=actual["end_block_id"])
        assert body["revision_id"] == result["revision"]
        content = body["content"]
        for token, image in result["images"].items():
            content = content.replace("https://feishu.cn/file/" + token, str(Path(image["saved_path"]).resolve()))
        raw_parts.append(content)
    assert markdown == "\n\n".join(raw_parts)
    assert markdown_headings(markdown) == [unit["title"] for unit in selected_units if "title" in unit]
    paths = [image["saved_path"] for image in result["images"].values()]
    assert re.findall(r"!\[[^\]]*\]\((/[^\n)]+)\)", markdown) == paths
    images = []
    for item in result["images"].values():
        content = Path(item["saved_path"]).read_bytes()
        assert len(content) == item["size_bytes"]
        images.append({"path": item["saved_path"], "sha256": hashlib.sha256(content).hexdigest(),
                       "size_bytes": len(content),
                       "data_url": "data:" + item["content_type"] + ";base64," + base64.b64encode(content).decode("ascii")})
    return {"markdown": markdown, "units": [
        {"unit_id": unit["id"], "kind": unit["kind"],
         **({"title": unit["title"]} if "title" in unit else {}),
         "explicitly_selected": unit["id"] in arguments.get("unit_ids", [])}
        for unit in selected_units], "images": images}


def capture(output, previous):
    """按 previous 的实际输入在 output 临时缓存下重读正文；返回带完整审阅材料的新场景。"""
    context = Prototype(output / "PROTOTYPE-wipe-me-cache")
    result = context.read(**previous["input"])
    total = round(time.monotonic() - context.input_started, 3)
    write_json(context.run_dir / "calls.json", context.calls)
    write_json(context.run_dir / "diagnostics.json", context.diagnostics)
    result_path = write_json(context.run_dir / "result.json", result)
    review = review_body(result, context, previous["input"])
    native = [{"command": call["arguments"], "purpose": command_purpose(call["arguments"], "read"),
               "revision": call["revision"], "seconds": call["seconds"],
               "elapsed_seconds": call["elapsed_seconds"], "content_chars": call["content_chars"]}
              for call in context.calls]
    scenario = {"name": previous["name"], "explain": previous["explain"], "input": previous["input"],
                "output": result, "exact_output": True, "diagnostics": context.diagnostics,
                "native": native, "local_result": result_path, "total_seconds": total,
                "body_review": review,
                "recorded_at": datetime.now(ZoneInfo("Asia/Singapore")).isoformat(timespec="seconds")}
    print(previous["name"] + "完成", flush=True)
    return scenario


def run_demo(output, base):
    """从 base HTML 复用证据，仅重跑正文；在 output 写入新演示和核对账本，返回其路径。"""
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    evidence = json.JSONDecoder().raw_decode(Path(base).read_text().split("const evidence=", 1)[1])[0]
    previous = [next(item for item in evidence["scenarios"] if item["name"] == name) for name in NAMES]
    scenarios = [capture(output, item) for item in previous]
    overlap, bounded, complete = scenarios
    assert len({item["output"]["revision"] for item in scenarios}) == 1
    for item, blocks, units, images in [(overlap, 52, 6, 4), (bounded, 12, 2, 1), (complete, 130, 19, 11)]:
        assert sum(actual["top_level_blocks"] for actual in item["output"]["actual_ranges"]) == blocks
        assert len(item["body_review"]["units"]) == units
        assert len(item["body_review"]["images"]) == images
    assert len(overlap["output"]["actual_ranges"]) == 1
    assert overlap["body_review"]["units"][0]["unit_id"] == overlap["input"]["unit_ids"][0]
    assert len(bounded["body_review"]["units"]) == 2
    assert bounded["body_review"]["units"][-1]["unit_id"] == bounded["input"]["end_unit_id"]
    assert bounded["output"]["actual_ranges"][0]["end_block_id"] != bounded["input"]["end_unit_id"]
    assert complete["output"]["scope"] == "full"
    comparisons = []
    for old, new in zip(previous, scenarios):
        # 同修订时进一步逐字比较旧正文与图片；路径是每次运行产生的，先归一化再比较。
        same_revision = old["output"]["revision"] == new["output"]["revision"]
        comparison = {"name": new["name"], "same_revision_as_previous": same_revision,
                      "markdown_matches_native_except_local_image_paths": True,
                      "headings_match_selected_units_in_order": True,
                      "images_match_selected_xml_and_markdown_in_order": True}
        old_path = Path(old["output"]["markdown_path"])
        if same_revision and old_path.is_file():
            old_body, new_body = old_path.read_text(), new["body_review"]["markdown"]
            for token in new["output"]["images"]:
                old_body = old_body.replace(old["output"]["images"][token]["saved_path"], token)
                new_body = new_body.replace(new["output"]["images"][token]["saved_path"], token)
                assert Path(old["output"]["images"][token]["saved_path"]).read_bytes() == Path(new["output"]["images"][token]["saved_path"]).read_bytes()
            assert old_body == new_body
            assert old["output"]["actual_ranges"] == new["output"]["actual_ranges"]
            comparison["same_body_ranges_and_image_bytes_as_previous"] = True
        comparisons.append(comparison)
    replacements = {item["name"]: item for item in scenarios}
    merged = [replacements.get(item["name"], item) for item in evidence["scenarios"]]
    unchanged = sum(item["name"] not in NAMES for item in evidence["scenarios"])
    assert [item for item in merged if item["name"] not in NAMES] == [item for item in evidence["scenarios"] if item["name"] not in NAMES]
    checks = dict(evidence["checks"])
    checks.update({"多章合并与图片": "父章与子章合并为 1 段；父章及 5 个子章仅出现一次，4 张图片按原位置嵌入",
                   "范围终点": "第一步至第二步共 2 个完整子章、12 个顶层块，含第二步末尾图片；未包含第三步",
                   "全文与图片": "130 个顶层块、18 个原标题和开篇正文，11 张文内图片已保存并按原位置嵌入",
                   "正文交付核对": "JSON 为实际业务返回；文件逐字等于原生 Markdown 替换本地图片路径，原标题顺序及图片顺序一致",
                   "正文验证时间": scenarios[-1]["recorded_at"]})
    ledger = {"recorded_at": scenarios[-1]["recorded_at"], "source": "本轮真实只读调用；前四工具完整复用旧证据与计时",
              "unchanged_other_scenarios": unchanged, "scenarios": comparisons,
              "limits": "同一篇真实文章的三个场景；未扩展系统性异常、混杂正文归属或大文档完整性；未做浏览器视觉验收。"}
    write_json(output / "body-checks.json", ledger)
    write_json(output / "evidence.json", {"checks": checks, "scenarios": merged})
    write_json(output / "observed-checks.json", checks)
    write_json(output / "timings.json", {"recorded_at": evidence["checks"]["验证时间"],
               "body_recorded_at": scenarios[-1]["recorded_at"], "unit": "seconds",
               "definition": "仅第五工具本轮重新联网；前四工具的采集、重放说明与计时保持。各场景独立计时。",
               "scenarios": [{"name": item["name"], "input": item["input"], "total_seconds": item["total_seconds"],
                              "commands": item["native"]} for item in merged]})
    return {"html": build_html(output / "demo.html", merged, checks), "evidence": str(output / "evidence.json"),
            "checks": str(output / "body-checks.json")}


def main():
    """解析已有 HTML 与临时输出目录，真实执行三个正文场景；打印产物路径，无返回值。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=str(Path(__file__).with_name("demo.html")))
    parser.add_argument("--output", default=tempfile.mkdtemp(prefix="zen43-body-demo-", dir="/tmp"))
    args = parser.parse_args()
    print(json.dumps(run_demo(args.output, args.base), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
