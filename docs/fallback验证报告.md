# Bifrost 跨 Provider Fallback 验证报告

> 日期：2026-09-07
> 版本：Bifrost v2.0.0（Windows 原生二进制）
> 方法：**真实制造失败**，用 provider 侧调用计数交叉验证，不靠响应内容猜
> 环境：Primary = `openai` → mock-A(9001)；Fallback = `anthropic` → mock-B(9002)

---

## 零、验证方法说明

**关键设计：故障注入 + 双侧计数**

1. mock provider 支持运行时切换状态（`POST /control`），**无需重启**
2. 每个 mock 自己统计 `served` / `failed`
3. 判断 fallback 是否发生，看的是 **provider 侧的计数**，而不只是返回内容
4. 另外交叉验证响应里的 `routing_info`（网关自己声明 `is_fallback`）

**fallback 的正确请求格式**（踩坑后确认）：

```json
{
  "model": "openai/mock-model",
  "messages": [{"role": "user", "content": "..."}],
  "fallbacks": ["anthropic/mock-model"]     // ← 字符串数组，"provider/model"
}
```

> 踩坑：我最初写成对象数组 `[{"provider":"anthropic","model":"mock-model"}]` → 400。
> 源码依据：`handlers/inference.go:367`
> `Fallbacks []string \`json:"fallbacks"\` // Fallback providers and models in "provider/model" format`

---

## 一、Case A：Primary 成功，不应调用 Fallback

**前置**：A/B 均健康，计数清零。

| 指标 | 结果 | 判定 |
|---|---|---|
| HTTP | 200 | PASS |
| 返回内容 | `[mock-A] req#1: case A` | 来自 primary |
| A served / failed | 1 / 0 | primary 被调用 |
| **B served / failed** | **0 / 0** | **fallback 未被调用 PASS** |

**结论：PASS** —— 主 provider 正常时，备用完全不参与。

---

## 二、Case B：Primary 失败 → Fallback 成功

**前置**：`POST /control {"mode":"fail","status":500}` 注入到 A。

| 指标 | 结果 |
|---|---|
| HTTP | **200**（用户端无感知） |
| 返回内容 | `[mock-B] anthropic#1: case B fallback` |
| A served / failed | 1 / **1**（主确实失败了） |
| B served / failed | **1** / 0（备用确实被调用） |

**响应中的 routing_info（网关自证）**：

```json
"routing_info": {
  "provider": "anthropic",
  "model": "mock-model",
  "key": "anthropic-key",
  "is_fallback": true,
  "primary_provider": "openai",
  "primary_model": "mock-model"
}
```

**结论：PASS** —— 跨 provider fallback 真实生效，且网关在响应里**明确标注了这是 fallback 结果**，
并保留原始 provider 信息。这对可观测性很有价值（用户看到 200，但运维能看到走了备用）。

---

## 三、Case C：A/B 都失败 → 返回 Primary 的错误

**前置**：A fail(500)，B fail(503)。

| 指标 | 结果 |
|---|---|
| HTTP | 500 |
| 错误消息 | `injected failure from mock-A` ← **A 的错误，不是 B 的 503** |
| `routing_info.provider` | `openai`（primary） |
| `routing_info.key` | `mock-key`（primary 的 key） |

**结论：PASS** —— 精确印证源码 `core/bifrost.go:5166-5168`：

```go
ctx.AppendRoutingEngineLog(... "All %d fallback(s) exhausted; returning primary error (%s)")
return nil, primaryErr
```

**这个设计的两面性**（面试可讲）：
- 好处：primary 的错误通常最有诊断价值。
- 代价：**如果 primary 是配置错误、fallback 是限流，你看到的是配置错误，会掩盖真实原因。**
  生产环境如果只看错误码，可能误判故障范围。

**恢复性验证**：把 A/B 切回 `ok` 后，**网关无需重启**，下一次调用立即成功
（`[mock-A] req#2: after recovery`）。

---

## 四、Case D：RequestCancelled 不触发 fallback

**本轮（2026-09-07 复测）：确定性验证 PASS。**

**方法修正**：上一轮 A 是「慢但成功」，B served=0 存在替代解释（可能只是因为 A 成功）。
本轮让 **A 既慢（3000ms）又最终被客户端取消**——客户端用 `curl --max-time 1` 在 A 返回前断开，
A 仍在后台处理慢请求。此时若 fallback 被误触发，B 一定会被调用（B.served>0）；若不触发，B.served=0。
因为 A 是「慢」不是「成功」，B 不被调用**只能**解释为 cancel 阻断了 fallback。

