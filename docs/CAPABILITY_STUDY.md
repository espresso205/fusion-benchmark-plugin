# 冻结能力实验与离线分析

`fusion_pipeline.capability_study` 只生成计划或读取既有证据，不启动 CLI、模型、CAD 或辅助 MCP，也不重新计算几何或正式评分。指标删除的既有 `ablation` 命令仍是另一种分析。

## 计划输入

`build_plan(spec: dict, output_dir: Path) -> dict` 要求全新的输出目录。最小结构如下；路径、服务和证据均须由维护者换成本地实际配置，示例不是正式实验授权。

```json
{
  "schema": "candidate-capability-experiment/1",
  "experiment_id": "pilot_01",
  "base_config": {
    "cad_backend": "fusion",
    "capability_registry": "C:/benchmark/private/capabilities.json",
    "questions_root": "C:/benchmark/public",
    "private_cases": "C:/benchmark/private/cases.json",
    "evaluator_directory": "C:/benchmark/evaluator",
    "cad_identity": {"backend": "fusion", "version": "REPLACE_WITH_VERSION"}
  },
  "common_identity_mode": "capture",
  "candidate_profiles": {
    "claude_main": {
      "provider": "claude",
      "model": "explicit-local-model-id",
      "reasoning": {"mode": "cli_default"},
      "cli_executable": "C:/local/claude.exe",
      "strict_tool_isolation": true
    }
  },
  "candidate_profile_ids": ["claude_main"],
  "case_ids": ["Q01", "Q02"],
  "repetitions": 2,
  "schedule_seed": 20261028,
  "groups": [
    {"id": "A", "skills": [], "mcp_servers": ["cad_native"]},
    {"id": "B", "skills": ["guidance"], "mcp_servers": ["cad_native"]},
    {"id": "C", "skills": [], "mcp_servers": ["cad_native", "lookup"]},
    {"id": "D", "skills": ["guidance"], "mcp_servers": ["cad_native", "lookup"]}
  ],
  "common": {
    "dataset_sha256": "REPLACE_WITH_DIGEST",
    "input_manifest_sha256": "REPLACE_WITH_DIGEST",
    "gt_annotations_sha256": "REPLACE_WITH_DIGEST",
    "common_protocol_sha256": "REPLACE_WITH_DIGEST",
    "budget_policy": {"seconds": 1800, "requests": 200},
    "plugin_source_sha256": "REPLACE_WITH_DIGEST",
    "evaluator_weights_sha256": "REPLACE_WITH_DIGEST",
    "cad_identity": {"backend": "fusion", "version": "REPLACE_WITH_VERSION"},
    "coordinate_mode": "geometry"
  },
  "admission": {"status": "not_verified"},
  "retry_policy": "retain_all_attempts_no_best_score_selection"
}
```

`base_config` 是现有运行配置，须保留数据、CAD、输出和评分所需的字段。`candidate_profiles` 也可直接放在该配置中。顶层 `capability_registry` 可覆盖其中的注册表路径。注册表、profile 和依赖均通过现有统一解析器校验，任何关闭的依赖都不会被自动打开。各组共同保留能力的相对顺序必须相同。

`common` 是维护端冻结的共同身份对象，包含数据、逐题输入清单、GT/annotations、共同协议、预算、插件/评分源码及权重、CAD 版本和坐标模式。运行记录必须逐字典值匹配该对象。它不会由分析器猜测或从正式得分反推。`generation_controls` 保存可控生成设置及不可得的 null；调度种子只控制运行顺序。

示例显式选择 `common_identity_mode=capture`，会读取真实文件并用实际身份替换示例中的 common 占位内容；也可直接省略示例 common 对象。模式为默认 `verify` 时，有真实 `questions_root/private_cases` 的配置会重新捕获并要求提供的 common 完整匹配，否则在创建输出目录前报 `planned_common_identity_mismatch`。`draft` 明确跳过来源绑定；缺少真实数据路径的离线草案也保留未绑定状态。未绑定草案即使填满格式正确的哈希，仍不可比较、不可正式执行。

共享接口 `capture_common_identity(config, case_ids) -> dict` 无子进程、只读文件：逐题通过 `input_assets.load_manifest` 或 `legacy_manifest` 验证公开输入；校验私有 cases 的 GT/annotations 字节与 `expected_hashes`、annotation 中的 `gt_sha256`；按传入 case 顺序汇总。返回的 common 除下表字段外，还保留 `case_identities`、`plugin_source_files`、`evaluator_source_files` 和 `scoring_protocol`，这些完整证据同样参与匹配。

