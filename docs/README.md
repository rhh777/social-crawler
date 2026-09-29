# 文档导航

## 使用

| 文档 | 内容 |
| --- | --- |
| [项目首页](../README.md) | 本地与 Compose 快速开始 |
| [Web 控制台](guides/web-console.md) | 登录、采集、定时计划、内容库 |
| [配置指南](guides/configuration.md) | 本地/部署环境文件、配置优先级、账号和任务字段 |
| [Agent 分析](guides/agent-analysis.md) | 模型配置、会话范围、附件和证据 |

## 设计

| 文档 | 对应模块 |
| --- | --- |
| [架构](design/architecture.md) | 任务流程与模块划分 |
| [任务执行与存储](design/execution-and-storage.md) | domain、orchestration、storage |
| [账号、浏览器与网络](design/accounts-and-browsers.md) | environments、浏览器网关 |
| [手动分析模块](design/analysis.md) | analysis |

## 部署与维护

| 文档 | 内容 |
| --- | --- |
| [运行维护](operations/runtime.md) | 启停、验证、恢复、备份 |
| [Kubernetes](../deploy/k8s/README.md) | 单副本基础清单 |
| [AdsPower 镜像](../deploy/docker/adspower/README.md) | 可选浏览器运行环境 |
| [AdsPower overlay](../deploy/k8s/overlays/adspower/README.md) | 同 Pod sidecar |
| [KasmVNC](../deploy/docker/kasmvnc/README.md) | 账号浏览器桌面 |

## 项目

| 文档 | 内容 |
| --- | --- |
| [参与贡献](../CONTRIBUTING.md) | 开发环境、验证命令、代码风格、提交与发布前检查 |
| [安全说明](../SECURITY.md) | 上报方式、设计边界、自建注意事项 |
| [变更记录](../CHANGELOG.md) | 版本与不兼容变更 |
| [第三方说明](../THIRD-PARTY-NOTICES.md) | 依赖、vendor 资源、容器镜像内的第三方组件 |
