"""ZEN-43 五工具命令行入口；只解析参数、调用工具并交付业务 JSON。

从仓库根运行 python3 -m experiments.zen43.prototype，也支持直接执行本文件。
实验未发布为正式 Skill，异常协议仍由 ZEN-44 推导。
"""

import argparse
import json
from pathlib import Path
import re
import sys
import tempfile
from xml.etree import ElementTree as ET

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from experiments.zen43.core.prototype import Prototype
from experiments.zen43.core.source import SAMPLE, write_json


def public_error(error):
    """将内部 error 映射为调用方可理解的错误说明；返回文本，不包含 CLI、内部标识或诊断路径。"""
    if isinstance(error, KeyError):
        return "所选读取单位不存在，请重新获取文章结构后再试。"
    if isinstance(error, OSError):
        return "本地文件读取或保存失败，本次未完成交付。"
    message = str(error)
    if isinstance(error, ET.ParseError) or re.search(r"cli|block_id|excerpt|tips|原生|ZEN-44|scope|token", message, re.I):
        return "来源内容暂无法完整读取，本次未完成交付。"
    return message


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
    parser.add_argument("--parent-id")
    parser.add_argument("--unit-ids", nargs="+")
    parser.add_argument("--start-unit-id")
    parser.add_argument("--end-unit-id")
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
            result = prototype.structure(args.article, args.parent_id, args.depth)
        elif args.tool == "locate":
            result = prototype.locate(args.article, args.keywords)
        else:
            result = prototype.read(args.article, args.unit_ids, args.start_unit_id, args.end_unit_id)
        write_json(prototype.run_dir / "calls.json", prototype.calls)
        write_json(prototype.run_dir / "diagnostics.json", prototype.diagnostics)
        # 五个入口均直接交付业务 JSON；调用证据和诊断保存在上面的临时文件。
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, KeyError, OSError, ET.ParseError) as error:
        write_json(prototype.run_dir / "calls.json", prototype.calls)
        write_json(prototype.run_dir / "error.json", {"type": type(error).__name__, "error": str(error)})
        print(json.dumps({"status": "prototype_error", "error": public_error(error)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
