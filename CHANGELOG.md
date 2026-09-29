# 变更记录

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)；`0.x` 阶段次版本号可能包含不兼容变更。

不兼容变更会在此注明升级步骤。数据库结构变化通过 Alembic 迁移完成，Web 启动时自动执行；升级前请按 [运行维护](docs/operations/runtime.md) 备份。

## [未发布]

## [0.1.0]

首个公开版本。

### 功能

- 小红书（XHS / RedNote）与抖音采集：关键词搜索、指定帖子导入，采集正文、评论与回复。
- Web 控制台：账号登录与保存、任务创建与监控、定时计划、内容库、Agent 分析会话。默认仅允许本机访问。
- CLI：`run`、`resume`、`list`、`status`、`cancel`、`export`、`bind-network`，真实访问需显式 `--online`。
- 账号与浏览器环境：持久 profile、代理绑定与出口校验、环境快照、会话恢复，可选 AdsPower 与 KasmVNC 运行环境。
- 编排：按账号串行、请求与时长预算、额度预留、风险事件记录、媒体下载。
- 存储：PostgreSQL 控制面与采集结果，任务中断后可恢复；Alembic 迁移 `0001`–`0007`。
- 部署：本地启动脚本（内置 `pgserver`）、Docker Compose、Kubernetes 基础清单与 AdsPower overlay。

### 已知限制

- 控制台按单人本机使用设计，没有登录与多用户认证，见 `SECURITY.md`。
- 平台接口与页面结构变化会导致采集失败，请先用少量数据验证。
- 部署单个应用实例；`CRAWLER_WORKERS` 控制并发，同一账号仍串行。
