# 参与贡献

这是一个个人项目，按自己的节奏维护，不保证响应时效。欢迎 issue 和 PR，但在动手写代码前请先开一个 issue 说明问题与思路，避免做完发现方向不一致。

## 开发环境

需要 Python 3.13 和 [uv](https://docs.astral.sh/uv/)。

```sh
uv sync --locked
uv run playwright install chromium
cp -n .env.example .env
```

浏览器测试需要已安装的 Playwright Chromium，未安装时相关用例会跳过。数据库集成测试与首次在线运行见 [运行维护](docs/operations/runtime.md)。

## 提交前的验证

```sh
uv run ruff check .
uv run pytest -q
python3 scripts/check_public_tree.py
```

三条都必须通过。`pytest` 全量约五分钟；默认会跳过 PostgreSQL 与浏览器相关用例，需要覆盖时按 [运行维护](docs/operations/runtime.md) 的说明启动隔离数据库并用 `scripts/run_tests.py --postgres`。

## 代码风格

- Ruff 负责格式与静态检查，规则见 `pyproject.toml`：`target-version = py313`、`line-length = 100`、`select = ["E4", "E7", "E9", "F", "I"]`。
- 注释写「为什么」，不重复代码本身在做什么；不要为了说明改动而留下过程性注释。
- 沿用现有模块的分层：`domain` 不依赖外部 IO，`adapters` 只处理平台协议与解析，`environments` 管账号与浏览器，`orchestration` 管调度与预算，`interfaces` 只做 CLI/HTTP 的输入输出。
- Web 控制台是原生 HTML/CSS/JS，没有构建步骤；新增第三方前端库需要 vendor 进 `web_static/vendor/` 并同时更新 `manifest.json`、许可证文件和 `THIRD-PARTY-NOTICES.md`。
- 面向用户的文案（控制台、CLI 输出、错误信息）使用中文，与 `web_static/zh-CN.json` 保持一致。

## 测试约定

- 测试只使用合成数据、本地 HTTP 页面和 Mock 服务，**不得**需要目标平台账号、真实 Cookie 或模型密钥。
- 不要把真实平台响应、账号标识或代理端点写进用例。需要网络地址时使用 `example.com` 与文档保留网段（`192.0.2.0/24`、`198.51.100.0/24`、`203.0.113.0/24`、`2001:db8::/32`），`scripts/check_public_tree.py` 会拦下其他地址。
- 新增需要数据库或浏览器的用例时，打上 `postgres` / `browser` 标记，保证默认 `pytest -q` 仍可在无依赖环境下跑完。

## 提交与 PR

提交信息使用 `类型: 简述` 形式（`feat:`、`fix:`、`docs:`、`refactor:`、`test:`、`chore:`），正文说明动机。一个 PR 只解决一件事，并在描述里写清改了什么、为什么、怎么验证的。

改动行为时同步更新对应文档：配置项改 [配置指南](docs/guides/configuration.md)，界面改 [Web 控制台](docs/guides/web-console.md)，模块结构改 [架构](docs/design/architecture.md)，运维流程改 [运行维护](docs/operations/runtime.md)。

## 不要提交的内容

`.env` 及任何私有配置、Cookie、浏览器 profile、代理凭据与端点、运行数据库与媒体产物、真实采集结果、平台账号标识、个人绝对路径、内部域名。这些都已在 `.gitignore` 与 `.dockerignore` 中排除，但忽略规则救不了已经 `git add` 的文件，提交前请自行 `git diff --cached` 确认。

`scripts/check_public_tree.py` 只扫描工作区，**不看 Git 历史、被忽略的文件和二进制内容**。它通过不代表没有泄漏。

## 发布前检查

打标签或推送新远端前：

```sh
git for-each-ref                     # 只应剩 refs/heads 与 refs/tags
python3 scripts/check_public_tree.py
uv build --out-dir dist              # 检查 sdist/wheel 内没有私有目录
```

编辑器和 AI 工具可能写入 `refs/notes/*`、`refs/codex/*` 之类的附加 ref，其中可能带有本地会话元数据。用 `git push origin main` 推送指定分支，**不要**用 `git push --mirror` 或 `--all`。

新增需要随源码分发的顶层目录或文件时，同步更新 `pyproject.toml` 里 `[tool.hatch.build.targets.sdist]` 的 `only-include` 白名单。
