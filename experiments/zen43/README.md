# ZEN-43 五工具原型

五工具主要行为已于 2026-10-25 验收，基线为 `1f87b3d`。本目录是独立实验资产，未发布为正式 Skill。当前目录与命令以本页为准；产品规格和审查结论维护在 [工具设计](https://linear.app/zenokat/document/工具设计-46589bf68b1a) 与 [ZEN-43 证据及交接](https://linear.app/zenokat/document/五个工具的技术实现原型运行证据与交接-301ec4ae6c1a)。

2026-10-26 整理从 `skills/zeno-waytoagi-search/scripts/prototype_zen43/` 移到此处，保留五工具输入输出、缓存规则和历史证据。另修正两处已复现缺陷：结构演示误把缓存零请求判为失败；合并单元格表格的 HTML 回退丢失已下载图片地址或失败标记。

## 入口与目录

| 位置 | 用途 |
| --- | --- |
| `prototype.py` | 五工具命令行统一入口，仅处理参数与业务 JSON |
| `core/prototype.py` | 五工具 Python 入口 `Prototype`，协调只读调用与交付 |
| `core/source.py` | 共享来源解析、评论剥除与 JSON 保存；复用正式 daily 脚本 |
| `core/sections.py` | 章节模型、读取单位、命中树 |
| `core/markdown.py` | 默认正文及命中片段的表达转换 |
| `core/progressive_search.py`、`summary_reader.py`、`monthly_logs.py`、`creation_months.py` | 渐进检索、社区摘要与日志缓存 |
| `core/article_cache.py` | 后三工具共用的文章与图片缓存 |
| `data/monthly-fixtures.json` | **运行依赖**：已观测月份锚点，覆盖 2025-06 至 2026-10 |
| `data/summary-lookup-samples.json` | 演示及摘要定位对照使用的真实样本 |
| `tests/` | 离线行为检查，替换外部 CLI 响应，不联网 |
| `demos/` | 演示运行器、模板、历史性能与摘要定位实验 |
| `evidence/demo.html` | 最新已验收的 32 场景自包含回放，内嵌正文与图片 |
| `evidence/*.json`、其他 HTML | 各轮历史证据，保留原采集时间、命令、路径及计时 |
| `evidence/README-history.md` | 整理前说明原文，包含阶段性描述；不是当前规则入口 |

当前核心模块不再从五工具入口反向导入公共函数。`demos/container_preview.py` 只负责本地样张，正文转换由 `core/markdown.py` 承担。历史整页日志实验仍使用 `Prototype.source/pool`，当前检索与摘要不走这条旧路径。

## 使用五工具

从仓库根目录执行；Python 3.10+、已有可用的 lark-cli 用户认证，无新增依赖。原型验证时使用 lark-cli 1.0.96。

```bash
python3 -m experiments.zen43.prototype search --query RAG
python3 -m experiments.zen43.prototype search --since 2026-10-01 --until 2026-10-23
python3 -m experiments.zen43.prototype summaries --articles Sb82wh600i3t9Nk6yO5cnWhZnCc YTt3wPPIeixEK5kbJPnc0YL7nTf
python3 -m experiments.zen43.prototype locate --keywords MarkItDown MinerU
python3 -m experiments.zen43.prototype structure --depth 2
python3 -m experiments.zen43.prototype read
```

也可直接运行 `python3 experiments/zen43/prototype.py ...`。后三工具省略 `--article` 时使用已保存的 PDF 工具文章样本；实际使用传入文章 URL/token。

- 继续检索：`search --cursor '<next_cursor>'`，保持同一 `--cache-dir` 与 `--profile`。
- 目录下钻：`structure --parent-id '<unit_id>' --depth 1`。
- 多章正文：`read --unit-ids '<unit_id>' '<另一个 unit_id>'`。
- 章节范围：`read --start-unit-id '<起点>' --end-unit-id '<终点>'`，支持只提供一端，与 `--unit-ids` 互斥。
- 开篇正文使用 `@opening`，无标题全文使用 `@document`；均是可直接传递的不透明读取标识。

Python 调用：

```python
from experiments.zen43.core.prototype import Prototype

tools = Prototype('/tmp/zen43-example-cache')
outline = tools.structure('文章 URL 或 token', depth=1)
body = tools.read('文章 URL 或 token', unit_ids=[outline['outline'][0]['unit_id']])
print(body['markdown_path'])
```

## 当前行为摘要

- 知识库检索保留 `drive +search` 查询语义，最多 30 个字符，每次最多 3 条；先匹配有效条目，过期且位置已知时定点刷新，未知才逐月查找。内部按需继续取候选、刷新条目与读取月份，凑满 3 条或耗尽查询后才返回；不足 3 条仅在结束时交付。`status=can_continue` 与 `next_cursor` 表示仍有工作，不保证下一次有命中。仅评论命中在内部排除，不返回评论状态；未匹配日志的候选也在内部排除，不返回业务 `notice`，真正来源异常仍报错。不承诺全局社区日期严格排序。
- 日期均指社区更新日期。先按范围筛选，再对实际遇到的重复文章保留最近记录；纯日期发现不附摘要，也不能以零散条目缓存代替月份覆盖。
- 摘要专门批量读取社区原文。有效条目直接返回，过期条目定点刷新，陌生文章批量补取创建时间，先查创建月，未命中再逐月向现在查找。一次 `summaries` 调用自动跨月查找，找齐或查完支持范围后返回；“本次”指整次调用，不是一个月。条目与整月缓存独立计时，七天到期后用到才重取。
- 月级读取时当月正常请求；月初前八天也将上月视为活跃月份，覆盖近期日志迁入。新月份发现与失效锚点恢复仍待 ZEN-44。
- 全文检索支持关键词 OR；无命中不预读全文。有命中时取得或复用完整 XML 建立归属，保留原生容器命中节选，不额外补取整容器；同容器不同节选均保留，仅精确重复去重。业务 JSON 仅返回 `match_tree`，只保留命中章节及祖先，同章片段合并并按原文排序；不回传 `article/keywords/revision`。
- 结构返回 `parent/outline`；节点使用 `unit_id/kind/has_children/children`，仅原文标题有 `title`。深度按实际父子关系计算，局部目录不重复查询起点。`has_children` 区分未展开与真实叶子。
- 正文按完整单位、多个单位或起止范围选择，终点包含完整终章，重叠内部合并。通过 `unit_ids` 选读时，每个合并后选区前添加 `> unit_ids: ["所选 ID"]`，按原文顺序标识调用方所选单位；不添加“跳过正文”的说明。全文及起止范围不加该列表标记。成功返回 `markdown_path/warnings/scope`，不返回 `actual_ranges`、块端点或图片清单。图片保留正文原位置；获取文件不等于 Agent 已读完正文或看见图片。
- 普通表格优先 GFM，分栏按原顺序逐栏展开；合并单元格保留语义 HTML。未知内容的完整检测与复杂嵌套仍未覆盖，不能将当前样本通过表述为全类型无损转换。
- 后三工具共用七天文章快照，相同请求复用，新格式/选区/关键词按需取得，补充表示不延长期限。观察到新修订时废弃旧快照；定位或正文各部分修订不同时，使用已刷新的缓存自动重做整次操作，重新计算章节与范围，最多重试一次；仍不一致才报错，不交付混合版本。已有本地图片复用，文件清理后补下载。
- 来源内容读取后剥除已识别评论再解析、保存与交付；复杂评论格式未全面验收。内部调用、范围与缓存诊断不进入业务返回。后三工具的 `article/revision` 留在内部诊断，修订一致性检查保持；摘要批量结果仍保留 `input`。

## 2026-10-26 评注后的接口调整

本轮落实满三条分页、隐藏评论命中状态、单篇返回精简和所选正文 ID 标记；API Contract 已按新接口同步。历史 `evidence/demo.html` 与原 JSON 仍是前一版采集快照，不将旧字段或旧计时冒充新接口实测。新版核对见 `evidence/contract-followup-checks.json`。

## 2026-10-27 创建月份匹配

`search(query)` 与 `summaries` 已接入 `drive metas batch_query`：仅为没有可用摘要记录的文章补取创建时间，每批最多 200 篇并去重；通过 `request_doc_info.doc_token` 对应原 Wiki token，不能按响应顺序或底层 `doc_token` 对应。搜索响应的 `create_time=0` 不参与月份计算，原生 `--sort create_time` 排序保持。

先查各文章的创建月，未命中再逐月向现在查；按阅读时区计算月份，并裁剪到社区日期查询窗口及支持范围。创建早于窗口不排除文章，而是从窗口首月开始。各候选优先查自己的首选月，随后推进后续月；已读月份共用缓存，同一检索会话不重复扫描。匹配到已知记录即停止该文章的查找，不承诺未读月份没有更新。纯日期查询仍由近到远。

单篇元数据失败、缺失或时间无效时，该文章沿用范围内由近到远查找；原因只留内部诊断。整个接口请求失败仍报错。有效摘要缓存无需元数据请求，过期且位置已知仍定点刷新。业务输入输出字段保持。

排序实测的 69 条记录、62 篇不同文章均与独立元数据创建时间倒序一致。新路径真实验证见 `evidence/creation-month-checks.json`：旧文章冷读 2 次请求、暖读 0 次；MarkItDown 首屏 6 次请求交付 3 条，暖读仅 1 次搜索。创建月能否作为安全下界、迁移或复制是否需要补查更早日志，已加入 [ZENO-44](https://linear.app/zenokat/issue/ZENO-44) 待验证。

## 验证与演示

离线检查从仓库根目录执行，不依赖认证：

```bash
python3 -m unittest discover -s experiments/zen43/tests -p 'verify_*.py'
python3 -m unittest discover -s tests -p 'test_waytoagi_daily.py'
python3 -m unittest discover -s tests -p 'test_waytoagi_updates.py'
node experiments/zen43/tests/verify_template.cjs
```

重新采集会执行真实只读请求或复用有效缓存，输出到新的临时目录，不覆盖 `evidence/`：

```bash
python3 -m experiments.zen43.demos.demo
python3 -m experiments.zen43.demos.structure_demo
python3 -m experiments.zen43.demos.body_demo
python3 -m experiments.zen43.demos.article_cache_demo
```

`structure_demo` 重跑八个结构场景，`body_demo` 重跑三个 PDF 正文场景；二者默认从 `evidence/demo.html` 保留其他场景，可用 `--base` 与 `--output` 指定输入和输出。结构演示按实际缓存命中核对零或一次请求。`demo` 是主要路径采集器，不自动重建所有历史迭代中追加的 32 个场景；当前完整回放仍以保存的 `evidence/demo.html` 为准。

`article_cache_demo` 要求新的输出目录，用于验证空缓存到跨工具复用链路。来源文章的标题、修订或结构变化可能使固定样本断言失败，应核对真实内容，不得改成模拟成功。

本地正文表达样张不联网：

```bash
python3 -m experiments.zen43.demos.container_preview --xml '<原生响应.json>' --result '<业务结果.json>' --diagnostics '<内部诊断.json>' --output /tmp/zen43-preview.md
```

历史探索入口仍可运行：

```bash
python3 -m experiments.zen43.demos.monthly_experiment --rounds 3 --months 17
python3 -m experiments.zen43.demos.summary_lookup_probe
```

这些是历史对照算法，不代表当前产品入口。既有证据中的原始路径与命令保持原样，可通过 Git 基线追溯；它们不是整理后的执行命令，旧计时也不是本轮实测。临时路径清除后可能失效；自包含 HTML 中保存的快照仍可查看。

## 已知限制与后续

缓存与续查状态仅用于单机单用户实验，不是账户隔离或持久数据库。macOS 正文与图片使用 CLI 允许的 `/tmp`，以实际返回 `saved_path` 为准。原型不会自动登录、改权限、升级 CLI 或写入飞书内容。

[ZEN-44](https://linear.app/zenokat/issue/ZEN-44) 负责异常、截断、完整/部分交付、失效恢复和正文混杂规则；[ZEN-57](https://linear.app/zenokat/issue/ZEN-57) 负责内容类型及嵌套组合的格式映射。此次审查已复现但留待该专项处理的缺陷：段落内嵌图片/未知无文字内容可能静默丢失；内联代码中的 XML 可能被误转换。它们不是认可的产品行为，也不因主要样本通过而视为已解决。

此次整理不包含新版浏览器视觉验收、全库完整性证明或正式 Skill 发布。
