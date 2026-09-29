# 配置指南

数据库、端口、并发数和模型服务等全局配置分为项目根目录 `.env.local`（本机）和 `.env.deploy`（Docker/Kubernetes）。先从 [`.env.example`](../../.env.example) 创建对应文件：

```sh
# 仅首次创建；已有文件请合并缺少的字段，不要覆盖。
cp -n .env.example .env.local
cp -n .env.example .env.deploy
chmod 600 .env.local .env.deploy
# 本地启动读取 .env.local
./scripts/start_local.sh
# Compose 显式读取部署配置
docker compose --env-file .env.deploy up -d
```

## 最小可用配置

本地跑起来不需要改任何字段：直接复制的 `.env.example` 就能启动，脚本会启动自带 PostgreSQL，账号在控制台里添加。下面这张表说明什么时候才需要动配置，其余字段都可以先不看。

| 你想做的事 | 需要配置 |
| --- | --- |
| 本地试用，采集一两个帖子 | 无，`cp -n .env.example .env.local` 即可 |
| 用已有的 PostgreSQL | `CRAWLER_DATABASE_URL` |
| 改端口或并发 | `CRAWLER_WEB_PORT`、`CRAWLER_WORKERS` |
| 用 Agent 分析采集结果 | `ANTHROPIC_API_KEY`，需要换服务地址时加 `CRAWLER_AGENT_API_URL` |
| 账号走代理 | `CRAWLER_XHS_PROXY` 等变量，或账号的 `proxy_file`；并在账号上启用 |
| 用 AdsPower 管理浏览器环境 | `ADS_API_KEY` 与账号的 `adspower_profile_id` |
| 本机用 Kameleo 管理浏览器环境 | 启动 Kameleo 桌面应用，并设置账号的 `kameleo_profile_id` |
| Docker Compose 部署 | `POSTGRES_PASSWORD`、`CRAWLER_COMPOSE_BIND_HOST`、`CRAWLER_POSTGRES_PORT` |

Cookie、浏览器 profile 和账号凭据是运行数据，由控制台写入 `data/`、`profiles/`，不要手写进环境配置文件。

## 读取规则

- `scripts/start_local.sh` 无论从哪个目录执行，都优先读取项目根目录 `.env.local`；迁移期间若该文件不存在，才兼容旧的 `.env`。
- Docker Compose 使用 `docker compose --env-file .env.deploy ...`。代理中继和 AdsPower Secret 同步脚本会优先读取项目根目录 `.env.deploy`。
- 直接运行 `social-crawler`、`social-crawler-web`、Cookie 捕获或容器验证时，用 `CRAWLER_ENV_FILE` 明确选择 `.env.local` 或 `.env.deploy`。
- 可在进程环境设置 `CRAWLER_ENV_FILE=/absolute/path/private.env` 选择其他文件；指定的文件缺失或格式错误会报错。这个变量应在启动前设置，不能放在待选文件里。
- 启动参数 > 已设置的进程环境变量 > 所选环境配置文件 > 程序默认值。空的进程变量也会覆盖文件。文件修改后重启服务生效。
- 不执行 shell 语句，不展开 `${VAR}`。密码含 `#`、空格、`$` 时用单引号包裹。Compose 自带 dotenv 解析器，单引号也可避免它展开 `$`。数据库 URL 中的密码还需要 URL 编码。
- Docker Compose 只把 `compose.yaml` 列出的变量传入容器；本项目使用 `docker compose --env-file .env.deploy up -d --force-recreate`。本机绝对路径不会自动挂载到容器。
- 库模块导入不会读取环境配置文件；自己的 Python 入口需先设置 `CRAWLER_ENV_FILE`，再调用 `social_crawler.config.load_config()`。

### 页面设置与环境变量

Web 已保存的 Agent 设置优先于环境配置文件，保存在运行根目录 `data/web/agent-settings.json`。希望以后全部从文件管理 Agent 时，先把需要的值写入对应的 `.env.local` 或 `.env.deploy`，停止服务，将该 JSON 改名备份到同一私有目录，再启动。备份也含密钥，不能提交到 Git。

