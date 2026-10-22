#!/usr/bin/env python3
"""Mock Provider 契约自测——不需要 Bifrost，纯本地可跑。

为什么需要这个脚本
------------------
`mock_provider_v2.py` 是依据报告反推重建的，行号与原件不一致。
「重建件是否忠实」不能靠嘴说，只能靠**把报告里写下的每一条行为变成断言**。
本脚本就是那组断言。

其中最值得一提的一条：SSE 的 `[DONE]` 帧必须是**合法 chunked 帧**。
当年第一版 mock 漏了这一层包裹，网关尾部直接报

    invalid character 't' after chunk size

所以本脚本不调用任何现成的 SSE 库，而是**用裸 socket 抓字节、手工解析 chunked 帧**，
并断言最后一帧也是规规矩矩包裹过的——这是那条 bug 的回归测试。

用法
----
    python scripts/verify_contract.py

退出码 0 = 全部通过；非 0 = 有断言失败。
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mock"))

from mock_provider_v2 import build_server  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -- {detail}" if detail else ""))
    return ok


# --------------------------------------------------------------------------
# 裸 HTTP / chunked 解析
# --------------------------------------------------------------------------


def raw_request(port: int, path: str, payload: dict | None = None) -> bytes:
    """发一个请求并返回**原始字节**（不解析 chunked），用于检验线上格式。"""
    body = b"" if payload is None else json.dumps(payload).encode()
    head = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode()
    sock = socket.create_connection(("127.0.0.1", port), timeout=15)
    try:
        sock.sendall(head + body)
        buf = b""
        while True:
            piece = sock.recv(65536)
            if not piece:
                break
            buf += piece
    finally:
        sock.close()
    return buf


def split_response(raw: bytes) -> tuple[int, dict[str, str], bytes]:
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ")[1])
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return status, headers, body


def parse_chunked(body: bytes) -> list[bytes]:
    """手工解析 chunked 帧；帧格式不合法会抛异常——这正是我们要验的。"""
    frames: list[bytes] = []
    pos = 0
    while True:
        nl = body.find(b"\r\n", pos)
        if nl < 0:
            raise ValueError("chunk size 行缺少 CRLF，帧结构损坏")
        size_line = body[pos:nl]
        try:
            size = int(size_line, 16)
        except ValueError as exc:
            raise ValueError(
                f"非法 chunk size {size_line!r}（当年 {{{{invalid character 't' after chunk size}}}} 就是这个）"
            ) from exc
        pos = nl + 2
        if size == 0:
            break
        frames.append(body[pos : pos + size])
        pos += size
        if body[pos : pos + 2] != b"\r\n":
            raise ValueError("chunk 数据后缺少 CRLF")
        pos += 2
    return frames


def get_json(port: int, path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as resp:
        return json.loads(resp.read().decode())


def post_json(port: int, path: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def main() -> int:
    print("=" * 72)
    print("Mock Provider 契约自测（对照 fallback / streaming / 任务总表 三份报告）")
    print("=" * 72)

    server = build_server(0, "A", host="127.0.0.1")
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"\n临时 mock 实例: 127.0.0.1:{port}  label=A\n")

    try:
        # --- 1. 健康检查与初始计数 ---------------------------------------
        print("[1] 健康检查 / 初始计数")
        health = get_json(port, "/health")
        check("GET /health 返回 200 且 status=ok", health.get("status") == "ok")
        check(
            "初始计数 served=0 failed=0",
            health.get("served") == 0 and health.get("failed") == 0,
            f"served={health.get('served')} failed={health.get('failed')}",
        )

        # --- 2. /v1/models（缺它 Bifrost 会把 key 标成 list_models_failed）
        print("\n[2] /v1/models —— Bifrost 建 key 时会主动探测")
        models = get_json(port, "/v1/models")
        ids = [m.get("id") for m in models.get("data", [])]
        check("GET /v1/models 返回 mock-model", "mock-model" in ids, f"ids={ids}")

        # --- 3. 非流式成功 + 计数 ----------------------------------------
        print("\n[3] 非流式调用与计数语义")
        status, body = post_json(
            port,
            "/v1/chat/completions",
            {"model": "mock-model", "messages": [{"role": "user", "content": "case A"}]},
        )
        content = body.get("choices", [{}])[0].get("message", {}).get("content", "")
        check("非流式返回 200", status == 200, f"status={status}")
        check(
            "响应体格式为 [mock-A] req#1: <内容>",
            content == "[mock-A] req#1: case A",
            f"content={content!r}",
        )
        check(
            "响应 id 含实例标签与序号",
            body.get("id") == "chatcmpl-mock-A-1",
            f"id={body.get('id')!r}",
        )
        health = get_json(port, "/health")
        check(
            "成功请求后 served=1 failed=0",
            health.get("served") == 1 and health.get("failed") == 0,
            f"served={health.get('served')} failed={health.get('failed')}",
        )

        # --- 4. 故障注入 fail ---------------------------------------------
        print("\n[4] 运行时故障注入：fail(500)")
        ctl = post_json(port, "/control", {"mode": "fail", "status": 500})[1]
        check("POST /control 切换 mode=fail", ctl.get("mode") == "fail", f"mode={ctl.get('mode')}")
        status, body = post_json(
            port,
            "/v1/chat/completions",
            {"model": "mock-model", "messages": [{"role": "user", "content": "case B primary"}]},
        )
        msg = body.get("error", {}).get("message", "")
        check("fail 模式返回 500", status == 500, f"status={status}")
        check(
            "错误消息为 injected failure from mock-A",
            msg == "injected failure from mock-A",
            f"message={msg!r}",
        )
        health = get_json(port, "/health")
        check(
            "失败请求记入 served 且计入 failed（=报告 Case B 的 1/1 语义）",
            health.get("served") == 2 and health.get("failed") == 1,
            f"served={health.get('served')} failed={health.get('failed')}",
        )

        # --- 5. 指定其它状态码（报告里出现过 503）-------------------------
        print("\n[5] 故障注入：fail(503)")
        post_json(port, "/control", {"mode": "fail", "status": 503})
        status, _ = post_json(
            port, "/v1/chat/completions", {"messages": [{"role": "user", "content": "x"}]}
        )
        check("fail(503) 生效", status == 503, f"status={status}")

        # --- 6. reset 清计数 ----------------------------------------------
        print("\n[6] reset：恢复且计数清零（报告踩坑#5：忘 reset 会导致数据不可用）")
        ctl = post_json(port, "/control", {"mode": "reset"})[1]
        check(
            "reset 后 mode=ok 且计数归零",
            ctl.get("mode") == "ok" and ctl.get("served") == 0 and ctl.get("failed") == 0,
            f"mode={ctl.get('mode')} served={ctl.get('served')} failed={ctl.get('failed')}",
        )
        status, body = post_json(
            port,
            "/v1/chat/completions",
            {"model": "mock-model", "messages": [{"role": "user", "content": "after recovery"}]},
        )
        content = body.get("choices", [{}])[0].get("message", {}).get("content", "")
        check(
            "恢复后立刻成功（无需重启 mock）",
            status == 200 and content.endswith("after recovery"),
            f"status={status} content={content!r}",
        )
        check("序号在 reset 后重新从 1 开始", content.startswith("[mock-A] req#1:"), f"{content!r}")

        # --- 7. 未知指令 ---------------------------------------------------
        print("\n[7] 非法 /control 指令")
        status, _ = post_json(port, "/control", {"mode": "explode"})
        check("未知 mode 返回 400", status == 400, f"status={status}")

        # --- 8. 流式：chunked 帧 + [DONE] 回归测试 -------------------------
        print("\n[8] 流式 SSE：chunked 帧结构与 [DONE] 包裹（当年那个 bug 的回归测试）")
        post_json(port, "/control", {"mode": "reset"})
        raw = raw_request(
            port,
            "/v1/chat/completions",
            {
                "model": "mock-model",
                "stream": True,
                "messages": [{"role": "user", "content": "stream me please"}],
            },
        )
        status, headers, body = split_response(raw)
        check("流式返回 200", status == 200, f"status={status}")
        check(
            "Content-Type 为 text/event-stream",
            headers.get("content-type") == "text/event-stream",
            f"ct={headers.get('content-type')!r}",
        )
        check(
            "使用 Transfer-Encoding: chunked",
            headers.get("transfer-encoding") == "chunked",
            f"te={headers.get('transfer-encoding')!r}",
        )

        frames: list[bytes] = []
        framing_ok = True
        framing_err = ""
        try:
            frames = parse_chunked(body)
        except ValueError as exc:
            framing_ok = False
            framing_err = str(exc)
        check("所有 chunked 帧结构合法（无 invalid character 错误）", framing_ok, framing_err)

        if framing_ok:
            payloads = [f for f in frames if f.startswith(b"data: ")]
            check("末帧为 data: [DONE] 且已被 chunked 正确包裹", payloads[-1] == b"data: [DONE]\n\n",
                  f"last={payloads[-1]!r}")
            json_frames = []
            for p in payloads[:-1]:
                try:
                    json_frames.append(json.loads(p[len(b"data: "):].decode()))
                except json.JSONDecodeError:
                    pass
            idxs = [f.get("chunk_index") for f in json_frames]
            check(
                "chunk_index 严格递增（0,1,2...）",
                idxs == list(range(len(idxs))) and len(idxs) > 0,
                f"chunk_index={idxs}",
            )
            rebuilt = "".join(
                f.get("choices", [{}])[0].get("delta", {}).get("content", "") for f in json_frames
            )
            check(
                "客户端可无损重建完整内容",
                rebuilt == "[mock-A] req#1: stream me please",
                f"rebuilt={rebuilt!r}",
            )

        # --- 9. slow 模式 ---------------------------------------------------
        print("\n[9] slow 模式（fallback验证报告 Case D 用 3000ms 制造取消窗口）")
        post_json(port, "/control", {"mode": "slow", "ms": 700})
        t0 = time.perf_counter()
        status, _ = post_json(
            port, "/v1/chat/completions", {"messages": [{"role": "user", "content": "slow"}]}
        )
        elapsed = (time.perf_counter() - t0) * 1000
        check(
            "slow 模式确实延迟（>=600ms）且最终成功",
            status == 200 and elapsed >= 600,
            f"elapsed={elapsed:.0f}ms status={status}",
        )

        # --- 10. Anthropic 协议 --------------------------------------------
        print("\n[10] Anthropic 协议端点")
        post_json(port, "/control", {"mode": "reset"})
        status, body = post_json(
            port,
            "/v1/messages",
            {"model": "mock-model", "messages": [{"role": "user", "content": "anthropic hi"}]},
        )
        text = ""
        blocks = body.get("content") or []
        if blocks and isinstance(blocks, list):
            text = blocks[0].get("text", "")
        check("Anthropic 非流式返回 200", status == 200, f"status={status}")
        check(
            "Anthropic 响应体为 [mock-A] req#1: <内容>",
            text == "[mock-A] req#1: anthropic hi",
            f"text={text!r}",
        )
        check(
            "Anthropic id 形如 msg_mock-A",
            str(body.get("id", "")).startswith("msg_mock-"),
            f"id={body.get('id')!r}",
        )

        # --- 11. 计数隔离：两个实例互不影响 ---------------------------------
        print("\n[11] 双实例计数隔离（fallback 判定的前提）")
        server_b = build_server(0, "B", host="127.0.0.1")
        port_b = server_b.server_address[1]
        threading.Thread(target=server_b.serve_forever, daemon=True).start()
        try:
            post_json(port_b, "/control", {"mode": "reset"})
            post_json(port, "/control", {"mode": "reset"})
            post_json(
                port, "/v1/chat/completions", {"messages": [{"role": "user", "content": "to A"}]}
            )
            ha, hb = get_json(port, "/health"), get_json(port_b, "/health")
            check(
                "请求打到 A 时 B 的 served 保持 0（正是 Case A 的判定依据）",
                ha.get("served") == 1 and hb.get("served") == 0,
                f"A.served={ha.get('served')} B.served={hb.get('served')}",
            )
            check("两实例标签不同", ha.get("label") == "A" and hb.get("label") == "B")
        finally:
            server_b.shutdown()
            server_b.server_close()

    finally:
        server.shutdown()
        server.server_close()

    # ---------------------------------------------------------------- 汇总
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [r for r in RESULTS if not r[1]]
    print("\n" + "=" * 72)
    print(f"汇总: {passed}/{len(RESULTS)} 通过")
    if failed:
        print("\n失败项:")
        for name, _, detail in failed:
            print(f"  - {name}  {detail}")
    print("=" * 72)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())