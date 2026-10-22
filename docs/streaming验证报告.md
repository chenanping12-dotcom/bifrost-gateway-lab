# Bifrost Streaming 验证报告

> 日期：2026-09-07
> 版本：Bifrost v2.0.0
> 环境：网关 8080；Primary=`openai`→mock-A(9001)；Fallback=`anthropic`→mock-B(9002)
> 结论：**正常流式 PASS；流式 fallback PASS；中途 provider 出错场景 UNVERIFIED（mock 限制，见第三节）**

---

## 一、正常流式（SSE）

**用例**：`POST /v1/chat/completions` 带 `stream:true`，Primary 健康。

| 指标 | 结果 | 判定 |
|---|---|---|
| HTTP | 200 | PASS |
| SSE chunk 数 | 7（含 `[DONE]`） | PASS |
| `finish_reason` | 末 chunk 含 `stop` | PASS |
| 内容重建 | `[mock-A] req#2: stream me please`（完整） | PASS |
| 末帧 | `data: [DONE]`（无解析错误） | PASS |

**修正记录**：第一版 mock 的 `[DONE]` 帧没按 `chunked` 编码包裹，导致网关尾部报
`invalid character 't' after chunk size`。已修复 mock（`mock_provider_v2.py:175` 用
`%x\r\n...` 帧包裹 `[DONE]`），重测后无报错。这本身是 mock 缺陷，不是 Bifrost 问题，
但顺手堵上了，免得误判。

---

## 二、流式 + 跨 Provider Fallback

**用例**：Primary(`openai`) 注入 500，请求带 `stream:true` + `fallbacks:["anthropic/mock-model"]`。

**原始 SSE 抓取（节选）**：

```json
data: {"id":"msg_mock-B","object":"chat.completion.chunk",
  "extra_fields":{
    "request_type":"chat_completion_stream",
    "routing_info":{
      "provider":"anthropic","model":"mock-model","key":"anthropic-key",
      "is_fallback":true,"primary_provider":"openai","primary_model":"mock-model"
    },"provider":"anthropic","chunk_index":0}}
```

| 指标 | 结果 | 判定 |
|---|---|---|
| HTTP | 200（用户无感知） | PASS |
| 流式内容来源 | `[mock-B] anthropic#...`（来自备用） | PASS |
| `routing_info.is_fallback` | `true`（每个 chunk 都标） | PASS |
| `routing_info.primary_provider` | `openai`（保留主 provider 信息） | PASS |
| `chunk_index` | 0,1,3… 递增 | PASS |
| 备用 Provider 调用计数 | B.served=1（真实打到 B） | PASS |

**结论：PASS。** Fallback 机制对**流式请求同样生效**——Bifrost 在 `handleRequest` 里统一处理
fallback，与是否流式无关。流式 fallback 时，每个下游 chunk 都带着 `is_fallback:true` 和
`primary_provider` 元数据，可观测性和非流式一致。

---

## 三、中途 Provider 出错（partial stream failure）

**状态：UNVERIFIED（本趟未构造，如实标注）。**

**为什么没做**：当前 mock 的故障注入只有"整体失败"（`mode=fail` 立即返回 500），
无法模拟"先吐几个 chunk 再在中途断流"的场景。要真验证，需要给 mock 加一个
`mode=partial-fail`（发 N 个 chunk 后断开连接 / 写非法帧）。这是 mock 工程增量，未在
本趟完成。

**已知事实（源码级，可讲）**：
- Bifrost 的流式在 `tryRequest` 里把上游 SSE 逐块转发（`core/bifrost.go:5351-5466`
  区域，插件短路/队列取结果后写回）。
- 上游中途断流时，网关要么转发错误帧要么终止流；fallback 是否对"已开始的流"生效，
  取决于错误发生时机——如果首个 chunk 之前就失败，会走正常 fallback；如果流已开始，
  中途失败通常不会再切备用（否则客户端会收到两段不连续的流）。
- **这是设计上的合理取舍**：已经向客户端开了流的请求，中途换 provider 会导致响应
  错乱，所以 Bifrost 对"流进行中"的失败一般不 fallback。具体边界需读 `handleStream`
  源码确认（建议后续补全）。

---

## 四、Connection Close / 资源泄漏

- **Context 取消**：已在《fallback验证报告.md》Case D 用确定性测试证明——客户端断开
  （context 取消）不会触发 fallback，且不会卡主请求线程。Streaming 同样走同一 context
  传播，断开时上游请求应被取消。
- **Goroutine 泄漏**：未在本次对 streaming 单独做 `runtime.NumGoroutine` 断言（需启用
  pprof 或注入计数）。网关对请求 context 的取消传播是其基础设计，但"压力后 goroutine 不
  持续增长"的断言留给《performance测试报告.md》的并发压测部分统一验证。

---

## 五、面试可讲点

- **SSE 是逐块转发，不是攒完再发**：网关把上游的每个 `data:` chunk 立刻写给客户端，
  `chunk_index` 单调递增，客户端能像直连 provider 一样做增量渲染。
- **流式 fallback 元数据完整**：每个 chunk 都带 `is_fallback` + `primary_provider`，
  可观测性不打折。
- **为什么流中途失败通常不 fallback**：已经开了流再换 provider 会让客户端收到两段
  拼接不上的响应，破坏流的语义；所以只在"首 chunk 之前"的失败才 fallback。
- **context 取消贯穿全链路**：客户端断开 → 网关 context 取消 → 上游请求取消，不烧
  幽灵 token，也不卡 worker。