账号是多条独立记录，数据库中已有的账号配置、TOML 中填写的字段优先于全局 AdsPower 默认值。`CRAWLER_DEFAULT_HEADLESS` 只用于初始化 Web 默认账号；修改已有账号请使用账号页面。环境中的数据库 URL 始终覆盖 Web 以前保存的数据库地址。

## AdsPower

本机使用 AdsPower 时在 `.env.local` 填写；Kubernetes 部署时在 `.env.deploy` 填写：

```dotenv
ADS_API_KEY='你的 AdsPower API key'
CRAWLER_ADSPOWER_API_URL=http://127.0.0.1:50325
CRAWLER_ADSPOWER_START_TIMEOUT=90
```

`ADS_API_KEY` 用于 **AdsPower CLI 运行服务启动**（`ads start -k`），采集器连接 Local API 时不把这个 key 当作账号配置保存。AdsPower 桌面版仍需要在桌面版中登录并启用 Local API；仅填写环境配置文件不会自动启动 AdsPower。

Kubernetes 从 `adspower-api` Secret 的 `api-key` 字段读取密钥。用下面的命令从 `.env.deploy` 同步：

```sh
uv run python scripts/sync_adspower_secret.py --context YOUR_CONTEXT
# 非默认命名空间可加 --namespace YOUR_NAMESPACE
```

此命令会创建/更新所选集群的 Secret，使用 stdin 传递内容，不打印密钥；命名空间需要已存在。更新后手动重启使用它的 Pod，密钥才会生效。

账号还需要保存 `browser_provider=adspower` 和 `adspower_profile_id` 的稳定绑定。在 Web 账号管理中选择 AdsPower 后，控制台会通过 Local API 读取当前 API key 可见的环境列表供选择，不需要手工复制 ID；任务 TOML 的 `[environment]` 仍可直接配置这两个字段。API 地址只允许 loopback；应用和 AdsPower 在不同容器时需共享网络命名空间。Kubernetes 使用同 Pod sidecar；Compose 使用 `docker compose --env-file .env.deploy --profile adspower up --build --wait` 启用可选 sidecar。

AdsPower 的 profile 是服务端管理的一条独立浏览器环境记录，包含自己的代理、指纹和登录数据；它不是项目 `profiles/` 下的 Chromium 目录。桌面与容器的代理地址不同（例如容器使用 loopback 或 `host.docker.internal` 中继）时，应分别使用两个 profile，避免一边修改代理后破坏另一边。

XHS / RedNote 的 AdsPower 浏览器模式以 profile 中的代理和登录状态为准，账号不需要填写项目 Cookie，也可以不选择项目代理。采集器不会比较或覆盖 profile Cookie。出口检测只通过该 AdsPower 浏览器执行，下载媒体也复用同一个浏览器网络栈，不会回退到容器直连。

抖音可选择 AdsPower 作为 WebSign 浏览器：登录状态、WebSecSDK 签名和 Cookie 来自 profile，签名后的数据请求仍由 `curl_cffi` 发出。AdsPower profile 未配置代理时，项目代理可留空，两条链路都使用直连；profile 配置代理时，账号应选择出口一致的项目代理。任务预检会同时实测两条链路，IP 不一致时拒绝运行。项目不读取 AdsPower 的代理凭据，也不会自动把 profile 代理复制给 `curl_cffi`。XHS / RedNote 的 AdsPower / Kameleo HTTP 接口模式同样允许两边直连并实测出口，但仍使用项目 Cookie；非托管浏览器的 `httpx` / `curl_cffi` 接口模式仍须使用项目 Cookie 和项目代理。

## Kameleo（本机试用）

首版接入面向本机已经安装的 Kameleo 桌面应用，支持 XHS / RedNote 的桌面 Chrome（Chroma）profile。先启动 Kameleo，再在 `.env.local` 保留或修改 Local API：

```dotenv
CRAWLER_KAMELEO_API_URL=http://127.0.0.1:5050
CRAWLER_KAMELEO_START_TIMEOUT=90
```

