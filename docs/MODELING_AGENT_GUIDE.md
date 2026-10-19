# 单题建模使用说明

候选仅接收本题公开 TASK/DXF、提交目录和[通用 Fusion 建模技能](../agent/fusion-modeling/SKILL.md)。先调用 `get_contract` 读取实际预算，再使用 Gateway 输入解析与 Fusion 工具。实际桥接由接收方预先安装；本说明不安装 Fusion，也不包含题目答案。

在自己的 Fusion 文档内完成原生建模，按合同导出 `submission.f3d`，记录实际单位、健康检查、外部引用与依赖证据，使用 `write_report` 和 `submit` 交付。维护端会独立冻结、重开及导出可信 STEP。不要读取 GT、私有标注或评分反馈，也不要更改冻结状态。

遇到超时或未知执行状态时停止新操作、保留请求证据并报告协调者；不得盲目重发、清锁或重启桌面来继续同一次尝试。
