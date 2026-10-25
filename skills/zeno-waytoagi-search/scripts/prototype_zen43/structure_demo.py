"""第四工具的真实只读演示：整篇、局部、叶子、开篇与无标题全文。

复用现有 HTML 内的其他工具证据，只替换结构场景；各工具采集时间分别标注。
另外比较原生 outline 深度与 section 范围；原生响应仅保存到临时目录。
"""

import argparse
from datetime import datetime
import json
from pathlib import Path
import tempfile
import time
from zoneinfo import ZoneInfo

from demo import build_html, command_purpose, section_nodes
from prototype import Prototype, SAMPLE, section_model, write_json, xml_root

NAMES = ["整篇默认一层", "篇章结构获取", "局部默认一层", "局部展开两层",
         "叶子章节结构", "开篇正文结构", "无标题文章结构", "无标题单位下钻"]
NO_HEADINGS = "https://waytoagi.feishu.cn/wiki/W1Lkw5pjyijyRHk99rMcy3VHnXf"


def capture(output, name, explanation, **arguments):
    """在 output 缓存下真实调用结构工具；输入场景说明和业务参数，返回场景与调用上下文。"""
    context = Prototype(output / "PROTOTYPE-wipe-me-cache")
    result = context.structure(**arguments)
    total = round(time.monotonic() - context.input_started, 3)
    write_json(context.run_dir / "calls.json", context.calls)
    write_json(context.run_dir / "diagnostics.json", context.diagnostics)
    path = write_json(context.run_dir / "result.json", result)
    native = [{"command": call["arguments"], "purpose": command_purpose(call["arguments"], "structure"),
               "revision": call["revision"], "seconds": call["seconds"],
               "elapsed_seconds": call["elapsed_seconds"], "content_chars": call["content_chars"]}
              for call in context.calls]
    scenario = {"name": name, "explain": explanation, "input": arguments, "output": result,
                "exact_output": True, "diagnostics": context.diagnostics, "native": native,
                "local_result": path, "total_seconds": total,
                "recorded_at": datetime.now(ZoneInfo("Asia/Singapore")).isoformat(timespec="seconds")}
    print(name + "完成", flush=True)
    return scenario, context


def check_native_scopes(output, units, parent, revision):
    """比较 SAMPLE 的原生标题上限和 section 边界；输入完整单位、父章及修订，返回证据字典。"""
    context = Prototype(output / "PROTOTYPE-wipe-me-cache")
    observations = []
    for depth in [1, 2, 3]:
        document = context.fetch(SAMPLE, "outline", max_depth=depth)
        assert document["revision_id"] == revision
        ids = [e.get("id") for e in xml_root(document["content"]).iter() if e.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}]
        expected = [u["id"] for u in units if u.get("rank", 7) <= depth]
        assert ids == expected
        observations.append({"scope": "outline", "max_depth": depth, "heading_ids": ids,
                             "content_chars": len(document["content"])})
    section = context.fetch(SAMPLE, "section", start_block_id=parent["id"])
    assert section["revision_id"] == revision
    blocks, selected, _ = section_model(section["content"])
    expected = [u["id"] for u in units if u["id"] == parent["id"] or parent["id"] in u["parents"]]
    assert [u["id"] for u in selected] == expected
    assert len(blocks) == parent["end"] - parent["start"]
    observations.append({"scope": "section", "start_block_id": parent["id"], "heading_ids": expected,
                         "top_level_blocks": len(blocks)})
    outline = context.fetch(NO_HEADINGS, "outline")
    observations.append({"scope": "outline", "article": NO_HEADINGS, "revision": outline["revision_id"],
                         "content": outline["content"]})
    assert not list(xml_root(outline["content"]).iter("h1"))
    return {"article": SAMPLE, "revision": revision, "observations": observations, "calls": context.calls,
            "limits": "两篇真实文章；容器内标题与 H2→H4→H6 仅离线构造验证。outline 不含开篇正文信息；未证明可替代 full。"}