在 Web 账号管理中选择 Kameleo 后，控制台会通过 Local API 读取当前工作区的 profile 列表供选择，并将所选 UUID 保存为账号绑定；TOML 对应 `browser_provider=kameleo` 和 `kameleo_profile_id`。API 地址只允许 loopback。程序通过 Kameleo 的 Playwright CDP 地址连接，运行结束会停止 profile；同一个 UUID 不能分配给多个账号。当前采集器只允许选择桌面 Chrome（Chroma）profile。

Kameleo profile 自己管理登录状态和代理，因此浏览器采集不需要填写项目 Cookie，出口检测和媒体下载也不需要项目代理；采集器不会比较、导入或覆盖 Kameleo profile Cookie。`httpx` / `curl_cffi` 仍是独立网络栈，使用这些模式时必须配置项目 Cookie 并另选项目代理。当前不支持 Kameleo mobile、Firefox/Junglefox、Docker sidecar 或 Kubernetes 部署。

## 全局配置清单

| 类别 | 环境配置字段 | 用途 / 默认 |
| --- | --- | --- |
| 数据库 | `CRAWLER_DATABASE_URL` | 本地脚本不设时启动自带 PostgreSQL；设置后连接现有数据库。直接 CLI/Web 也可读取 `data/postgres.url`；未配置数据库时启动报错 |
| Web | `CRAWLER_WEB_HOST`、`CRAWLER_WEB_PORT` | 本地 `127.0.0.1:8765`；Compose 内固定监听 `0.0.0.0` |
| 并发 | `CRAWLER_WORKERS` | 模板和本地脚本为 2；直接 Web 未配置为 1；同账号仍串行 |
| 数据路径 | `CRAWLER_ROOT`、`CRAWLER_LOCAL_DIR` | 前者供 Web/本地脚本使用；后者是本地脚本旧名。脚本默认启动目录下 `social-crawler-local/`，直接 Web 默认当前目录 |
| 媒体路径 | `CRAWLER_ARTIFACTS_DIR` | 默认运行根目录 `artifacts/`；Compose 使用已挂载的 `/app/artifacts` |
| 本地安装 | `CRAWLER_SKIP_SETUP` | `1` 跳过依赖与浏览器安装；首次运行先安装依赖 |
| Host 校验 | `CRAWLER_WEB_ALLOWED_HOSTS` | 额外允许的 Host，逗号分隔 |
| AdsPower | `ADS_API_KEY`、`CRAWLER_ADSPOWER_API_URL`、`CRAWLER_ADSPOWER_START_TIMEOUT` | 启动密钥、Local API 地址、启动超时秒数（5–900，默认 90） |
| Kameleo | `CRAWLER_KAMELEO_API_URL`、`CRAWLER_KAMELEO_START_TIMEOUT` | 本机 Local API 地址（默认 `127.0.0.1:5050`）、启动超时秒数（5–900，默认 90） |
| Agent | `ANTHROPIC_API_KEY`、`CRAWLER_AGENT_API_URL`、`CRAWLER_AGENT_MODEL` | 默认官方 Anthropic 地址和 `sonnet`；页面保存值优先 |
| Agent 兼容 | `ANTHROPIC_BASE_URL` | 未设置 `CRAWLER_AGENT_API_URL` 时回退 |
| 浏览器 | `CRAWLER_DEFAULT_HEADLESS`、`CRAWLER_BROWSER_EXECUTABLE_PATH` | 初始账号无头模式、本地浏览器可执行路径；Compose 镜像内浏览器路径固定 |
| 账号代理 | 自定义变量，如 `CRAWLER_XHS_PROXY` | 在账号 `proxy_env` 中引用变量名，或继续使用 `proxy_file`。不引用不会自动生效 |
| 代理中继 | `CRAWLER_PROXY_RELAY_HOST`、`CRAWLER_PROXY_RELAY_MAP` | macOS/OrbStack 可选中继，普通环境留空 |
| Compose | `POSTGRES_PASSWORD` | 内置 PostgreSQL 的初始密码；不采用本机 `CRAWLER_DATABASE_URL` |
| Compose 端口 | `CRAWLER_COMPOSE_BIND_HOST`、`CRAWLER_POSTGRES_PORT` | 宿主机绑定默认 `127.0.0.1`，PostgreSQL 端口为 5433 |
| 部署标识 | `CRAWLER_IMAGE_DIGEST` | 验证报告中的镜像摘要，不影响采集行为 |
| AdsPower 容器高级项 | `ADS_KERNEL_VERSION`、`PORT`、`ADS_HEALTH_TIMEOUT`、`ADS_HEALTH_FAILURES`、`ADS_READY_FILE` | 内核预热、Local API 端口、健康检查超时与连续失败阈值、ready 文件；K8s 在 overlay 注入，不由 Secret 同步脚本修改 |

