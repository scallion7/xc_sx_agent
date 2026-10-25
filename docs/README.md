# 文档导航

本目录将当前使用说明与历史开发记录分开。展示名称统一为 **Ecom Service Agent｜电商售后智能体平台**；仓库、数据库、API 路径和配置键保留兼容名称。

| 你想了解什么 | 推荐阅读 |
|---|---|
| 项目解决什么问题 | [项目首页](../README.md) |
| 面试时如何演示 | [五分钟演示](guides/five-minute-demo.md) |
| 服务与模块如何协作 | [架构概览](architecture/overview.md) |
| 如何运行和配置 | [配置与运行](guides/configuration.md) |
| 如何防止重复执行、如何恢复 | [可信执行与一致性](engineering/reliable-execution.md) |
| 如何使用新旧模块和配置名称 | [命名与兼容约定](engineering/naming-and-compatibility.md) |
| 如何管理政策、模型和发布 | [知识与治理](engineering/knowledge-and-governance.md) |
| 哪些结果已经验证 | [验收状态与证据](evaluation/acceptance.md) |
| 个人工程扩展如何说明 | [贡献说明](CONTRIBUTIONS.md) |
| 部署前检查什么 | [部署与维护清单](operations/deployment-checklist.md) |
| 为什么采用这些设计 | [知识快照与共享治理 ADR](decisions/0002-knowledge-snapshots-and-governance.md) |
| 查看完整开发过程 | [历史归档](history/README.md) |

## 文档维护规则

- 当前状态以验收状态页为汇总入口，每项结论注明提交、环境或证据；新结果追加记录。
- `history/` 记录当时情况，`evidence/` 保存原始结果；不把旧报告中的测试数改成最新测试数。
- 根目录的旧分期文档路径作为兼容入口保留，正文已归档；新增功能文档优先按主题命名。
- 展示名称使用 Ecom Service Agent，页面副标题为客户服务、客服工作台、运行管理台。历史示例中的“并夕夕/小夕”不作为当前产品名。
- 不因文档改名修改数据库、迁移版本、接口路径或会话文件格式。代码重命名的别名与升级方式见[命名与兼容约定](engineering/naming-and-compatibility.md)。
- 演示截图位于 `assets/`；历史截图/录像仍在 `evidence/platform/`，不得把它们标成新界面验收。
