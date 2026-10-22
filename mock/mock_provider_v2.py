#!/usr/bin/env python3
"""双协议 Mock LLM Provider（OpenAI + Anthropic），支持运行时故障注入。

用途
----
作为 Bifrost 网关的**上游 provider 替身**，让「跨 provider fallback / 流式透传 /
故障快速失败」这些机制可以在**零 token 成本**下被真实触发和计数验证。
判断 fallback 是否发生，看的是本 mock 侧的 served/failed 计数，而不是响应内容——
否则无法区分「谁处理了这个请求」。

⚠️ 关于来源（重要，不要误读）
------------------------
本文件是**依据验证报告反推重建**的，不是当年那次实验的原始副本
（原始 `mock_provider_v2.py` 位于已删除的临时目录，未纳入版本管理）。
重建时以以下已记录的事实为准绳：

- `fallback验证报告.md`   Case A–D 的计数与响应体格式
- `streaming验证报告.md`   SSE chunk 格式与 `[DONE]` 帧的 chunked 包裹要求
- `BIFROST_NEXT_TASKS.md` §六 mock v2 的能力清单与 /control 契约
- `Bifrost第一步验证记录.md` §四 provider 接入方式

因此**行号与原始文件不一致**（报告里引用的 `mock_provider_v2.py:175` 指的是已丢失的原件）。
本文件是否忠实复现了当时的契约，由 `scripts/verify_contract.py` 实测判定——
该脚本把报告里写下的每一条行为当作断言跑一遍。

已实现的契约
------------
======================  ==================================================
端点                     行为
======================  ==================================================
GET  /health            返回 mode / served / failed / inflight
POST /control           运行时切换模式，无需重启进程
GET  /v1/models         OpenAI 风格模型列表；**必须实现**，否则 Bifrost 建 key 时
                        会把 key 标为 `list_models_failed`
POST /v1/chat/completions  OpenAI 兼容；支持 stream=true
POST /v1/messages       Anthropic 兼容；支持 stream=true
======================  ==================================================

/control 支持的指令
-------------------
    {"mode": "ok"}                    恢复正常
    {"mode": "fail", "status": 500}   立刻以指定状态码失败
    {"mode": "slow", "ms": 3000}      延迟指定毫秒后正常响应
    {"mode": "reset"}                 恢复 ok 且**计数清零**

计数器语义（务必理解，否则会误判 fallback）
------------------------------------------
`served`  = 收到的推理请求总数（尝试数）
`failed`  = 其中被注入失败的数量
即 `served=1, failed=1` 表示「请求确实打到了本 provider，且它失败了」。
这正是 fallback验证报告 Case B 里 primary 的 `1 / 1` 的含义。
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------
# 共享状态：所有请求线程共用一个实例
# --------------------------------------------------------------------------


class MockState:
    """进程内共享的可变状态（模式 + 计数器 + 请求序号）。"""

    def __init__(self, label: str) -> None:
        self.label = label
        self.lock = threading.Lock()
        self.mode = "ok"          # ok | fail | slow
        self.fail_status = 500    # mode=fail 时返回的状态码
        self.slow_ms = 3000       # mode=slow 时的延迟
        self.served = 0           # 收到的推理请求数
        self.failed = 0           # 其中被注入失败的数量
        self.seq = 0              # 请求序号，用于 req#N
        self.inflight = 0

    # -- 计数 ---------------------------------------------------------------

    def begin(self) -> int:
        """登记一次请求，返回本次的序号（从 1 开始）。"""
        with self.lock:
            self.served += 1
            self.seq += 1
            self.inflight += 1
            return self.seq

    def end(self, *, failed: bool) -> None:
        with self.lock:
            self.inflight -= 1
            if failed:
                self.failed += 1

    # -- 控制 ---------------------------------------------------------------

    def apply_control(self, body: dict) -> dict:
        """应用一条 /control 指令，返回应用后的状态快照。"""
        mode = body.get("mode")
        with self.lock:
            if mode == "reset":
                self.mode = "ok"
                self.served = 0
                self.failed = 0
                self.seq = 0
            elif mode == "ok":
                self.mode = "ok"
            elif mode == "fail":
                self.mode = "fail"
                # 报告里出现过 fail(500) 与 fail(503) 两种
                self.fail_status = int(body.get("status", 500))
            elif mode == "slow":
                self.mode = "slow"
                # 任务总表写的是 ms，早期脚本写过 delay_ms，两个都收
                self.slow_ms = int(body.get("ms", body.get("delay_ms", 3000)))
            elif mode is not None:
                return {"error": f"unknown mode: {mode}"}
            return self.snapshot_locked()

    def snapshot_locked(self) -> dict:
        return {
            "label": self.label,
            "mode": self.mode,
            "fail_status": self.fail_status,
            "slow_ms": self.slow_ms,
            "served": self.served,
            "failed": self.failed,
            "inflight": self.inflight,
        }

    def snapshot(self) -> dict:
        with self.lock:
            return self.snapshot_locked()


# --------------------------------------------------------------------------
# HTTP 处理
# --------------------------------------------------------------------------


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"   # 必须 1.1，否则 SSE 无法用 chunked 长连接
    server_version = "mock-provider-v2"

    state: MockState  # 由 serve() 注入到子类上

    # -- 基础设施 -----------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        if self.server.verbose:  # type: ignore[attr-defined]
            super().log_message(fmt, *args)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- chunked 长连接的底层写入 ------------------------------------------
    #
    # 这是当年踩过的坑：手写 chunked 时，**每一帧**（包括最后的 [DONE]）
    # 都必须带 `%x\r\n <payload> \r\n` 的帧头，否则 Bifrost 会报
    #   invalid character 't' after chunk size
    # 因为 `[DONE]` 的 't' 被当成了 chunk size 的一部分。

    def _write_chunk(self, payload: bytes) -> None:
        self.wfile.write(b"%x\r\n" % len(payload))
        self.wfile.write(payload)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _end_chunks(self) -> None:
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    # -- 路由 ---------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/health":
            snap = self.state.snapshot()
            snap["status"] = "ok"
            self._send_json(200, snap)
        elif path == "/control":
            self._send_json(200, self.state.snapshot())
        elif path == "/v1/models":
            # 必须存在：Bifrost 建 provider key 时会主动探测该端点，
            # 缺失会让 key 状态停在 list_models_failed。
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "mock-model",
                            "object": "model",
                            "created": 0,
                            "owned_by": "mock",
                        }
                    ],
                },
            )
        else:
            self._send_json(404, {"error": {"message": f"no such path: {path}"}})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/control":
            result = self.state.apply_control(self._read_json())
            status = 400 if "error" in result else 200
            self._send_json(status, result)
            return
        if path == "/v1/chat/completions":
            self._handle_inference(anthropic=False)
            return
        if path == "/v1/messages":
            self._handle_inference(anthropic=True)
            return
        self._send_json(404, {"error": {"message": f"no such path: {path}"}})

    # -- 推理 ---------------------------------------------------------------

    def _handle_inference(self, *, anthropic: bool) -> None:
        body = self._read_json()
        seq = self.state.begin()

        # 模式判定要在计数登记之后，这样 fail 的请求也会体现在 served 里
        mode, status, slow_ms = self.state.mode, self.state.fail_status, self.state.slow_ms

        if mode == "fail":
            self.state.end(failed=True)
            self._send_json(
                status,
                {
                    "error": {
                        "message": f"injected failure from mock-{self.state.label}",
                        "type": "injected_failure",
                    }
                },
            )
            return

        if mode == "slow" and slow_ms > 0:
            time.sleep(slow_ms / 1000.0)

        text = self._extract_prompt(body)
        reply = f"[mock-{self.state.label}] req#{seq}: {text}"
        stream = bool(body.get("stream"))

        try:
            if anthropic:
                self._reply_anthropic(seq, reply, stream)
            else:
                self._reply_openai(seq, reply, stream)
        except (BrokenPipeError, ConnectionResetError):
            # 客户端中途断开（fallback验证报告 Case D 就是靠这个场景）
            self.state.end(failed=False)
            return
        self.state.end(failed=False)

    @staticmethod
    def _extract_prompt(body: dict) -> str:
        """从两种协议里尽量取出用户输入，用于回显。"""
        messages = body.get("messages") or []
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                content = msg.get("content")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    parts = [
                        c.get("text", "")
                        for c in content
                        if isinstance(c, dict) and c.get("type") == "text"
                    ]
                    if parts:
                        return " ".join(parts)
        # Anthropic 原生 / Bifrost 原生字段
        return str(body.get("input") or body.get("prompt") or "")

    # -- OpenAI 协议 --------------------------------------------------------

    def _reply_openai(self, seq: int, reply: str, stream: bool) -> None:
        model = "mock-model"
        if not stream:
            self._send_json(
                200,
                {
                    "id": f"chatcmpl-mock-{self.state.label}-{seq}",
                    "object": "chat.completion",
                    "created": 0,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": reply},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 4,
                        "completion_tokens": len(reply.split()),
                        "total_tokens": 4 + len(reply.split()),
                    },
                },
            )
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        for idx, token in enumerate(reply.split(" ")):
            if idx:
                token = " " + token
            payload = {
                "id": f"chatcmpl-mock-{self.state.label}-{seq}",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": model,
                "choices": [{"index": 0, "delta": {"content": token}}],
                "chunk_index": idx,
            }
            self._write_chunk(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode())

        # 末帧：必须同样用 chunked 帧包裹，否则网关报 invalid character 't' after chunk size
        self._write_chunk(b"data: [DONE]\n\n")
        self._end_chunks()

    # -- Anthropic 协议 -----------------------------------------------------

    def _reply_anthropic(self, seq: int, reply: str, stream: bool) -> None:
        if not stream:
            self._send_json(
                200,
                {
                    "id": f"msg_mock-{self.state.label}",
                    "type": "message",
                    "role": "assistant",
                    "model": "mock-model",
                    "content": [{"type": "text", "text": reply}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 4, "output_tokens": len(reply.split())},
                },
            )
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        for idx, token in enumerate(reply.split(" ")):
            if idx:
                token = " " + token
            payload = {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": token},
                "chunk_index": idx,
            }
            self._write_chunk(
                f"event: content_block_delta\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()
            )

        self._write_chunk(b"event: message_stop\ndata: [DONE]\n\n")
        self._end_chunks()


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------


class QuietHTTPServer(ThreadingHTTPServer):
    """客户端主动断连是**预期行为**（Case D 就是靠它证明取消会阻断 fallback），
    不该在控制台刷一屏 traceback 把真正的验证输出淹掉。"""

    daemon_threads = True

    def handle_error(self, request, client_address) -> None:  # noqa: ANN001
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def build_server(port: int, label: str, host: str = "127.0.0.1", verbose: bool = False):
    """构造（但不启动）一个 mock server，便于测试里拿随机端口。"""
    state = MockState(label)

    handler = type(
        f"MockHandler_{label}",
        (MockHandler,),
        {"state": state},
    )

    httpd = QuietHTTPServer((host, port), handler)
    httpd.verbose = verbose  # type: ignore[attr-defined]
    httpd.state = state      # type: ignore[attr-defined]
    return httpd


def serve(port: int, label: str, host: str = "127.0.0.1", verbose: bool = False) -> None:
    httpd = build_server(port, label, host, verbose)
    print(
        f"[mock-{label}] listening on http://{host}:{port}  "
        f"(label={label}, 模式=ok)\n"
        f"           POST /control  {{\"mode\":\"fail\",\"status\":500}} | "
        f"{{\"mode\":\"slow\",\"ms\":3000}} | {{\"mode\":\"reset\"}}\n"
        f"           GET  /health   查看 served/failed 计数",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main() -> None:
    ap = argparse.ArgumentParser(description="双协议 Mock LLM Provider（含故障注入）")
    ap.add_argument("--port", type=int, default=9001)
    ap.add_argument("--label", default="A", help="实例标签，出现在响应体与计数里，如 A / B")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--verbose", action="store_true", help="打印每个请求的访问日志")
    args = ap.parse_args()
    serve(args.port, args.label, args.host, args.verbose)


if __name__ == "__main__":
    main()