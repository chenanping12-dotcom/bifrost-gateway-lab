# bifrost-gateway-lab

对 [Bifrost](https://github.com/maximhq/bifrost)（Go 写的 LLM 网关，v2.0.0）做**黑盒行为验证**的实验仓库。

不问「这个网关有哪些功能」，只问**「它宣称的容错语义，在真实运行时到底成不成立」**，并把每一条结论
钉在可重跑的脚本和可复核的计数上。

**一条命令复现全部结论：**

```bash
python scripts/download_bifrost.py     # 首次：下载 112MB 单文件网关（可续传）
python scripts/verify_contract.py      # 29/29  不需要网关，验 mock 自身契约
python scripts/verify_fallback.py      # 26/26  端到端跑真实 Bifrost
```

两个脚本都是**自包含**的：自己拉起 mock provider、自己拉起网关、自己清理，退出码 0 表示全过。

---

## 一、验证结论

### 跨 Provider Fallback（`scripts/verify_fallback.py` — 26/26 PASS）

判定 fallback 是否发生，看的是 **mock provider 侧的 `served` 计数**，不是响应内容。
内容只能告诉你「谁最后成功了」，**计数才能告诉你「备用到底有没有被调用过」**——这两件事经常被混为一谈。

| Case | 场景 | 结论 | 判定依据（可复核） |
|---|---|---|---|
| **A** | Primary 正常 | 备用**完全不被调用** | `B.served == 0` |
| **B** | Primary 返回 500 | 用户端 **200 无感知**，备用接管 | `A.served=1, A.failed=1`，`B.served=1`，`routing_info.is_fallback=true` |
| **C** | 两个都失败 | 返回 **500，且错误消息来自 primary** | 响应体含 `injected failure from mock-A`（不是备用那条 503） |
| **D** | 客户端中途取消 | 备用**不被调用** | `A.served=1, A.failed=0`，`B.served=0` |
| **E** | 流式 + fallback | SSE 每个 chunk 都带 `is_fallback` 与 `primary_provider` | 流式内容来自 `mock-B`，`B.served=1` |

其中 **Case D 的构造是整组实验里最讲究的一处**：让 primary 变**慢**（3000ms）而不是变**失败**，
客户端 1 秒后断开。因为 primary 是「慢」而非「成功」，备用不被调用就**只能**解释为「取消阻断了
fallback」——排除了所有替代解释。

### Mock Provider 契约（`scripts/verify_contract.py` — 29/29 PASS）

同一套断言把 `mock/` 的行为钉死，包括当年踩过的一个坑的**回归测试**：
SSE 的 `[DONE]` 帧必须是合法 chunked 帧，否则网关尾部报

```
invalid character 't' after chunk size
```

验证方式是**用裸 socket 抓字节、手工解析 chunked 帧**，而不是调现成的 SSE 库——
库会把帧解码得太好，反而发现不了帧本身是坏的。

---

## 二、诚实标注：哪些验过，哪些没验

做技术项目最容易犯的错，是把「代码写出来了」当成「结论成立了」。所以这部分单独列。

| 项目 | 状态 | 依据 |
|---|---|---|
| 跨 provider fallback（A–E） | ✅ **已实测复现** | `reports/fallback_matrix.json`，26/26 |
| 客户端取消阻断 fallback | ✅ 已实测 | Case D |
| 流式 fallback + 逐 chunk 元数据 | ✅ 已实测 | Case E |
| Provider 快速失败（宕机 → 502） | ✅ 已实测 | 见 `docs/Bifrost第一步验证记录.md` §5.5 |
| Provider 恢复（不重启网关） | ✅ 已实测 | 同上 §5.6 |
| 语义缓存短路机制 | ⚠️ **仅源码级 + 独立模块测试** | 见 `docs/semantic-cache验证报告.md` |
| 语义缓存真实插件端到端 | ❌ **未验证** | 需 embedding provider + 向量库，当时未配 |
| Key 加权路由流量分布 | ❌ 未完成 | 同一 provider 下多 key 共用 base_url，无法区分流量归属 |
| Provider 健康度 / 熔断 | ❌ 未开始 | 需改 Go 代码并重新编译 |
| UI 页面级交互 | ⚠️ 部分 | 8080 可访问、API 全通，未做浏览器自动化 |

**没验的就是没验，报告里不写成「已实现」。** 这一栏的存在本身就是筛选信号：
能清楚说出自己工作的边界，比多说一个功能更能建立信任。

---

## 三、目录结构

```
bifrost-gateway-lab/
├── README.md
├── mock/
│   └── mock_provider_v2.py       双协议 mock（OpenAI + Anthropic）+ 运行时故障注入
├── scripts/
│   ├── download_bifrost.py       可续传下载 + 字节数校验
│   ├── verify_contract.py        mock 契约自测（29 项，不需要网关）
│   └── verify_fallback.py        端到端 fallback 矩阵（26 项，真实 Bifrost）
├── docs/                         当时的原始报告（历史记录，未改动）
│   ├── Bifrost源码调用链分析.md
│   ├── Bifrost第一步验证记录.md
│   ├── fallback验证报告.md
│   ├── streaming验证报告.md
│   ├── semantic-cache验证报告.md
│   └── BIFROST_NEXT_TASKS.md
├── reports/
│   └── fallback_matrix.json      脚本跑出来的机器可读结果
└── tools/                        (gitignore) 网关二进制 + 运行期数据
```

`docs/` 是**当时写的历史记录**，保留原样；`scripts/` + `reports/` 是**今天可重跑的复现装置**。
两者并存，是因为历史报告记录的是「当时怎么想的」，而脚本证明的是「结论现在仍然成立」。

---

## 四、Mock Provider 的能力

`mock/mock_provider_v2.py` 是整个实验的地基——没有可控的上游，就测不了网关的容错。

```bash
python mock/mock_provider_v2.py --port 9001 --label A
python mock/mock_provider_v2.py --port 9002 --label B
```

| 端点 | 作用 |
|---|---|
| `POST /v1/chat/completions` | OpenAI 兼容，支持 `stream=true` |
| `POST /v1/messages` | Anthropic 兼容 |
| `GET /v1/models` | **必须实现**，见下方坑 #2 |
| `POST /control` | 运行时故障注入，**无需重启** |
| `GET /health` | 返回 `served` / `failed` 计数 |

```bash
curl -X POST http://127.0.0.1:9001/control -d '{"mode":"fail","status":500}'   # 立刻开始失败
curl -X POST http://127.0.0.1:9001/control -d '{"mode":"slow","ms":3000}'     # 变慢
curl -X POST http://127.0.0.1:9001/control -d '{"mode":"reset"}'              # 恢复 + 计数清零
curl http://127.0.0.1:9001/health                                             # served=x failed=y
```

**计数语义**（容易读错，务必看清）：

- `served` = 收到的推理请求总数（尝试数）
- `failed` = 其中被注入失败的数量

所以 `served=1, failed=1` 的意思是「请求**确实打到了**这个 provider，而且它失败了」——
这正是 Case B 里 primary 的读数。

> ⚠️ `mock_provider_v2.py` 是**依据报告反推重建**的：原始文件在已删除的临时目录里，没有纳入版本管理。
> 重建件是否忠实，由 `verify_contract.py` 的 29 条断言判定，而不是由文档自称。

---

## 五、环境坑（都是实测踩出来的）

1. **必须把 `TMPDIR`/`TEMP`/`TMP` 指到工作区内。**
   这是最坑的一个。Bifrost 启动时有个 SQLite 迁移 `make_base_pricing_columns_nullable` 要重建表、
   需要 temp store；受限环境下默认 TEMP 目录不可写，SQLite 抛出

   ```
   unable to open database file: The system cannot find the file specified.
   ```

   看上去像路径或权限问题，**实际是 temp store 不可写**。不设这个环境变量，网关会在第 40 多行
   迁移处直接退出，表现为「永远不 ready」。`verify_fallback.py` 已经替你设好了。

2. **`GET /v1/models` 不能省。**
   Bifrost 建 provider key 时会主动探测该端点。mock 没实现它时，key 状态会一直停在
   `list_models_failed`。补上之后变成 `success`——这反过来证明了探测行为的存在。

3. **SSE 的 `[DONE]` 帧也必须用 chunked 帧包裹**，否则报 `invalid character 't' after chunk size`。

4. **不要只用 HTTP 状态码判断 API 是否存在。**
   `/api/providers`、`/api/config`、`/api/nonexistent` **全都返回 200**，因为未匹配路径会
   fallback 到 SPA 首页。必须看响应体：真 API 返回 JSON，fallback 返回 HTML。

5. **`openai-compatible` 是个陷阱。**
   `POST /api/providers` 会**接受**这个名字（返回 200），但加 key 时报
   `unsupported provider: openai-compatible`。**创建成功 ≠ 可用**，做故障转移实验必须用真实
   provider 类型（`openai` / `anthropic`）。

6. **npx 的下载器不要用。** 中断会留下损坏的二进制，而它用 `existsSync` 判断是否已下载，
   下次会直接执行损坏文件。改用 `curl -C -` 或本仓库的 `download_bifrost.py`。

---

## 六、这套实验想说明什么

这份仓库的价值不在于「用了 Bifrost」，而在于**怎么把一个模糊的功能宣称，变成一条可证伪的结论**：

- **判定指标要选对**：用 mock 侧计数判定 fallback，而不是看响应内容——内容会掩盖「备用其实也被打过」。
- **实验设计要排除替代解释**：Case D 用「慢」而非「失败」构造，才让 `B.served == 0` 有唯一解释。
- **结论要能重跑**：所有数字都能被 `verify_fallback.py` 重新产出，而不是只存在于一篇文档里。
- **边界要写清楚**：没验的项（语义缓存真实插件、加权路由、熔断）明确标为未验证。

---

## 环境

- Windows（无 Docker / WSL2）
- Python 3.13（`mock/` 与 `scripts/` 均只用标准库，无第三方依赖）
- Bifrost v2.0.0 windows/amd64，117,577,216 字节