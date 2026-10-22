# BIFROST 项目任务总表

> 最后更新：2026-09-07
> 原则：**PASS 必须有实际测试证据**。代码写出来了不等于 PASS。
> 环境：Windows 原生（WSL2 虚拟化 BIOS 禁用、无 Docker），Go 1.26.8 已装，Bifrost v2.0.0 二进制 + 源码（下载中）

---

## 零、本轮开工前发现的现实差距（必须先说）

上一轮结束时，外界可能认为"项目已经搭好了"。实际状态是：

| 项 | 上一轮结束时的真实状态 | 本轮处理 |
|---|---|---|
| Git 仓库 | **不存在** | 源码包下载中（112MB，断点续传） |
| Go 工具链 | **未安装** | **已装 go1.26.8 windows/amd64** |
| 源码 | 仅 3 个 raw 抓取的文件 | 已补充 governance.go / inference.go / schemas 等，完整包下载中 |
| 可编译 | 否 | 待源码包就绪后验证 |

**这意味着：所有"修改 Go 代码 + 编译 + 单元测试"的任务，在本轮开始时一步都做不了。**
本轮优先攻下了不需要重新编译就能验证的部分，并解锁了编译能力。

---

## 一、P0：必须完成

| # | 任务 | 状态 | 证据 / 说明 |
|---|---|---|---|
| 1 | **Virtual Key → Provider Key 真实绑定** | **PASS** | 见第二节。根因：字段用错（应为 `key_ids`，非 `keys`），已修正 |
| 2 | Provider 调用链验证 | **PASS** | openai(9001) 与 anthropic(9002) 双 provider 均调通 |
| 3 | 跨 Provider fallback | **PASS** | Case A/B/C 全过，见 `fallback验证报告.md` |
| 4 | Key 加权路由 | **未完成** | 未做流量分布实测（10000 次统计）。原因：单 provider 多 key 无法区分流量归属，需要额外 mock 端点 |
| 5 | Provider 健康状态 | **未完成** | 需要改 Go 代码 |
| 6 | 请求失败自动 fallback | **PASS** | 同 #3 Case B |
| 7 | 语义缓存真实验证 | **未完成** | 启动日志中 active 插件**没有 semantic cache**，需确认是否需额外启用/配置 |
| 8 | Streaming 请求验证 | **部分 PASS** | 上一轮已验证 SSE 逐块返回；本轮未重跑，未做 cancel/goroutine 检查 |
| 9 | UI 核心功能验证 | **部分 PASS** | 8080 可访问、API 全通；未做页面级交互验证（浏览器自动化未装） |
| 10 | API + UI + Provider 完整链路 | **PASS** | Client→VK→Auth→Policy→Key 选择→Provider→Response 已端到端跑通 |

---

## 二、P0-1 详情：Virtual Key 绑定问题定位与修复

### 现象
上一轮 `allow_all_keys` 和显式绑 key id 都失败，一直报
`no keys found for provider: openai and model: mock-model`

### 定位过程（不靠猜，逐层追）
1. 从报错字符串反查：`core/bifrost.go:8419` 在 `selectKeyFromProviderForModelWithPool`(8400)
2. 关键：`bifrost.account.GetKeysForProvider(ctx, providerKey)`（8414）**收 ctx** → key 按 ctx 中的 VK 过滤
3. 查 VK 实际存储状态 → 发现 `allow_all_keys=False, keys=[]`（我传的值根本没存进去）
4. 读 `handlers/governance.go:1784-1796`：
   ```go
   allowAllKeys := false
   if pc.KeyIDs.IsUnrestricted() { allowAllKeys = true }
   else if !pc.KeyIDs.IsEmpty() { keys, err = h.configStore.GetKeysByIDs(ctx, pc.KeyIDs) }
   ```
5. 读 `governance.go:205` 结构体定义确认：
   ```go
   KeyIDs schemas.WhiteList `json:"key_ids,omitempty"`  // List of DBKey UUIDs
   ```

### 根因（**是我的测试脚本用错 API，不是项目 bug**）
| 我上一轮用的 | 正确字段 |
|---|---|
| `keys: ["<uuid>"]` | **`key_ids: ["<uuid>"]`** |
| `allow_all_keys: true`（作为输入） | 该字段是**计算产物**，不是输入；用 `key_ids: ["*"]` 表达 |
| PUT 不带 `id` | 导致每次 PUT 都**新建** provider config（id 从 1 涨到 4），不是更新 |

### 修复与实测结果
```
创建：POST /api/governance/virtual-keys
      {"name":"demo-vk","is_active":true,
       "provider_configs":[{"provider":"openai","key_ids":["*"],"allowed_models":["*"],"weight":1.0}]}

返回：allow_all_keys=True  ← 打通
```

| 用例 | 结果 |
|---|---|
| 正确 VK → 已授权 openai | **200**，内容正确 |
| 伪造 VK | **401 `virtual_key_not_found`** |
| 正确 VK → 未授权 anthropic | **403 `provider_blocked`** |
| 不带 Authorization | 放行（**Bifrost 默认不强制认证**，是设计选择不是缺陷） |