输入摘要是规范化 JSON `[{case_id,input_manifest}, ...]` 的 SHA256，私有摘要是 `[{case_id,gt,annotations}, ...]` 的 SHA256，其中 GT/annotations 值为实际文件摘要。dataset 摘要绑定这两项。插件摘要绑定实际 `runner.source_hashes()`；共同协议使用 `common_protocol_sha256()`。评测身份绑定所选 protocol、weights 字节及 evaluator 中实际 Python/JSON 来源清单；有 `PACKAGE_MANIFEST.json` 时同时校验包清单。所有 JSON 摘要按 UTF-8、键排序、紧凑分隔符生成。只提供 evaluator zip 的配置须先准备已验证的 evaluator_directory 再冻结实验。

当前正式共同预算固定为 1800 秒和 200 次受控请求。CAD 版本来自配置声明的 `cad_identity`，静态捕获不冒充实际 CAD 观测；版本未知时仍不完整，执行端需另核对实际连接版本。

可比较分析及正式执行要求以下 **全部九个 common 键**；缺省、null、占位字符串或格式错误均标为不完整：

| 必需键 | 值约束 |
|---|---|
| `dataset_sha256` | 数据版本身份，64 位小写十六进制 SHA256 |
| `input_manifest_sha256` | 按计划 case 顺序汇总的输入清单身份，64 位小写十六进制 SHA256 |
| `gt_annotations_sha256` | GT 与 annotations 的组合身份，64 位小写十六进制 SHA256 |
| `common_protocol_sha256` | 共同任务协议字节身份，64 位小写十六进制 SHA256 |
| `plugin_source_sha256` | 冻结插件源码身份，64 位小写十六进制 SHA256 |
| `evaluator_weights_sha256` | 冻结评分源码及权重的组合身份，64 位小写十六进制 SHA256 |
| `budget_policy` | 对象，含正有限数 `seconds` 与正整数 `requests`，布尔值不视作数字 |
| `cad_identity` | 对象，含 `backend=fusion\|solidworks` 与非空字符串 `version` |
| `coordinate_mode` | `geometry` 或 `strict` |

完整性检查记录在计划及分析的 `common_identity_validation` 中，含 `status=complete\|incomplete`、必需键、缺少键、无效键及 `binding_status`。只有字段完整且 `common_identity_capture.status=captured` 的计划可进入可比分析；格式正确的任意声明哈希无法替代真实来源绑定。

离线草案仍可用 `common={}` 或不完整身份生成。其原始观测、完成率、所有计划分母和 attempts 保留，但每格 `comparable=false`，对比 `comparison_eligible=false`，质量配对数为 0，正式分数聚合及完成率主效应差值为 null，并列出不可比原因与限制。不得以两边共同缺失身份推断可比。修复身份须生成新计划。

`admission` 保存与输入哈希绑定的图纸可读性和 GT 主指标可测性准入证据。离线计划可保留 `not_verified`；正式执行仍须先完成这些准入。缺失版本证据不会被伪造成已验证。

正式准入必须由维护者预先提供下述结构，不能由捕获函数自动评图或推断 GT 可测性。每题的三个摘要必须与 `common.case_identities` 中该题的实际摘要完全匹配，两项检查均需 `passed` 和独立证据摘要：

```json
{"admission": {
  "status": "passed",
  "cases": {
    "Q01": {
      "input_manifest_sha256": "REPLACE_WITH_CAPTURED_CASE_DIGEST",
      "gt_sha256": "REPLACE_WITH_CAPTURED_GT_DIGEST",
      "annotations_sha256": "REPLACE_WITH_CAPTURED_ANNOTATIONS_DIGEST",
      "drawing_readability": {"status": "passed", "evidence_sha256": "REPLACE_WITH_EVIDENCE_DIGEST"},
      "gt_primary_measurability": {"status": "passed", "evidence_sha256": "REPLACE_WITH_EVIDENCE_DIGEST"}
    }
  }
}}
```

缺少题目、证据摘要不是 64 位小写十六进制、检查未通过或输入/GT/annotation 身份已过期，都会在正式校验时触发 `study_admission_not_verified`。计划及分析保存 `admission_validation` 和原因。离线分析保留原始分数、失败和全部分母，并明确未完成 admission 的限制；相应比较标为不可比、质量配对与主效应差值不进入有效分析。

## 冻结输出和调度