def run_demo(output, base):
    """读取 base HTML 证据，在 output 保存八个新结构场景与合并演示；返回文件路径及核对结果。"""
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    evidence = json.JSONDecoder().raw_decode(Path(base).read_text().split("const evidence=", 1)[1])[0]
    scenarios = []
    default, first = capture(output, NAMES[0], "省略 depth，默认只看最外层；开篇单独作为读取单位，原文编号标题原样保留。", article=SAMPLE)
    scenarios.append(default)
    deep, context = capture(output, NAMES[1], "展开两层：原文 H1 → H3 是实际父子两层，不按 H 数字截断。", article=SAMPLE, depth=2)
    scenarios.append(deep)
    assert default["output"]["revision"] == deep["output"]["revision"]
    units = context.diagnostics["structure_units"]
    assert [n["unit_id"] for n in default["output"]["outline"]] == [u["id"] for u in units if u["depth"] == 1]
    assert [n["unit_id"] for n in section_nodes(deep["output"]["outline"])] == [u["id"] for u in units if u["depth"] <= 2]
    parent = next(u for u in units if u["title"] == "动手安装吧")
    children = [u for u in units if parent["id"] in u["parents"]]
    assert len(children) == 2 and all(u["rank"] == 3 and u["depth"] == 2 for u in children)
    for name, depth in [(NAMES[2], None), (NAMES[3], 2)]:
        arguments = {"article": SAMPLE, "parent_id": parent["id"]}
        if depth is not None:
            arguments["depth"] = depth
        scenario, _ = capture(output, name, "从“动手安装吧”向下看；不重复父章，也不包含其他兄弟章。本样本的两节安装说明没有更深子标题，因此展开一层和两层相同。", **arguments)
        assert scenario["output"]["revision"] == deep["output"]["revision"]
        assert [n["unit_id"] for n in section_nodes(scenario["output"]["outline"])] == [u["id"] for u in children]
        scenarios.append(scenario)
    for name, section, explanation in [
        (NAMES[4], children[0]["id"], "安装 MarkItDown 没有子标题，返回空目录；这不表示本章没有正文。"),
        (NAMES[5], "@opening", "开篇正文是可读取单位，但不是原文标题，也没有下级章节。"),
    ]:
        scenario, _ = capture(output, name, explanation, article=SAMPLE, parent_id=section)
        assert scenario["output"]["outline"] == []
        assert scenario["output"]["revision"] == deep["output"]["revision"]
        scenarios.append(scenario)
    no_headings, _ = capture(output, NAMES[6], "真实文章“云舒：和 AI 鸡同鸭讲…”没有章节标题，整体用 @document 表示，不把加粗文字冒充目录。", article=NO_HEADINGS)
    assert [n["unit_id"] for n in no_headings["output"]["outline"]] == ["@document"]
    scenarios.append(no_headings)
    empty, _ = capture(output, NAMES[7], "指定 @document 后没有下级标题；可用同一标识读取整篇无标题正文。", article=NO_HEADINGS, parent_id="@document")
    assert empty["output"]["outline"] == [] and empty["output"]["revision"] == no_headings["output"]["revision"]
    scenarios.append(empty)
    # 直接复用上轮已确认全文检索返回的父章与叶子标识，核对两工具衔接；不改第三工具证据。
    located = next(s for s in evidence["scenarios"] if s["name"] == "仅局部正文命中")["output"]
    assert located["revision"] == deep["output"]["revision"]
    assert [n["unit_id"] for n in section_nodes(located["match_tree"])] == [parent["id"], children[0]["id"]]
    for scenario in scenarios:
        assert len(scenario["native"]) == 1
        assert scenario["native"][0]["elapsed_seconds"] <= scenario["total_seconds"]
        complete_units = scenario["diagnostics"]["structure_units"]
        parent_ids = {u["parents"][-1] for u in complete_units if u["parents"]}
        selected = scenario["input"].get("parent_id")
        expected_parent = {"unit_id": selected, "has_children": selected in parent_ids} if selected else None
        assert scenario["output"]["parent"] == expected_parent
        for node in section_nodes(scenario["output"]["outline"]):
            assert node["has_children"] == (node["unit_id"] in parent_ids)
            assert set(node) == {"title", "unit_id", "kind", "has_children", "children"}
    capability = check_native_scopes(output, units, parent, deep["output"]["revision"])
    capability["observed_at"] = scenarios[-1]["recorded_at"]
    write_json(output / "structure-capability-check.json", capability)
    checks = evidence["checks"]
    checks.update({"章节层级": "整篇默认 11 个外层单位；两层 19 个单位，H1 → H3 按父子两层处理",
                   "结构局部范围": "动手安装吧仅返回两个子章；不重复基节点、不含兄弟章；叶子下钻为空",
                   "结构特殊单位": "PDF 开篇为 @opening；真实无标题文章为 @document；特殊单位下无子标题",
                   "结构读取衔接": "沿用上轮全文检索的父章与子章 unit_id，分别取得局部目录和叶子空目录；本轮未执行第五工具",
                   "结构返回字段": "节点含 title、unit_id、kind、has_children、children；内部位置留在诊断；类型为 section/opening/document",
                   "结构子章标识": "根据完整原文判断；默认一层的父章标有子章但未展开，两层的真实叶子标无子章",
                   "结构查询起点": "整篇 parent=null；局部 parent 单独返回读取标识与有无子章，不占展开层数",
                   "结构验证时间": scenarios[-1]["recorded_at"]})
    # 保留其他工具的整个场景对象，包括旧计时和上轮重放说明；同名结构场景替换，新增场景追加。
    by_name = {s["name"]: s for s in scenarios}
    merged = [by_name.pop(s["name"], s) for s in evidence["scenarios"]]
    merged += list(by_name.values())
    for old in evidence["scenarios"]:
        if old["name"] not in NAMES:
            assert old == next(s for s in merged if s["name"] == old["name"])
    write_json(output / "evidence.json", {"checks": checks, "scenarios": merged})
    write_json(output / "observed-checks.json", checks)
    write_json(output / "timings.json", {"recorded_at": checks["验证时间"], "structure_recorded_at": checks["结构验证时间"],
               "unit": "seconds", "definition": "仅第四工具本轮重新联网；其他场景沿用旧采集。各场景独立计时，elapsed_seconds 截至命令返回，total_seconds 截至工具整理完成。",
               "scenarios": [{"name": s["name"], "input": s["input"], "total_seconds": s["total_seconds"], "commands": s["native"]} for s in merged]})
    return {"html": build_html(output / "demo.html", merged, checks), "evidence": str(output / "evidence.json"),
            "checks": checks, "new_structure_scenarios": len(scenarios)}


def main():
    """接收已有 HTML 与输出目录，真实验证第四工具；打印结果路径，无返回值。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=str(Path(__file__).with_name("demo.html")))
    parser.add_argument("--output", default=tempfile.mkdtemp(prefix="zen43-structure-demo-", dir="/tmp"))
    args = parser.parse_args()
    print(json.dumps(run_demo(args.output, args.base), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