Compose 的数据库密码已经用于初始化卷后，仅编辑 `.env.deploy` 不会自动修改 PostgreSQL 内部的用户密码。不要通过删除数据卷来更新密码。

## 按账号、任务保存的配置

账号的代理、Cookie 和 profile 分别配置，任务单独设置采集范围。可以通过 Web 页面管理，也可以写入 TOML：

CLI 示例可从 `configs/canary-xhs.toml` 复制到 Git 忽略的 `config.local.toml`：

```sh
cp configs/canary-xhs.toml config.local.toml
uv run social-crawler run --config config.local.toml --cookies data/cookies/xhs.json --online
```

| 所在位置 | 可修改字段 |
| --- | --- |
| Web 账号 / TOML `[environment]` | `account_ref`、`cookie_file`、`profile_dir`、`browser_provider`、`browser_channel`、`adspower_profile_id`、`adspower_api_url`、`adspower_start_timeout`、`kameleo_profile_id`、`kameleo_api_url`、`kameleo_start_timeout`、`headless` |
| 同上：网络和身份 | `proxy_env`、`proxy_file`、`expected_user_id`、`user_agent`、`impersonate`、`locale`、`timezone_id`、`douyin_webid` |
| 同上：会话一致性 | `session_version`、`binding_version`、`auto_session_recovery`、`consistency_policy` |
| Web 任务 / TOML `[run]` | `platform`、`source_type`、`keywords`、`post_targets`、`sort`、`adapter`、`headless`、`download_media` |
| 同上：数量与预算 | `content_limit`、`comment_limit`、`unlimited_comments`、`reply_parents`、`reply_limit`、`max_requests`、`max_seconds`、`max_pages`、`max_no_progress`、`min_interval`、`request_timeout`、`network_retries` |
| 开发探针 `[run]` | `seed_contents`：手动提供种子帖子，不是通用运行配置 |
| Web 定时任务、账号池、系统配置 | 时间表、时区、启停、账号分配、任务默认值等保存在 PostgreSQL，通过相应页面管理 |

浏览器 UA / `impersonate` 与镜像内 Chrome 版本配套，不建议单独修改。账号网络绑定、session 回执等由程序生成，不应当作普通配置手工改写。

## 部署与命令行选项

- `deploy/k8s/base/`、`deploy/k8s/components/`、`deploy/k8s/overlays/`：镜像、CPU/内存、PVC/存储类、副本、探针、Xvfb、Service 等基础设施设置仍由 Kubernetes YAML 管理；现有数据库 Secret 为 `social-crawler-secrets/postgres-password`。全局应用变量可以通过容器 `env` / Secret 注入，优先于文件。
- AdsPower runtime 高级项在 `deploy/docker/adspower/entrypoint.sh` / `healthcheck.sh` 使用；若直接通过通用加载器启动该 entrypoint，`.env.deploy` 中的这些变量会传入：`CRAWLER_ENV_FILE=.env.deploy uv run python -m social_crawler.config --exec sh deploy/docker/adspower/entrypoint.sh`。此入口需要已安装 AdsPower CLI 的运行环境，通常在镜像内执行。
- `Dockerfile` / `deploy/docker/adspower/Dockerfile` 的 `ARG`：uv、Playwright、Chrome 版本与校验和、AdsPower CLI 版本、基础镜像属于构建输入；用 `docker build --build-arg` 修改，需要重新构建。
- CLI 的 `--database-url-file`、`--output`、Cookie 捕获脚本的输出路径/超时/通道，以及临时恢复预算等单次操作选项保留为命令行参数，详见各入口 `--help`。
- `alembic.ini` 是迁移工具配置；Web 启动会使用有效数据库地址迁移，一般不需要手工改它。
