"""从同修订本地证据生成阅读样张；复用默认正文转换，不发起网络请求。"""

import argparse
import json
from pathlib import Path

from experiments.zen43.core.sections import section_model
from experiments.zen43.core.markdown import render_block


def build_preview(xml_path, result_path, output, diagnostics_path=None):
    """输入原生响应、业务结果、output 和可选内部诊断；按选区生成样张，返回绝对路径，兼容旧证据。"""
    source = json.loads(Path(xml_path).read_text())["data"]["document"]
    result = json.loads(Path(result_path).read_text())
    diagnostics = json.loads(Path(diagnostics_path).read_text()) if diagnostics_path else result
    if source["revision_id"] != result["revision"] or result["warnings"]:
        raise ValueError("来源修订或素材状态不适合生成本次对照")
    blocks, _, positions = section_model(source["content"])
    parts = ["> 阅读形式对照样张：由同修订已取得的 XML 本地转换，采用正文工具相同的 Markdown 优先规则。",
             "> 来源：" + result["article"]]
    ranges = diagnostics["body_ranges"] if "body_ranges" in diagnostics else result["actual_ranges"]
    for actual in ranges:
        left, right = positions[actual["start_block_id"]], positions[actual["end_block_id"]] + 1
        for block in blocks[left:right]:
            rendered = render_block(block, diagnostics.get("body_images", result.get("images", {})))
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
    parser.add_argument("--diagnostics")
    args = parser.parse_args()
    print(build_preview(args.xml, args.result, args.output, args.diagnostics))


if __name__ == "__main__":
    main()
