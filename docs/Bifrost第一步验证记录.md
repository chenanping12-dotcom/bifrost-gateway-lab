# Bifrost 第一步验证记录（可行性实证）

> 日期：2026-09-06
> 目标：回答"这个第一步怎么跑"——在**本机 Windows 环境**下把 Bifrost 真正跑起来，并留下可复现的证据
> 结论：**跑通了**。网关启动、Web UI、非流式、流式（SSE）全部验证通过，并额外完成了 provider 宕机/恢复测试。
> 诚实声明：本文所有数字均为本机实测。**未使用真实 LLM**，provider 是本地 mock（零成本）。

---

## 一、先说结论

| 验证项 | 结果 | 实测证据 |
|---|---|---|
| 网关启动 | 通过 | 监听 `127.0.0.1:8080`，启动耗时 11.5 秒 |
| Web UI | 通过 | `GET /` → 200 |
| 健康检查 | 通过 | `{"components":{"db_pings":"ok"},"status":"ok"}` |
| 非流式调用 | 通过 | 200，内容回显正确 |
| 流式（SSE） | 通过 | 逐块返回，`chunk_index` 递增 0→1→2 |
| Provider 宕机 | 通过 | 502 `provider_connection_failed`，**2.3ms 快速失败** |
| Provider 恢复 | 通过 | 重启后自动恢复，网关无需重启 |

**最重要的判断：可行性没问题，可以干。**

---

## 二、本机环境探测结果（这一步决定了路线）

| 组件 | 状态 | 影响 |
|---|---|---|
| Node.js | v22.22.2（已有） | 可用 |
| Docker | **未安装** | 官方 `docker run` 路线暂时走不了 |
| Go | **未安装**（2026-09-07 已装 go1.26.8，见 `BIFROST_NEXT_TASKS.md`） | 无法从源码 `go build`（当时） |
| Ollama | **未安装** | 无法用本地真实模型 |
| WSL2 | 已安装但**虚拟化在 BIOS 被禁用** | **WSL2 + Ubuntu 路线走不通**，需进 BIOS 开启 |

**这意味着报告里 Phase 0 写的"WSL2 + Ubuntu"和"docker run"两条路，在你这台机器上当前都不可用。**
所以本记录改走 **Windows 原生二进制** 路线——结果证明这条路完全可行，且更快。

> 后续如果要接 Prometheus/Grafana（Phase 4/5），再补 Docker 也不迟。
> 现阶段**不要**为了跑第一步就去装 Docker Desktop 或折腾 BIOS，那是把时间花在环境上而不是代码上。

---

## 三、实际执行步骤（可复现）

### 3.1 下载二进制（绕过 npx，推荐）

官方给的 `npx -y @maximhq/bifrost` 能用，但**有两个坑**（见第六节），我改成了直接下载：

```bash
# 1. 查最新版本号
curl -s https://getbifrost.ai/latest-release
# → {"name":"v2.0.0","changelogUrl":"..."}

# 2. 直接拉 Windows 二进制（112.1 MB，支持断点续传）
curl -L -C - --retry 5 --retry-delay 2 --retry-all-errors \
  -o bifrost-http.exe \
  https://downloads.getmaxim.ai/bifrost/v2.0.0/windows/amd64/bifrost-http.exe
```

实测：约 290~310 KB/s，耗时约 6 分钟，最终大小 **117,577,216 字节**。

### 3.2 启动

```bash
bifrost-http.exe -port 8080 -host 127.0.0.1 ^
  -app-dir "C:\Users\29469\.workbuddy\toolchain\bifrost\appdata" ^
  -log-style pretty
```

一键启动脚本已生成：`start-bifrost.bat`（GBK 编码 + CRLF，双击即可）。

### 3.3 启动参数（从 `-h` 实测）

| 参数 | 默认值 | 说明 |
|---|---|---|
| `-port` | `8080` | 监听端口 |
| `-host` | `localhost` | 可用 `BIFROST_HOST` 环境变量覆盖 |
| `-app-dir` | — | 应用数据目录（config.db + logs.db） |
| `-log-level` | `info` | debug / info / warn / error |
| `-log-style` | `json` | json / pretty |

---

## 四、关键发现：Provider 配置方式

**Bifrost v2.0.0 没有 `config.json`，配置存在 SQLite（`config.db`）里**，所以必须走 API：

```bash
# 1. 创建 provider
POST /api/providers
{"provider":"openai","keys":[],"network_config":{"base_url":"http://127.0.0.1:9001"}}

# 2. 给 provider 加 key（key 是独立资源，不在 provider 的 POST 里）
POST /api/providers/{provider_name}/keys
{"name":"mock-key","value":"sk-mock-123","models":["mock-model"],"weight":1}

# 3. 调用时模型名格式为 {provider}/{model}
POST /v1/chat/completions
{"model":"openai/mock-model","messages":[...]}
```

**注意：只创建 provider 不加 key，调用会报**
`no keys found for provider: openai and model: mock-model`

---

## 五、实测记录（原始输出）

### 5.1 健康检查

```
GET /health → 200
{"components":{"db_pings":"ok"},"status":"ok"}
```

### 5.2 未配 provider 时（证明请求确实进了内核）

```
POST /v1/chat/completions {"model":"gpt-4o-mini",...} → 400
{"error":{"message":"could not auto resolve a provider for the request, please specify a provider explicitly"}}
```

