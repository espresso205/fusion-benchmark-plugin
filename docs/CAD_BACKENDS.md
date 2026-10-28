# Fusion 与 SolidWorks 建模后端

`provider` 选择 CLI 宿主，`execution_backend` 选择执行路径，`cad_backend` 选择 CAD 软件。缺少 `cad_backend` 的旧配置按 Fusion 处理。SolidWorks 只接受 `unified_gateway`，本版本只接收自包含 `.SLDPRT` Part，必须明确提交 configuration；不接收 `.SLDASM`。

## 配置

配置生成器保留 Fusion 的 `--fusion-url` 用法。SolidWorks 改用 `--cad-backend solidworks --solidworks-bundle <本机已安装bundle目录>`，不传 Fusion URL。其余 CLI、模型、公开题库、私有注册表和新输出目录参数与原用法相同。省略 bundle 时按本机安装位置发现，静态预检报告实际依赖是否齐全。

默认不启动 SolidWorks；`--launch-cad` 是明确允许启动的策略。正式运行先查询已有实例与文档，存在用户文档或不能确认安全空闲时停止。普通管理 `doctor/preflight` 只做本地配置和 CLI 检查，不建立 CAD 连接、不消耗模型额度。MCP 握手、无启动连接探测、合成建模和正式模型运行必须分别记录。

显式诊断使用 `python -B fusion-benchmark-bootstrap.py cad-probe --config <配置> --output <新目录> --stage discovery`。把阶段改为 `connect` 会执行不启动软件的真实连接检查；仅在明确允许启动时再添加 `--launch-cad`。诊断保存原始工具 schema、版本、调用和阶段证据，不调用模型。结果未知时保留全局桌面锁供维护端核对。

Codex 的严格原生工具排除能力不足，默认严格模式会提前失败。受信任同账户运行只有显式接受 `--allow-native-tools` 时生成宽松配置，记录授权来源。这不是操作系统沙箱，不代表撤销了同账户文件权限。候选始终只能通过自己的 Worker Gateway 连接配置中的 CAD；不能在全局注册直接访问来替代受控适配。

## 工件与身份

新执行合同为 `cad-worker/1`、Gateway 状态为 `cad-gateway/1`、提交回执为 `cad-submission/1`、原生描述符为 `cad-native/1`，ledger 为 schema 3。它们共同绑定 run、case、attempt、评分协议、CAD 后端及配置摘要。旧 schema 2、Fusion worker/gateway/trial 及 F3D 回执继续按旧规则读取，不修改历史输出。

Fusion 提交 `submission.f3d`，configuration 为 null；SolidWorks 提交 `submission.SLDPRT`，configuration 是准确的非空配置名。Fusion 保留 ZIP 容器初检；SolidWorks 新版原生容器的头部会随文件变化，提交必须匹配原生保存回执中的文件哈希、Part 类型和配置，不猜测固定 magic。描述符只绑定文件字节，真实识别、配置激活和健康判断由维护端独立重开完成。候选的依赖声明与 STEP 均不直接成为可信评分依据。

提交后 Gateway 撤权。维护端确认候选进程树已经结束、没有未决 CAD 请求，重新校验原生字节、配置与依赖声明，然后冻结。冻结后的后端或配置不能在同一运行内切换。

## SolidWorks 运行时与维护端检查

评测器继续使用 Python 3.12；SolidWorks MCP 使用安装 bundle 的私有 Python 3.11，stdio 运行于每次 attempt 的独立工作和日志目录。仓库仅包含适配与受控 helper，不复制或修改 SolidWorks、第三方 bundle 或全局配置。进程隔离和参数计划门禁保持开启，禁止继承内部 isolated-worker 标记或启用 unsafe Python。

桥接原有模板发现若未找到 Part 模板，维护 helper 只在真实连接版本对应的标准模板目录查找；必须恰有一个无链接的 `.prtdot`，否则拒绝猜选。它记录模板路径与哈希，不修改软件全局首选项，也不向候选暴露模板路径。

维护端固定实际 COM 实例的 PID、会话和版本，并在工作进程内检查实例一致性。几何写操作仅能作用于本题拥有的文档；多实例歧义、超时、断连、部分变更或无法确认静止都保留桌面租约，进入待核对状态，不自动重复写操作。Fusion 与 SolidWorks 共用既有 `fusion-benchmark-desktop.lock`。

Gateway 区分建模操作未知与派发前工作区未确认。派发前检查失败记录 `cad_workspace_unconfirmed`，不虚构未决建模调用；下一请求必须重新通过实例、文档归属和空闲检查。已经派发的操作后检查失败仍保留 `pending_cad`。维护探针传输本身结果未知时持久化 `cad_probe_unknown`，重启 Gateway 也不能绕过屏障或重发探针。

只有经过审查并绑定供应方源码哈希、实例、参数回显及正常进程退出的明确无修改分支，才可识别为已完成的输入拒绝。目前包括指定草图查找失败和螺纹模板路径校验失败；任意 `invalid_input`、原生操作返回空特征或矛盾回执均不自动获得该分类，也不自动重试原生写操作。

候选正常退出但未提交时，仅在零 CAD 调用、无未决请求、无自有文档、进程树已清空、维护端再次确认空闲且公开输入身份未变的条件下，记为 `candidate_missing` 并继续后续题目。这不是模型成功，不冻结原生文件或生成分数；其他未确认情况仍需维护端核对。

可信导出使用独占暂存副本，按冻结 configuration 重开，独立检查外部引用、重建和特征错误、实体有效性与正体积。记录 STEP AP、实体范围与实际单位，恢复临时修改的全局首选项。新 STEP 需由维护端独立解析，核对有效性、实体数、毫米包围盒与体积；失败不缩放、不补形状、不调整评分。只有哈希、来源、清理和几何复验全部满足后，才发布可信 STEP。

## 支持与验收状态

能力状态区分声明支持、依赖发现、MCP 握手、CAD 连接与可信导出。安装文件存在、工具列表可读或单元测试通过，均不等同于真实建模已通过。本地安装的实际 CAD 版本必须由连接证据报告，不能用桥接宣称的 SolidWorks 2025 验证范围代替本机 2026 的实测。

输入仍是 `fusion-public-inputs/1` 的 TASK 与有序 PNG/JPEG/DXF；多页 PDF 暂缓。评分仍按 `SCORING_V4.md`，主分为 shape/dimension/mass；不同 CAD 的结果差异如实保留。

当前尺寸探针会比较指定直径的 **BRep 圆柱面数量**，不是语义孔数。同一圆柱若被另一 CAD/STEP 导出器分成两个半圆柱面，即使直径和实际圆柱数量一致，也可能因面数翻倍而整项不通过。应结合 `dimension.raw.items` 中的 `diameter_difference_mm`、`failure_type=face_count_mismatch` 及形状/材料分布指标解释，不能仅凭低尺寸分断言尺寸错误。该限制不改变既有评分；消除面分割敏感性需另行版本化并验证新指标，不覆盖历史成绩。

源码测试使用 `python -B -m unittest discover -s tests -v`。设置 `RUN_GEOMETRY_TESTS=1` 会增加独立几何回归；三个历史资产回归需要 `FUSION_BENCHMARK_REGRESSION_ROOT`。完整便携包按 `BUILD.md` 单独构建和移位验证，不因源码测试通过而宣称已发布。