输出包括 `experiment-plan.json`、输入副本 `study-spec.json`、私有注册表副本、每个 profile 的快照、每个组的技能/资源/服务源码快照和 `cells/*.json`。快照不读取认证文件内容；仅保留配置引用。整个目录是私有维护端材料，不能交给候选。

每格键为 `(candidate_profile_id, group_id, case_id, repetition)`，repetition 从 **1** 开始。计划不隐式展开推理强度，须为不同设置显式注册不同 profile。三个 profile × A/B/C/D × 两题 × 两次重复生成 48 格，独立零件数仍是两题。

`cells` 即完整串行执行顺序。以 case × repetition 为区块，对 profile/组组合进行种子打乱和逐区块轮转，避免把某一组固定放在所有区块的首位。每格生成新 `run_id` 和独立配置，不复用前格的会话或工件。所有真实执行仍由现有 Trial、桌面租约和未知 CAD 状态停止机制负责。

计划关键字段：

| 字段 | 含义 |
|---|---|
| `plan_sha256` | 除自身外整份规范化 JSON 的摘要 |
| `profiles[id].configuration` | 统一解析器返回的完整非敏感启动配置 |
| `profiles[id].snapshot_path` | 相对计划根目录的 profile 快照 |
| `profiles[id].evidence` | 维护者预先取得的 CLI/模型/连接/隔离/推理支持证据 |
| `groups[id].selection` | 显式有序选择；空 skill 列表保持为空 |
| `cells[].config_path` | 相对计划根目录的逐格配置路径 |
| `cells[].config_sha256` | 规范化完整逐格配置摘要 |
| `cells[].candidate_profile_sha256 / selection_sha256` | 该格最终 profile 与能力选择绑定 |
| `frozen_files` | 全部计划快照及配置的相对路径到文件字节摘要映射 |

修改任意快照、逐格配置、完整因子组合或计划摘要会被拒绝。计划目录不得原地更新；修复输入、配置或版本后生成新目录和新计划。

## 执行端校验接口

```python
validate_plan_execution(plan_path, cell_id, config,
                        require_verified=False, observed_launch=None) -> dict
```

返回匹配的 cell。应在加入运行期字段、启动候选或 CAD 前调用；它会验证完整配置摘要、profile、全部快照、原始能力来源和已冻结的可执行文件字节。推理 override 或其它配置更改报 `planned_candidate_configuration_mismatch`。

执行端使用 `require_verified=True` 时，所选 `profile_evidence[id]` 必须含 `configuration_valid=true`、`cli_available=true`、`required_flags_present=true`、非空 `cli_version`、本地绝对 `resolved_executable` 及其 `executable_sha256`。校验器检查已安装文件的字节摘要。传入 `observed_launch` 时，其 `cli_version/resolved_executable/executable_sha256` 必须与冻结证据一致；版本升级不能混入旧单元。

该模式还要求上述 common 身份全部完整且已捕获，否则报 `study_common_identity_incomplete`；随后调用共享 capture 接口重新读取计划全部 case 的输入、GT、插件、协议和评分来源，要求与冻结 common 完全一致。声明的 CAD backend 必须与逐格配置一致。缺少来源绑定的草案不能因 CLI 就绪而获准正式执行。

这只是静态身份和 CLI 就绪证据校验；真实执行仍须用新探测结果检查显式推理支持，并完成输入/GT 准入、模型请求、桌面租约、隔离和清理校验。`cli_default` 不要求伪造模型实际回显，模型身份仍可为 `requested_only`。普通离线计划及分析不执行 help/version 子进程。

## 可移植运行证据

```python
run_manifest_template(plan_path, cell_id, run_id, attempt_id) -> dict
```

该模板生成 `schema=candidate-capability-run/1`，绑定 `plan_sha256/experiment_id/cell_id/candidate_profile_id/group_id/case_id/repetition/config_sha256/candidate_profile_sha256/selection_sha256`，以及 `frozen_identity=plan.common` 和 `requested_reasoning`。运行端补充 `run_id/attempt_id/attempt_index`，每个格的首次 attempt_index 是 1。

`outcome` 包含：

| 字段 | 约定 |
|---|---|
| `native_submitted / trusted_export / primary_complete / cad_unknown` | 明确布尔值 |
| `formal_score` | 完整正式主分；未提交、未完整测量时为 null |
| `primary_coverage` | 0–1 或 null |
| `policy_lower_bound` | 可选；独立保存既有下界，不填入 formal_score |
| `failure_stage / failure_category` | 字符串或 null；保留辅助 MCP、输入、测量和基础设施故障 |