---

## 三、未完成项的真实原因（不粉饰）

### P0-4 加权路由
- **限制**：同一 provider 下多个 key 共用同一个 base_url，无法区分请求落到了哪个 key。
- **要解开需要**：给每个 key 配独立的 mock 端点（不同端口），再统计分布。
- **下一步**：起 2~3 个 mock，在同一 provider 下建多个不同 base_url 的 key —— 但 Bifrost 的 base_url 是 **provider 级别**而非 key 级别，
  需要先确认 key 是否支持独立 endpoint（**未确认**）。

### P0-7 语义缓存
- **实测事实**：v2.0.0 启动日志中 active 插件为
  `telemetry / prompts / logging / governance / routing / compat / model-catalog-resolver`——**没有 semantic cache**。
- **待确认**：是需要额外启用，还是需要配置 vector store，或属于企业版。
- **已知机制**（源码级）：`core/bifrost.go:5351-5382` 插件可在 `RunLLMPreHooks` 短路，
  语义缓存在此实现不走下游。机制清楚，但**未实测**。

### P0-8 Streaming
- 上一轮已验证 SSE 逐块返回与 `chunk_index` 递增。
- **未做**：context cancel、客户端断开后的 goroutine 回收、流式下的 fallback 行为。

### P0-9 UI
- 8080 可访问，所有 API 实测通过。
- **未做**：页面级交互验证。`agent-browser` 安装 8 分钟未完成且需下 500MB Chromium，按约定不作为硬依赖。

---

## 四、P1：强烈建议完成（全部未开始，均需写 Go 代码）

| # | 任务 | 状态 | 阻塞原因 |
|---|---|---|---|
| 1 | 自适应路由（Adaptive Routing） | 未开始 | 需改 `core/keyselectors/`，需编译验证 |
| 2 | Provider 延迟统计 | 未开始 | 同上 |
| 3 | Provider 成功率统计 | 未开始 | 同上 |
| 4 | Key 健康度 | 未开始 | 同上 |
| 5 | 熔断/冷却机制 | 未开始 | 同上 |
| 6 | 请求级 Trace | 部分具备 | 源码中 tracer 已存在（fallback 每次开 span），未验证输出端 |
| 7 | 错误分类 | 部分具备 | 已有 `AllowFallbacks`、`RequestCancelled` 等分类，未系统梳理 |
| 8 | Routing Dashboard | 未开始 | 需改 UI |
| 9 | Cache Dashboard | 未开始 | 依赖 #P0-7 先确认缓存是否可用 |
| 10 | Fallback Dashboard | 未开始 | 需改 UI |

---

## 五、P2：项目增强（全部未开始）

| # | 任务 | 状态 |
|---|---|---|
| 1 | Prometheus metrics | 未开始（源码有 telemetry 插件，未验证 `/metrics`） |
| 2 | 日志结构化 | 未开始 |
| 3 | 性能测试（100/500 并发） | 未开始 |
| 4 | 并发测试 | 未开始 |
| 5 | 压测 | 未开始 |
| 6 | README | 未开始 |
| 7 | 架构图 | 未开始 |
| 8 | 面试技术总结 | 未开始 |

---

## 六、本轮新增的可复用资产

| 文件 | 用途 |
|---|---|
| `C:\Users\29469\.workbuddy\toolchain\bifrost\mock_provider_v2.py` | **双协议 mock**（OpenAI + Anthropic）+ `/v1/models` + **运行时故障注入** |
| `C:\Users\29469\.workbuddy\toolchain\go\bin\go.exe` | Go 1.26.8 工具链 |

### mock v2 的故障注入（关键能力，测 fallback/熔断全靠它）
```bash
# 让 provider 立刻开始失败（无需重启）
curl -X POST http://127.0.0.1:9001/control -d '{"mode":"fail","status":500}'
# 变慢
curl -X POST http://127.0.0.1:9001/control -d '{"mode":"slow","ms":3000}'
# 恢复 + 计数清零
curl -X POST http://127.0.0.1:9001/control -d '{"mode":"reset"}'
# 查调用次数（用于证明 provider 真的被调用 / 没被调用）
curl http://127.0.0.1:9001/health   # served=x failed=y
```

### 顺带修掉的一个坑
上一轮 provider key 状态一直是 `list_models_failed`（因为 v1 mock 没实现 `/v1/models`）。
v2 补上后，key 状态变为 **`success`** —— 这印证了当时的猜测，也说明
**Bifrost 建 key 时会主动探测 provider 的 `/v1/models`**。

---

## 七、下一步（按解锁顺序）

1. **等源码包下载完成** → 解压 → `go build ./...` 验证工具链可用（关键门禁）
2. `go mod download`（Bifrost 依赖 Azure/GCP/AWS SDK，体积大，可能很慢）
3. 打通编译后，按 P1 顺序实现：自适应路由 → 健康度 → 熔断 → 补加权路由测试
4. 确认语义缓存是否可用，再决定是否做 Cache Dashboard
5. 性能测试 + goroutine 泄漏检查（pprof 端点在 `main.go:142` 已启动）
