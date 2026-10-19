# 维护端评分

纯 STEP 入口为 `python -B evaluate.py --cases <cases.json> --output <new-output>`，默认协议 `fusion-step-adapted-v3.0`。输入需包含 candidate、GT、冻结标注和预期哈希；输出目录必须不存在。字段与评分政策见[评分合同](SCORING_V3.md)。

纯 STEP CLI 不证明文件来自 Fusion。自动流程应使用可信 F3D 重开导出链路，并保留哈希绑定及导出回执。管理 MCP 的 `results` 只用于授权维护端；候选不读取分数来重试。独立评测器可从[同级项目](https://github.com/espresso205/fusion-step-evaluator)获取。