**前置**：A=`slow(3000ms)`，B=`reset`，双侧计数清零。

| 指标 | 结果 | 判定 |
|---|---|---|
| 客户端 | 中止（http=000，1s 断开） | 取消发生 |
| A served / failed | 1 / 0 | 主 provider 确实被调用（慢请求已抵达） |
| **B served** | **0** | **fallback 未被触发 PASS** |

**结论：PASS。** 客户端取消请求后，尽管主 provider 仍在慢响应，网关**没有**把请求转给备用 provider。
这与源码完全一致：`core/bifrost.go:4896`

```go
// Handle request cancellation
if primaryErr.Error != nil && primaryErr.Error.Type != nil &&
   *primaryErr.Error.Type == schemas.RequestCancelled {
    bifrost.logger.Debug("request cancelled, we should not try fallbacks")
    return false
}
```

**为什么这设计是对的**：用户主动取消了请求，再花钱/花时间在备用 provider 上重算一遍毫无意义，
而且会产生「用户已离开但后台仍在烧 token」的幽灵请求。Bifrost 把 `RequestCancelled` 当成
「不要 fallback」的硬规则，符合直觉。

---

## 五、Case E：`AllowFallbacks=false` 不触发 fallback

**状态：源码级验证 PASS；端到端需插件（未做，如实标注）。**

**机制**：`AllowFallbacks` 是 **BifrostError 自带的字段**（`schemas/bifrost.go:1836`
`AllowFallbacks *bool \`json:"-"\``），由内核/插件在**产生错误时**设置，
**不是请求体可传的字段**。源码 `core/bifrost.go:4903`：

```go
// Check if this is a short-circuit error that doesn't allow fallbacks
// Note: AllowFallbacks = nil is treated as true (allow fallbacks by default)
if primaryErr.AllowFallbacks != nil && !*primaryErr.AllowFallbacks {
    bifrost.logger.Debug("allowFallbacks is false, we should not try fallbacks")
    return false
}
```

**判定逻辑**：
- `AllowFallbacks == nil` → 视为 `true`（默认允许，保证韧性）
- `AllowFallbacks == &true` → 允许
- `AllowFallbacks == &false` → **禁止 fallback，立即返回该错误**

**为什么端到端没法用 mock 直接触发**：我的 mock provider 只能返回普通 HTTP 5xx，
Bifrost 会把它包成 `BifrostError` 且 `AllowFallbacks=nil`（默认允许）。
要造出 `AllowFallbacks=&false`，需要一个 **PreLLMHook / PostLLMHook 插件**显式返回该错误——
这属于「改代码造错误」，不在 P0 运行时验证范围内（会引入自定义插件工程，超出本次实测边界）。

**结论**：
- 代码路径已逐行确认，逻辑无误 → **源码级 PASS**。
- 端到端实测 → **未做**（需自定义插件，标 UNVERIFIED，不伪造）。

---

## 六、汇总

| Case | 场景 | 状态 | 证据强度 |
|---|---|---|---|
| A | Primary 成功，不调备用 | **PASS** | 强（双侧计数） |
| B | Primary 失败 → 备用成功 | **PASS** | 强（计数 + routing_info 自证） |
| C | 全部失败 → 返回 primary 错误 | **PASS** | 强（错误消息 + routing_info） |
| D | RequestCancelled 不 fallback | **PASS** | 强（取消 + 慢响应，B 计数=0，无替代解释） |
| E | AllowFallbacks=false 不 fallback | **源码级 PASS / 端到端 UNVERIFIED** | 源码逐行确认；运行时需插件 |

---

## 七、本轮踩到的坑（可复用）

1. **fallbacks 是字符串数组**：`["anthropic/mock-model"]`，不是对象数组。
   来源：`handlers/inference.go:367`。
2. **`/v1/chat/completions` 是 OpenAI 兼容端点，严格校验**：
   - 塞 `input`（Bifrost 原生字段）→ 400
   - 塞对象数组格式的 fallbacks → 400
   - `BifrostChatRequest` 的原生字段是 `provider/model/input/params`（`schemas/chatcompletions.go:14-21`），
     **不能直接用在 OpenAI 兼容端点**
3. **mock 必须实现 `/v1/models`**：否则 Bifrost 建 key 时会标 `list_models_failed`。
4. **验证 fallback 要看 provider 侧计数**，不能只看返回内容——否则分不清谁处理的。
5. **不要忘 reset 计数**：我在 Case D 犯了这错，导致数据不可用。