`usage` 可包含 `actual_skill_reads`、`per_server_tool_calls`、`controlled_calls`、`modeling_seconds`、`input_tokens/output_tokens/reasoning_tokens`、`cost`。无法取得时保留 null。技能读取可用次数、读取事件数组或逐技能次数映射；服务调用使用 ID 到次数映射。

实际暴露证据应填 `exposure_verification_status`，另附可见工具 schema/哈希、观测模型/推理/隔离、清理证据及来源。模板不会把“配置启用”写成“已实际暴露”或“候选已使用”。附加的 provider/model/原生推理请求等字段若与冻结配置不符同样拒绝；完整原始记录保存在分析 attempts 中。

默认每格最多一次 attempt。允许基础设施重试时，必须在生成计划前设置：

```json
{"infrastructure_retry": {
  "max_attempts": 2,
  "selection_rule": "first_non_infrastructure_attempt"
}}
```

可选选择规则为 `first_attempt` 或 `first_non_infrastructure_attempt`，从不按分数选择。后续 attempt 需附 `retry={reason: "infrastructure_failure", previous_attempt_id: "...", evidence_sha256: "..."}`；前一次必须是未提交且 CAD 状态已知的基础设施失败。技能/MCP 不可用不自动归为可重试基础设施。所有 attempt 均保留；超出预设次数、重复/不连续编号或无证据的多 attempt 拒绝整格。

如果出现 retry 字段，它必须是只含 `reason/previous_attempt_id/evidence_sha256` 的对象，原因必须是 `infrastructure_failure`，前次 ID 必须为非空字符串，证据摘要必须为 64 位小写十六进制。列表、null、字符串、数字或不完整对象均以 `study_retry_metadata_invalid` 留存拒绝，分析继续输出全部计划格，不因调用 `.get()` 失败中断。

## 分母与比较

`analyze_plan(plan_path, run_manifest_paths, output_dir) -> dict` 只读计划和运行 JSON，独占创建 `capability-analysis.json/.csv/.md`。完整计划目录可复制到其它位置后离线分析；分析无需原始 skill、注册表、CLI 或 CAD 安装。运行端的来源复核仍需要原始安装。

JSON 的 `cells` 保留全部计划格，`status` 为 `observed/missing_run/rejected_run`，每格保留所有 `attempts`。不匹配或无法读取的记录保留在 `rejected_runs`；它们不会进入质量配对，也不会从计划分母中消失。CSV 的缺测分数是空单元，JSON 保持 null；不把未提交改成正式零分。

`strata` 按 profile × 组展示计划分母、原生提交/可信导出/完整测量率、失败分类与阶段、未知 CAD 比例、完整子集的正式均分以及 usage 均值和各自可得数量。未启用、启用但未使用、启用但使用证据未知分开记录；实际暴露验证状态另计数。

`within_profile_comparisons` 只在同一 profile 内比较只改变一个 skill/MCP ID 的组；标准四组产生 B−A、C−A、D−B、D−C。四组完整时另给 `(D−C)−(B−A)`。B−C 不解释为单因素贡献。

质量差只使用相同 case × repetition、双方主指标完整且有正式分数的样本，返回每题差值、可配对数量、独立题数和未配对原因。例如 A 两题为一成功/一未提交，B 两题均成功，则提交率为 0.5 和 1.0，质量配对只有一题。正式 A 第二题仍为 null。

`profile_comparisons` 在同能力组内比较不同 profile，默认标为 `system_settings`，逐项列出 CLI、模型、推理、路径、认证引用、隔离和版本/连接证据差异。不把跨 CLI 的同名 high 当作等量计算。

只有两份 profile 的其余启动字段、可执行字节及全部已验证证据完全相同，双方均为 explicit native effort 且模型、CLI 版本、连接、隔离、生成控制和推理支持证据齐备时，才标为 `reasoning_only`。`cli_default` 或 variant 联动设置保持 `system_settings`。

质量配对是共同完整子集上的描述，存在选择偏差，必须与全部计划格完成率一起解读。重复不是新增独立题目。当前版本保留每题差值，不生成置信区间；后续如估计区间应按 case 聚类，不能把每次重复当独立样本。

源码合成验证：`python -B -m unittest tests.test_capability_study -v`。它不代表真实 CLI、模型、CAD、辅助 MCP 或便携发布验收通过。
