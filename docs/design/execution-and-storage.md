# 任务执行与存储

## 运行、任务与分页

`runs` 保存一次采集的配置、状态、累计请求与耗时；`tasks` 保存这次运行的具体操作。操作包含 `resolve_target`、`search`、`detail`、`comments`、`replies`。每个任务单独保存数量目标、分页上下文、游标历史、样本引用和停止原因。

关键词运行按关键词创建搜索任务，再从结果派生详情、评论和有限回复。指定帖子运行保留每个输入的解析状态；同一帖子的不同有效输入共享后续取数任务。单条无效输入或不可见内容记录缺口，账号失效、验证码、出口变化等会话级问题停止整次运行。

XHS / RedNote 浏览器路径优先完成当前笔记的相关操作，再继续搜索，以复用页面上下文。HTTP 适配器与浏览器适配器都返回统一 `PageResult`，不直接维护数据库游标。

## 事务与去重

[Store.commit_page](../../src/social_crawler/storage/store.py) 在一个事务中完成业务条目保存、子任务派生、任务条目快照、数量统计、游标推进和事件记录。提交失败时不会留下“游标已前进但数据未保存”的状态。

| 表或表组 | 保存内容 |
| --- | --- |
| `contents` | 帖子最新合并记录，主键为 `(platform, id)` |
| `comments` | 评论及回复，主键为 `(platform, content_id, id)`，保留 root / parent 关系 |
| `content_times` | 平台发布时间、更新时间和首次采集时间 |
| `task_items` | 每个任务实际观察到的条目快照，主键为 `(task_id, item_id)` |
| `hits` | 一次运行中的关键词命中关系 |
| `post_sources` | 指定帖子的原始输入顺序、脱敏展示、解析状态与任务关联 |
| `runs`、`tasks`、`run_contexts` | 运行范围、阶段进度、执行账号及定时来源 |
| `accounts`、`schedules`、`system_settings` | 账号池、定时规则与全局运行控制 |
| 网络、额度和恢复表 | 出口绑定、网络观测、额度策略与预留、恢复探针 |
| `events`、`operation_metrics`、`risk_events` | 运行事件、操作耗时、错误和处理记录 |
| `analysis_sessions`、`analysis_messages`、`analysis_sources` | 分析会话、消息及证据 |

内容库显示最新记录，任务导出使用当时保存的快照。新的定时任务会重新采集配置范围，暂不支持只取新增评论。

## 请求限制与结束状态

[RequestBudget](../../src/social_crawler/orchestration/budget.py) 在请求前检查取消、执行 epoch、Cookie 是否变化和剩余时间，再执行间隔控制与额度预留。额度支持平台、账号、IP 分组等维度，并跨运行保存在数据库。网络失败最多按配置重试两次；每次请求仍经过预算控制。风控错误不会触发换账号或换传输绕过。

数量限制、页数上限、连续无进展、请求数和耗时共同约束运行。“不限制评论数”也仍受其他预算约束。达到数量上限记为 `limit`；平台明确无下一页记为 `exhausted`。前者不能解释为平台数据已经采完。

运行的结束状态包括 `completed`、`partial`、`blocked`、`failed` 和 `canceled`。查看结果时同时读取停止原因、字段缺失、跳过输入和 `response_has_more`，不能只依据状态名判断完整性。

## 中断与恢复

恢复沿用数据库中的原始采集范围。CLI `resume --config` 读取环境配置，不用新的 `[run]` 覆盖旧任务。恢复默认沿用剩余预算；需要更多额度时，可以追加请求数或时间。

每次执行都会增加 epoch（执行版本号），数据库拒绝旧 worker 的后续写入。浏览器恢复时重新打开相关页面，已保存的数据通过任务内去重避免重复计数；尚未提交的页面可能再次请求。

## 定时与并发

[Scheduler](../../src/social_crawler/orchestration/scheduler.py) 支持固定间隔、每日时间及带时区的五段 Cron，校验最短调度间隔。到点生成普通运行，通过 `(schedule_id, scheduled_for)` 唯一约束避免同一时点重复创建。错过多个周期时派发一次并推进下次时间，不补跑全部历史周期。

定时任务启动时从同平台账号池选择可用账号。应用重启会检查已创建但未派发的定时运行；已经中断的采集按恢复流程处理。[WorkerPool](../../src/social_crawler/orchestration/pool.py) 只执行资源键不冲突的工作。当前仍要求一个应用实例。

## 迁移与验证

表定义位于 `storage/store.py`，版本迁移位于 [storage/migrations](../../src/social_crawler/storage/migrations/)。Web 启动执行迁移；CLI `init-db` 初始化当前表结构。升级已有实例前先备份，CLI `init-db` 仅用于新数据库。

相关回归覆盖事务故障、重复页、恢复预算、旧 epoch、任务来源、定时幂等和额度预留，主要见 [test_storage_worker.py](../../tests/test_storage_worker.py)、[test_post_targets.py](../../tests/test_post_targets.py)、[test_scheduler.py](../../tests/test_scheduler.py)。PostgreSQL 测试需要配置独立的测试数据库。
