# 图片与多附件建模输入

每题使用显式 `inputs.json` 清单绑定任务说明与有序附件。支持 PNG、JPG/JPEG、DXF，以及它们的混合；单图题不需要 `drawing.dxf`。现有 `TASK.md + drawing.dxf` 目录及 scheduler 旧参数继续可用。

本合同负责建模输入准入。HTML 的 `fusion-report-inputs/1` 只负责资料展示，其 PDF、WebP 和普通附件能力不能用来推断建模模型能够读取这些格式。PDF 分页、WebP、动画图片、STEP/F3D 答案不属于本合同支持的输入。

## 清单

文件为 UTF-8 严格 JSON：重复键、未知字段、未知版本都会失败。根字段固定为 `schema`、`case_id`、`task`、`attachments`：

```json
{
  "schema": "fusion-public-inputs/1",
  "case_id": "Q01",
  "task": {
    "id": "task", "role": "task", "path": "TASK.md", "mime": "text/markdown",
    "bytes": 123, "sha256": "<TASK.md 的实际小写 SHA256>", "display_name": "建模任务"
  },
  "attachments": [
    {
      "id": "front", "role": "view", "path": "views/front.png", "mime": "image/png",
      "bytes": 456, "sha256": "<原始 PNG 的实际小写 SHA256>", "display_name": "主视图"
    },
    {
      "id": "outline", "role": "drawing", "path": "outline.dxf", "mime": "application/dxf",
      "bytes": 789, "sha256": "<原始 DXF 的实际小写 SHA256>", "display_name": "轮廓"
    }
  ]
}
```

示例中的字节数与摘要是占位信息，不能直接用于注册。使用下面的脚本可创建包含真实摘要的两张小图和 DXF 样例，不调用模型或 Fusion：

```powershell
python -B examples/create_public_input_example.py C:/benchmark/examples/new-inputs/Q01
```

输出目录必须不存在；脚本不会覆盖已有材料。输入只含合成方块图纸，没有参考模型或评分数据。

`task` 的 `id/role/path/mime` 固定为 `task/task/TASK.md/text/markdown`。附件角色允许 `drawing`、`view`、`reference`；这里的 `reference` 指公开参考图片/图纸，不是私有标准答案。每个描述符的七个字段均必填。`id` 为 1–64 位 ASCII 字母、数字、下划线或连字符，首字符必须是字母或数字；`case_id` 采用同样的身份规则，流水线仍限定 `Qxx` 题号。

所有列出的附件都必需。数组顺序就是阅读顺序，任务说明在附件之前；同一 SHA256 可用于内容相同但身份不同的附件，ID 与路径不能重复。MIME 与后缀必须一致，PNG 使用 `image/png`，JPG/JPEG 使用 `image/jpeg`，DXF 使用 `application/dxf`；后缀大小写均可。

路径相对清单文件所在题目录，只能使用 `/`。拒绝绝对路径、UNC、盘符、ADS、点段、空段、反斜线、Windows 保留名、尾点或空格、控制字符、大小写别名及文件/目录前缀冲突。文件、公开根和祖先不能含符号链接或 junction，文件不能有硬链接别名。候选只能按冻结清单读取本题附件，额外文件不会因出现在目录中而自动成为公开输入。

## 固定限制

|对象|限制|
|---|---:|
|清单 JSON|1 MiB|
|附件数，不含 TASK|16|
|TASK.md|1 MiB|
|每份 DXF|16 MiB|
|每张原始图片|8 MiB|
|全部输入字节总量|64 MiB|
|图片像素总数|24,000,000|
|图片任意单边|12,000 像素|
|编码后的单次图片 MCP 响应|12 MiB，包含 JSON 与 base64 开销|

输入必须是非空普通文件。TASK 要求有效 UTF-8 文本；DXF 检查文件签名，实际实体解析由 Gateway 的 DXF 解析器完成；图片检查后缀、MIME、签名、真实解码、帧数和尺寸。超限或不支持的必需输入在建模前失败，不能静默忽略附件。