这个 400 是**好消息**——它证明请求穿过 HTTP 层进了路由内核，只是没有可解析的 provider。

### 5.3 配好 provider 后（完整链路打通）

```
POST /v1/chat/completions {"model":"openai/mock-model",...} → 200
{"id":"chatcmpl-mock-A-2","choices":[{"message":{"content":"[mock-A] request#2 echo: hello via gateway"}}],
 "usage":{"prompt_tokens":4,"completion_tokens":10,"total_tokens":14},
 "extra_fields":{"routing_info":{"provider":"openai","model":"mock-model","key":"mock-key"}}}
```

### 5.4 流式（SSE）

```
data: {... "delta":{"content":"[mock-A] "} ... "chunk_index":0}
data: {... "delta":{"content":"request#3 "} ... "chunk_index":1}
data: {... "delta":{"content":"echo: "} ... "chunk_index":2}
data: {... "delta":{"content":"stream "} ... "chunk_index":3}
```

逐块返回，`chunk_index` 严格递增——**网关的 SSE 透传是正常工作的**。

### 5.5 故障测试：provider 宕机

杀掉 9001 端口的 mock provider 后：

```
POST /v1/chat/completions → 502，耗时 0.002279s
{"status_code":502,"error":{"type":"provider_connection_failed",
 "message":"network error occurred while connecting to provider API (DNS lookup, connection refused, etc.)"}}
```

**2.3 毫秒就失败了**——没有长阻塞、没有雪崩，这是正确的快速失败行为。

### 5.6 恢复测试

重启 mock provider（**不重启网关**），下一次调用立即成功：

```
{"content":"[mock-A] request#1 echo: provider recovered"} → 200
```

网关自动恢复，无需任何干预。

---

## 六、踩过的坑（很重要，省你两小时）

1. **npx 的下载器太慢，且中断会留下损坏缓存。**
   112 MB 的包，Node 的 stream 下载 3 分钟只到 34%。中断后文件残留在
   `%LOCALAPPDATA%\bifrost\v2.0.0\bin\`，而 npx 用 `existsSync` 判断，
   **下次会直接执行损坏的二进制**。
   → 解决：改用 `curl -C -` 直接下载，并把残留目录删掉。

2. **不要只用 HTTP 状态码判断 API 是否存在。**
   `/api/providers`、`/api/config`、`/api/nonexistent` 全都返回 200——
   因为未匹配路径会 fallback 到 SPA 首页。**必须看响应体**（真 API 返回 JSON，fallback 返回 HTML）。

3. **`openai-compatible` 是个陷阱。**
   `POST /api/providers` 会**接受**这个名字（返回 200），但加 key 时报错
   `unsupported provider: openai-compatible`。
   → **配置层校验不严格：创建成功 ≠ 可用。** 做故障转移实验时必须用真实 provider 类型。

4. **Git Bash 下 `taskkill //PID` 参数会被路径转换破坏**（报"无效参数/选项"）。
   → 用 PowerShell 的 `Stop-Process -Id <pid> -Force`。

5. **`rm -rf` 在 `$LOCALAPPDATA` 下会被安全策略拦截**（路径解析混乱）。
   → 用 PowerShell 的 `Remove-Item -LiteralPath`。

6. **8080 端口**：之前被 `ApplicationWebServer.exe` 占用，本次实测时已释放。
   如果再被占，用 `-port 8081` 换端口即可。

---

## 七、本次用到的文件

| 文件 | 路径 | 说明 |
|---|---|---|
| 二进制 | `C:\Users\29469\.workbuddy\toolchain\bifrost\bifrost-http.exe` | v2.0.0，112 MB |
| 启动脚本 | `C:\Users\29469\.workbuddy\toolchain\bifrost\start-bifrost.bat` | 双击启动，GBK+CRLF |
| Mock provider | `C:\Users\29469\.workbuddy\toolchain\bifrost\mock_provider.py` | 零依赖 OpenAI 兼容 mock |
| 配置与日志 | `C:\Users\29469\.workbuddy\toolchain\bifrost\appdata\` | config.db / logs.db |

启动 mock provider（可起多个，用于后续负载均衡/故障转移实验）：

```bash
python mock_provider.py 9001 mock-A 0     # 参数：端口 标签 延迟毫秒
python mock_provider.py 9002 mock-B 0
```

---

## 八、下一步（Phase 1 的第一件事）

第一步的目的已经达到：**用最小代价证明这条路走得通**。接下来别急着学 Go，先做这两件事：

1. **打开 Web UI 熟悉功能**：http://127.0.0.1:8080
   把 provider / key / governance 的配置界面点一遍，比读文档快。

2. **让 Agent 帮你拆主干架构**（报告里给的原话，现在可以执行了）：

   > "帮我分析 `core/bifrost.go` 和 `transports/bifrost-http`，
   > 画出从 HTTP 请求进入到调用 provider 的完整调用链，标出每一步的文件与行号。"

**待补（不急，按需）**：
- Ollama：想在本地跑真实模型时装（比 mock 更真实，但要多下几百 MB + 模型）
- Docker：Phase 4/5 要上 Prometheus/Grafana 时再装
- Go 工具链：**已于 2026-09-07 安装 go1.26.8**（见 `BIFROST_NEXT_TASKS.md` 第零节）

**未验证、不要写进简历**：官方自称的 11 µs 开销 / 5000 RPS——本文没有测性能，
Phase 5 用 mock provider 自测后再填。
