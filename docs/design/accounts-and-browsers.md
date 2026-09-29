# 账号、浏览器与网络

## 账号环境

[EnvironmentConfig](../../src/social_crawler/environments/session.py) 定义账号标识、Cookie 文件、持久 profile、浏览器 provider、代理、预期用户身份、会话版本及环境一致性策略。账号配置保存在数据库中，Cookie 和浏览器状态保存在运行目录；备份时需要同时保存。

各平台使用独立账号环境。Chromium profile 是本地目录；AdsPower 和 Kameleo 使用由部署者填写的 profile ID，Local API 地址仅允许 loopback。同一账号、profile 或冲突代理资源不能同时被采集、登录或浏览器查看占用。Kameleo 首版仅接入 XHS / RedNote 的本机桌面 Chrome profile；AdsPower 还可用于抖音 WebSign。

托管浏览器 profile 是浏览器采集的登录状态与代理所有者。AdsPower 与 Kameleo 浏览器模式不要求项目 Cookie，也不会比较、注入或覆盖 profile Cookie；身份仍在真实平台响应中在线验证。网络以 provider + profile ID 的不可逆摘要和浏览器实测出口建立绑定，不读取或持久化其代理凭据；媒体通过该浏览器的 CDP 网络流下载。抖音 AdsPower 是混合链路：浏览器 profile 提供登录态和 WebSign，数据请求仍由 `curl_cffi` 通过项目代理或直连发出，运行前必须确认两个出口一致。XHS / RedNote 的托管浏览器 HTTP 接口模式同样比较浏览器与 HTTP 出口，并继续使用项目 Cookie。非托管浏览器的独立 HTTP 适配器仍使用项目 Cookie 与代理池。

## 登录与浏览器查看

[AccountBrowser](../../src/social_crawler/environments/account_browser.py) 复用账号现有 profile。打开窗口会保留已有登录。点击“保存登录并关闭”后，程序校验身份并导出 Cookie；验证失败则保持窗口打开。

Linux 容器通过 [KasmVNC](../../deploy/docker/kasmvnc/README.md) 为账号启动独立桌面。HTTP 资源和 WebSocket 经 `/browser/<session>/` 走控制台同源网关；上游只监听 loopback，使用随机口令，网关使用会话范围的 HttpOnly Cookie。本机没有 KasmVNC 时使用原生浏览器窗口。

页面停止发送心跳后约 120 秒，浏览器会话自动关闭；也可手动关闭。托管浏览器启动时使用账号桌面的 display；AdsPower 容器 sidecar 与应用共享 X11 socket，Kameleo 当前只支持本机桌面应用。

有头的托管浏览器采集为每个运行任务分配独立 KasmVNC display，并把该 display 传给 profile 启动请求。控制台可将同一只读画面同时呈现在任务详情和账号池查看窗口中，不会再次启动 profile；任务结束时桌面随浏览器一起释放。本机没有 KasmVNC 时仍使用 AdsPower / Kameleo 的原生窗口。

## 网络与环境一致性

代理来自账号引用的环境变量或私有文件。设置了代理却无法读取或连接时停止，不自动降为直连。可选宿主机 TCP 中继完全由 `CRAWLER_PROXY_RELAY_HOST` 和 `CRAWLER_PROXY_RELAY_MAP` 配置，不内置上游 IP。

[network.py](../../src/social_crawler/environments/network.py) 对比 HTTP 与浏览器出口，并保存观测记录。纯浏览器托管模式只使用 profile 出口；抖音 AdsPower 以及 XHS / RedNote 托管浏览器 HTTP 模式同时观测 profile 浏览器与 HTTP 请求，双方都未配置代理时按直连出口比较，有任一方使用代理时仍要求实测出口一致。首次在线运行在出口一致时自动记录绑定；通过控制台主动更换代理会清除旧绑定并在下次运行重新初始化。配置未变但实际出口漂移时仍会停止任务。CLI 继续通过显式执行 `bind-network` 更新出口绑定。

登录保存浏览器环境快照，HTTP 路径复用相应 User-Agent / Client Hints。`consistency_policy` 支持 `off`、`warn`、`strict`：默认记录差异，严格模式对快照缺失和高严重度不一致阻止执行。具体比较字段见 [environment_snapshot.py](../../src/social_crawler/environments/environment_snapshot.py)。

## 账号状态与恢复

人工启用 / 禁用控制任务分配，“已保存登录”仅表示本地有凭据。打开账号列表不发起平台诊断；手动停用不因重新登录而自动启用。删除账号保留历史运行及本地会话文件。

自动冷却默认关闭：错误仍停止当前任务并记录，明确登录失效仍要求处理。开启后按错误进入等待，到期执行有限恢复探针和 canary，成功后恢复调度。

账号还可单独开启自动会话恢复：登录失效后，程序尝试从现有 profile 导出一次登录资料。身份不匹配、验证码或恢复失败时需要人工处理。手动禁用的账号仍保持禁用。源码分别位于 [session_recovery.py](../../src/social_crawler/environments/session_recovery.py) 与 Web 的恢复派发逻辑。

## 访问控制

控制台供本地操作者使用，账号设置可能展示 Cookie 和代理凭据。它没有多用户登录认证，Compose 默认绑定本机，Kubernetes 使用 ClusterIP 与端口转发；其他访问方式需要部署者提供认证与隔离。

相关回归见 [test_account_browser.py](../../tests/test_account_browser.py)、[test_browser_gateway.py](../../tests/test_browser_gateway.py)、[test_adspower.py](../../tests/test_adspower.py)、[test_kameleo.py](../../tests/test_kameleo.py)、[test_session_recovery.py](../../tests/test_session_recovery.py) 和 [test_network.py](../../tests/test_network.py)。