图片不会自动缩放或裁切。EXIF 方向为 2–8 时，返回像素按照该方向归一化，并采用无损 PNG 编码；JPEG 的 CMYK 色彩模式需要转为 RGB。原始输入保持原字节和摘要。其他图片直接返回原字节。工程尺寸应取自明确标注，不得把图片像素长度当作毫米。

## 注册、重载与读取

```powershell
python -B -m scheduler register-case C:/benchmark/runs/run-001 Q01 --manifest C:/benchmark/questions/Q01/inputs.json
python -B -m scheduler register-case C:/benchmark/runs/run-002 Q01 --task C:/benchmark/questions/Q01/TASK.md --drawing C:/benchmark/questions/Q01/drawing.dxf
```

`--manifest` 与 `--task/--drawing` 互斥；旧方式要求两项一起提供。调度器先验证完整输入，在独占暂存目录复制并再次校验，然后发布完整公开目录。失败不登记成功，也不会保留本次未提交的半套输入。进程崩溃留下的目录不会被自动覆盖，应由维护端检查后使用新 run。

外层 checkpoint 和冻结提交继续使用 `schema_version=2`。旧 `inputs={task,drawing}` 原样保存和读取，不在重载时升级或重写；新 `contract.inputs` 直接保存本合同清单。`checkpoint.json` 是权威，公开目录不依赖冗余 `inputs.json`。冻结提交完整绑定清单，包括附件顺序。

共享 `fusion_pipeline.input_assets` API：

|API|行为|
|---|---|
|`iter_assets(manifest)`|依次给出任务和附件|
|`validate_manifest(manifest, case_id=None)`|只验证结构、身份、声明限额，不读文件|
|`load_manifest(path, case_id=None)`|严格读取清单，并验证相对目录中的全部文件|
|`verify_manifest(manifest, root)`|验证全部附件的字节数、摘要和内容；成功返回 `True`|
|`legacy_manifest(case_id, root)`|为旧固定文件建立临时统一视图，不改写历史合同|
|`read_asset(manifest, root, asset_id)`|每次重新验证，返回 `(descriptor, bytes)`|
|`image_result(manifest, root, asset_id)`|返回含原生 MCP image 的结果字典|

读取结果的 `structuredContent` 与首个 text 块包含相同元数据：`id`、附件零基 `index`、`source_sha256`、`returned_sha256`、`width`、`height`、`transform`、`source_mime`、`mime`。`transform` 为 `none` 或 `exif_transpose:<方向值>`。第二个 content 块为 MCP 原生 `type:image`、`mimeType` 和 base64 `data`，不能用文件路径或 base64 文本说明替代。审计仅记录这些身份摘要，不重复保存完整 base64。

Gateway 的 `list_inputs` 提供发现，`read_image(id)` 按 ID 读取图片；文本读取不能读取图片字节，`parse_dxf(id)` 保留 DXF 实体解析。新输入链路使用统一 Gateway 后端；旧 guard 后端对不支持的输入提前失败。读取和提交继续受到工具预算、取消、截止时间及提交后撤权约束。协议返回原生图片并不等于某个 CLI/模型组合已验证视觉能力，真实看图和完整 Fusion 流程应分别记录验收证据。

## 真实视觉与报告

使用 `python -B fusion-benchmark-bootstrap.py visual-check --config <pipeline.json> --output <新目录>` 显式执行真实 CLI/模型检查。它调用正式 Gateway 的图片工具，读取两张无答案提示的随机图；维护端核对图形数量、附件顺序及输入/返回图摘要。输出包含 CLI 版本、requested model、实际会话证据和回答。此检查消耗模型额度，不调用 Fusion，不改变管理 `preflight` 的只读语义。Codex 的实际后端模型名称仍不由 exec JSON 证明；其他未经实测的 CLI/模型组合标为未验证。

Runner 从本次冻结清单生成 HTML 资料映射，顺序与建模一致，报告不会重新扫描原题目录。资料配置优先级为显式 `inputs` 参数、配置 `report_inputs`、自动冻结输入映射。映射文件在 ledger 根，公开附件在 `public/Qxx/`，它不进入候选白名单；报告生成失败不改变已有评分。
