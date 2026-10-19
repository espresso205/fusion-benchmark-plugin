# 分发

v3.0.0 的源码与完整便携包是两种交付物。源码不含运行时；完整便携包由[构建脚本](../scripts/build_portable_plugin.py)收集锁定依赖后生成。推荐流程见[构建说明](BUILD.md)，接收方流程见[使用说明](PORTABLE_PLUGIN_USAGE.md)。

通用分发只包含代码、文档、技能、配置模板及第三方许可；不包含私有 GT、标注、真实题目、账号、历史运行或本机配置。题目与私有参考由[数据仓库](https://github.com/espresso205/fusion-benchmark-dataset)按组织者权限单独交付。不要把维护端源码与私有包作为候选工作区。

历史 `package_fusion_pipeline.py` 仍可验证并叠加原冻结 ZIP；它使用旧 README 和协议，不用于生成 v3.0.0 完整便携包。新旧包及历史 run 不覆盖。
