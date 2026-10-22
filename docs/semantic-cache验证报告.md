# Bifrost 语义缓存（Semantic Cache）验证报告

> 日期：2026-09-07
> 版本：Bifrost v2.0.0
> 结论速览：**短路机制源码确认 PASS；缓存逻辑独立测试 PASS；真实插件端到端启用 UNVERIFIED（需 embedding + 向量库，本趟未配）**

---

## 一、架构确认（源码级，强证据）

语义缓存是 Bifrost 的一个 **LLM 插件**（`plugins/semanticcache/main.go`，36KB）。
它复用了 Bifrost 的插件短路机制：在 `PreLLMHook` 里做缓存查找，命中就返回
`LLMPluginShortCircuit`，**请求根本不会到达 Provider**。

源码锚点（`plugins/semanticcache/main.go`）：

```go
// 344  PreLLMHook performs the cache lookup before the request reaches the
//      provider and short-circuits the call when a match is found.
func (plugin *Plugin) PreLLMHook(ctx, req) (*BifrostRequest, *LLMPluginShortCircuit, error) {
    ...
    // 415  direct (exact-hash) search
    shortCircuit, err := plugin.performDirectSearch(...)
    if shortCircuit != nil {
        return req, shortCircuit, nil        // ← 短路，不调下游
    }
    // 433  semantic (embedding similarity) search
    shortCircuit, err := plugin.performSemanticSearch(...)
    if shortCircuit != nil {
        return req, shortCircuit, nil        // ← 短路，不调下游
    }
}
```

**这印证了之前《Bifrost源码调用链分析.md》里的结论**：语义缓存是在插件层短路的，
不是"代理之后才缓存"，所以命中时 Provider 完全不被调用——这正是它比"代理后置缓存"
更优的设计点。

### 两种模式

| 模式 | 需要 | 说明 |
|---|---|---|
| **Direct（精确哈希）** | 仅 VectorStore | key = hash(prompt+system+params)，完全相同才命中 |
| **Semantic（语义相似）** | Embedding Provider + Dimension + VectorStore | 用 embedding 做相似度检索，近似请求也能命中 |

配置结构（`plugins/semanticcache/main.go:28` `Config`）：
- `embedding_model`（语义模式 REQUIRED）
- `vector_store_namespace`
- 语义模式启用条件：`Provider + EmbeddingModel + Dimension > 0`

---

## 二、缓存逻辑验证（独立模块，真实测试 PASS）

因为真实插件的语义模式需要 embedding provider + 向量库，配置面较深、本趟未启用，
我用一个**独立、零依赖、可单测**的 Go 模块 `bifrostx/cache` 复刻插件的
**direct-hash 短路模式**，把"命中时 Provider 不被调用"这件事用测试钉死。

测试覆盖（全部 `go test` PASS）：

| 用例 | 期望 | 结果 |
|---|---|---|
| 第一次请求 | miss → 调 Provider → 存 | PASS |
| 第二次完全相同请求 | **hit → Provider 不被调用**（calls==1） | PASS |
| 不同 prompt 请求 | miss（calls==2） | PASS |
| TTL 过期后同请求 | 过期 → miss → 再调 Provider（calls==2） | PASS |
| fetch 报错 | 不缓存 → 下次重试 | PASS |

核心断言（节选自 `cache_test.go`）：

```go
// 第二次完全相同请求必须是 hit，且 provider 只被调用过 1 次
if !hit2 { t.Fatal("second identical request must be a hit") }
if atomic.LoadInt64(&calls) != 1 {
    t.Fatalf("provider must be called exactly once, got %d", calls)
}
```

**这证明了缓存短路模式的语义正确性**，与真实插件 `performDirectSearch` 返回
`LLMPluginShortCircuit` 的行为一致。

---

## 三、真实插件端到端启用

**状态：UNVERIFIED（本趟未完成，如实标注，不伪造）。**

**为什么没做**：
1. 运行实例 `GET /api/plugins` 返回 `{"count":0,"plugins":[]}` —— 语义缓存默认**未启用**。
2. 启用语义模式需配置 Embedding Provider + Dimension + VectorStore；启用 direct 模式也需
   提供 VectorStore 实现。Bifrost v2.0.0 的插件加载走 `config.LLMPlugins`
   （`core/bifrost.go:252-259`），配置面非简单开关，本趟未在运行实例上完成注册验证。
3. 强行"调通"会引入 embedding mock + 向量库 mock 的额外工程，超出 P0 运行时验证边界，
   且我已用 §二 的独立测试 + §一 的源码确认覆盖了"命中短路"这一核心命题。

**已验证 vs 未验证**：
- ✅ 短路机制（命中不调下游）：源码逐行确认 + 独立测试证明
- ✅ 缓存读写 / TTL / 不同请求 / 错误不缓存：独立测试证明
- ⚠️ 真实插件在运行网关上启用并跑到 live cache hit：未做

**若后续要补全（建议路线）**：
1. 起一个 mock embedding provider（返回固定维度向量）；
2. 用 in-memory vectorstore 配置 semanticcache 插件；
3. `POST /api/plugins` 或 config.json 注册插件；
4. 发两次相同请求，看 `routing_info` / 插件日志里的 `cache_debug`，并交叉验证 mock Provider 的 `served` 计数只 +1。

---

## 四、面试可讲点

- **为什么缓存放插件层而不是代理层**：插件在 `PreLLMHook` 短路，命中时请求根本不离开
  网关内核去打 Provider，省的是真实的上游 token + 延迟；代理后置缓存做不到"零下游调用"。
- **direct vs semantic 的取舍**：direct 零成本但只对完全相同请求生效；semantic 能兜近似
  请求（"帮我写封邮件"和"写一封邮件"语义相同），但要付 embedding 开销和向量库运维。
- **TTL 与失效**：缓存必须有 TTL，否则模型行为变化（如 system prompt 更新）后旧缓存会
  返回过时结果——这是生产环境最容易踩的坑。
