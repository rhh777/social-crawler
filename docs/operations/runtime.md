# 运行维护

## 启动与部署

本地使用 [start_local.sh](../../scripts/start_local.sh)，默认把数据库、Cookie、profiles、日志和媒体集中到 `social-crawler-local/`。不设置 `CRAWLER_DATABASE_URL` 时由脚本启动隔离 PostgreSQL；配置了连接串则使用已有数据库。Ctrl-C 停止本脚本启动的服务，保留数据。

Compose 从源码构建，提供 PostgreSQL、应用和持久卷；使用 `--profile adspower` 时还会构建并启动 AdsPower sidecar。应用和 sidecar 固定为 `linux/amd64`，PostgreSQL 保持宿主机原生架构。Kubernetes 使用 [基础清单](../../deploy/k8s/README.md)；需要 AdsPower 时使用 [可选 overlay](../../deploy/k8s/overlays/adspower/README.md)。部署前自行构建镜像并提供给集群，在 overlay 中配置镜像仓库、digest、节点、存储类和网络设置。

```sh
docker compose --env-file .env.deploy up --build --wait
docker compose --env-file .env.deploy --profile adspower up --build --wait
```

第二条命令要求 `.env.deploy` 中有 `ADS_API_KEY`。sidecar 与应用共享 loopback 和 X11 socket，Local API 与动态 CDP 端口不会发布到宿主机；Chrome 使用 sidecar 自己的 1 GiB `/dev/shm`。AdsPower 浏览器环境的代理属于 profile 配置。桌面端与容器需要不同代理地址时，应创建容器专用 profile，并在 Web 账号里填写它的 `adspower_profile_id`，避免修改桌面环境后使本机失效。

部署一个应用实例。`CRAWLER_WORKERS` 控制进程内 worker 数；同一账号、profile 及冲突资源仍串行。控制台没有完整的公网多用户认证，本地端口或受保护的端口转发是默认访问方式。

## 开发验证

```sh
uv sync --locked
uv run playwright install chromium
uv run ruff check .
uv run pytest -q
python3 scripts/check_public_tree.py
```

测试使用合成数据、本地 HTTP 页面和 Mock 服务，不需要目标平台账号或模型密钥。未设置 `CRAWLER_TEST_DATABASE_URL` 时，PostgreSQL 变体跳过；浏览器测试需要已安装的 Playwright Chromium。

需要数据库集成测试时，在独立终端启动隔离数据库：

```sh
uv run --no-project --python 3.11 --with pgserver==0.1.4 \
  scripts/local_postgres.py --directory data/test-postgres \
  --url-file data/test-postgres.url
```

然后运行：

```sh
uv run python scripts/run_tests.py --postgres \
  --database-url-file data/test-postgres.url
```

测试创建并清理独立 schema；仅使用测试数据库。不要把现有部署连接串或正在采集的数据目录交给测试工具。

## 首次在线运行

先在 Web 配置账号、选择代理并保存登录；首次采集会在双出口一致时自动初始化出口绑定和缺失的默认额度策略。从一个帖子或很小的关键词范围开始。CLI 使用私有 TOML 和 Cookie 文件，仍需显式绑定出口，真实访问必须带 `--online`：

```sh
cp configs/canary-xhs.toml config.local.toml
# 编辑 config.local.toml，填写自己的账号环境；准备其中引用的代理文件。
uv run social-crawler bind-network --config config.local.toml
uv run social-crawler run --config config.local.toml \
  --cookies data/cookies/xhs.json --online
```

这些命令需要已配置的 PostgreSQL。`bind-network` 会联网检测并保存账号出口；它不创建完整额度策略，缺少额度时应先通过控制台配置。帖子模板的 `post_targets` 默认为空，使用前填写目标链接或 ID。代理凭据写入配置文件，避免出现在命令行历史中。

[container_online_validation.py](../../scripts/container_online_validation.py) 用于在线验证，会访问平台并生成报告、网络观测与样本，参数见 `--help`。

### Web 控制台在线冒烟

本地控制台已经配置好真实账号后，可以运行固定的低流量冒烟矩阵：

```sh
uv run python scripts/live_smoke.py --confirm-online
```

脚本只接受 loopback 控制台地址，并要求开始前没有排队或运行中的任务。它会依次验证抖音有头/无头、小红书 HTTPX/curl_cffi、Chromium 有头/无头和 AdsPower 有头/无头，共八个任务；每个任务只取一条内容，不下载媒体，其中抖音和小红书各有一个任务最多请求一条评论，不展开回复。测试会校验搜索、详情和评论任务正常结束、请求预算未超限、没有风险事件；Chromium 校验 HTTP 与浏览器出口一致，AdsPower 则校验其 profile 管理的浏览器出口。

运行前需要账号池中各有一个 `ready` 状态的抖音账号、小红书 Chromium 账号和小红书 AdsPower 账号，并保持 AdsPower Local API 可用。默认自动选择符合条件的账号；有多个账号时可用 `--douyin-account-id`、`--xhs-chromium-account-id`、`--xhs-adspower-account-id` 和 `--xhs-http-account-id` 固定选择。结果写入权限为 `0600` 的 `data/live-smoke-latest.json`，其中不保存 Cookie、代理地址或账号配置。

这是显式访问真实平台的运维测试，不属于默认 `pytest`。脚本自身的矩阵和判定逻辑由 `tests/test_live_smoke.py` 使用合成数据覆盖。

## 查看与恢复

```sh
uv run social-crawler list
uv run social-crawler status RUN_ID
uv run social-crawler cancel RUN_ID
uv run social-crawler export RUN_ID
uv run social-crawler resume RUN_ID --config config.local.toml \
  --cookies data/cookies/xhs.json --online
```

恢复会沿用原采集范围和进度；追加请求数或时间使用 `--extra-requests`、`--extra-seconds`。如果任务因登录或网络问题停止，先处理错误再恢复。CLI 全局参数（如 `--database-url-file`、`--output`）放在子命令之前。

默认报告目录为 `data/validation/<run-id>/`，包含报告、覆盖与质量检查、内容/评论快照、输入来源、运行配置、版本、事件和脱敏样本。达到设置的数量上限，只说明本次目标已完成。报告及脱敏样本仍可能含正文、账号引用和实际网络地址。

## 备份与升级

| 位置 | 需要保存的内容 |
| --- | --- |
| PostgreSQL | 任务、采集数据、账号、计划、额度、分析会话与证据 |
| `data/` | Cookie、Web 设置、Agent 密钥及恢复目录、附件、报告 |
| `profiles/` | 持久浏览器会话、身份回执和环境快照 |
| `artifacts/` | 已下载媒体 |
| `.env.local`、`.env.deploy` 与其他私有配置 | 数据库、代理和模型服务配置 |
| AdsPower 持久卷（如使用） | sidecar 的 profile 状态及运行数据 |

升级前停止新任务派发，等待或停止正在执行的任务，再备份数据库与对应文件。Web 启动执行 Alembic 升级。已有数据库密码不会因修改 Compose 环境变量自动变化；不要通过删除持久卷更新密码。

使用 `deploy/docker/runtime.Dockerfile` 复用镜像时，通过 `RUNTIME_IMAGE` 指定基础镜像，构建会核对依赖声明、锁文件和浏览器版本。应用配置变化与运行镜像不匹配时应重新构建完整镜像。
