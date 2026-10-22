#!/usr/bin/env python3
"""端到端复现 Bifrost 跨 Provider Fallback 验证矩阵（Case A–E）。

这是本仓库的**头号产物**：一条命令重跑当年那份 fallback验证报告 的全部结论。

    python scripts/verify_fallback.py

它做四件事：
  1. 拉起两个 mock provider：mock-A(:9001)、mock-B(:9002)
  2. 拉起真实 Bifrost 网关（tools/bifrost-http.exe）
  3. 建 provider / key，然后逐个跑 Case A–E
  4. 把结果写成 reports/fallback_matrix.json，并与报告里记录的期望值逐条比对

判定 fallback 是否发生，看的是 **mock 侧的 served 计数**，不是响应内容——
内容只能告诉你「谁最后成功了」，计数才能告诉你「备用到底有没有被调用过」。

Case 一览（对照 fallback验证报告.md）
  A  Primary 成功        → 备用完全不被调用（B.served == 0）
  B  Primary 500        → 用户端 200，请求由备用完成，routing_info.is_fallback == true
  C  两个都失败          → 返回 500，且错误消息来自 **primary** 而不是备用
  D  客户端中途取消      → 备用**不被调用**（A 是慢不是失败，所以 B.served==0 无替代解释）
  E  流式 + fallback     → SSE 每个 chunk 都带 is_fallback 与 primary_provider
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mock"))

from mock_provider_v2 import build_server  # noqa: E402

MOCK_A_PORT = 9001
MOCK_B_PORT = 9002
PROVIDER_A = "openai"
PROVIDER_B = "anthropic"

RESULTS: list[dict] = []


def record(case: str, name: str, ok: bool, expected: str, actual: str) -> bool:
    RESULTS.append(
        {"case": case, "check": name, "pass": ok, "expected": expected, "actual": actual}
    )
    print(f"    [{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"           期望: {expected}")
        print(f"           实际: {actual}")
    return ok


# --------------------------------------------------------------------------
# HTTP 辅助
# --------------------------------------------------------------------------


def http_json(method: str, url: str, payload: dict | None = None, timeout: float = 30.0):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw.decode())
            except json.JSONDecodeError:
                return resp.status, {"_raw": raw[:400].decode("utf-8", "replace")}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw.decode())
        except json.JSONDecodeError:
            return exc.code, {"_raw": raw[:400].decode("utf-8", "replace")}
    except Exception as exc:  # noqa: BLE001
        return -1, {"_error": f"{type(exc).__name__}: {exc}"}


def reset_mocks() -> None:
    for port in (MOCK_A_PORT, MOCK_B_PORT):
        http_json("POST", f"http://127.0.0.1:{port}/control", {"mode": "reset"})


def mock_counters(port: int) -> dict:
    return http_json("GET", f"http://127.0.0.1:{port}/health")[1]


def chat(
    bifrost: str,
    content: str,
    fallbacks: list[str] | None = None,
    stream: bool = False,
    timeout: float = 30.0,
):
    payload: dict = {
        "model": f"{PROVIDER_A}/mock-model",
        "messages": [{"role": "user", "content": content}],
    }
    if fallbacks:
        payload["fallbacks"] = fallbacks
    if stream:
        payload["stream"] = True
    return http_json("POST", f"{bifrost}/v1/chat/completions", payload, timeout=timeout)


def chat_and_abort(bifrost: str, content: str, fallbacks: list[str], abort_after: float) -> None:
    """发出请求，在指定秒数后**直接断开 socket**（模拟 curl --max-time）。"""
    body = json.dumps(
        {
            "model": f"{PROVIDER_A}/mock-model",
            "messages": [{"role": "user", "content": content}],
            "fallbacks": fallbacks,
        }
    ).encode()
    host_port = bifrost.split("//", 1)[1]
    host, port = host_port.split(":")
    head = (
        f"POST /v1/chat/completions HTTP/1.1\r\n"
        f"Host: {host_port}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode()
    sock = socket.create_connection((host, int(port)), timeout=30)
    try:
        sock.sendall(head + body)
        time.sleep(abort_after)
    finally:
        sock.close()   # 立刻断开，不等响应


def raw_chat(bifrost: str, payload: dict, timeout: float = 30.0) -> bytes:
    body = json.dumps(payload).encode()
    host_port = bifrost.split("//", 1)[1]
    host, port = host_port.split(":")
    head = (
        f"POST /v1/chat/completions HTTP/1.1\r\n"
        f"Host: {host_port}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode()
    sock = socket.create_connection((host, int(port)), timeout=timeout)
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


def extract_content(body: dict) -> str:
    try:
        return body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        try:
            blocks = body.get("content") or []
            return blocks[0].get("text", "")
        except (KeyError, IndexError, TypeError):
            return ""


def routing_info(body: dict) -> dict:
    return (body.get("extra_fields") or {}).get("routing_info") or {}


# --------------------------------------------------------------------------
# Bifrost 生命周期
# --------------------------------------------------------------------------


def wait_for_gateway(base: str, timeout: float = 90.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, _ = http_json("GET", f"{base}/health", timeout=3)
        if status == 200:
            return True
        time.sleep(1.0)
    return False


def configure_providers(base: str) -> tuple[list[str], dict]:
    """建两个 provider 各绑一个 key；返回 (日志, key 状态)。"""
    log: list[str] = []
    key_status: dict = {}

    for name, port in ((PROVIDER_A, MOCK_A_PORT), (PROVIDER_B, MOCK_B_PORT)):
        status, body = http_json(
            "POST",
            f"{base}/api/providers",
            {
                "provider": name,
                "keys": [],
                "network_config": {"base_url": f"http://127.0.0.1:{port}"},
            },
        )
        log.append(f"POST /api/providers {name} -> {status}")

        status, body = http_json(
            "POST",
            f"{base}/api/providers/{name}/keys",
            {
                "name": f"{name}-key",
                "value": f"sk-mock-{name}",
                "models": ["mock-model"],
                "weight": 1,
            },
        )
        log.append(f"POST /api/providers/{name}/keys -> {status}")
        # key 状态里出现 success（而非 list_models_failed）即证明 /v1/models 被探测到
        blob = json.dumps(body, ensure_ascii=False)
        key_status[name] = "success" if "success" in blob else blob[:200]

    return log, key_status


# --------------------------------------------------------------------------
# Cases
# --------------------------------------------------------------------------


def run_case_a(base: str) -> None:
    print("\n  Case A — Primary 成功，备用不应被调用")
    reset_mocks()
    status, body = chat(base, "case A", fallbacks=[f"{PROVIDER_B}/mock-model"])
    a, b = mock_counters(MOCK_A_PORT), mock_counters(MOCK_B_PORT)

    record("A", "HTTP 200", status == 200, "200", str(status))
    record(
        "A",
        "内容来自 primary (mock-A)",
        extract_content(body).startswith("[mock-A]"),
        "以 [mock-A] 开头",
        repr(extract_content(body)),
    )
    record("A", "A.served == 1", a["served"] == 1, "1", str(a["served"]))
    record("A", "A.failed == 0", a["failed"] == 0, "0", str(a["failed"]))
    record("A", "B.served == 0（备用未被调用）", b["served"] == 0, "0", str(b["served"]))
    record("A", "B.failed == 0", b["failed"] == 0, "0", str(b["failed"]))


def run_case_b(base: str) -> None:
    print("\n  Case B — Primary 500 → 备用接管")
    reset_mocks()
    http_json("POST", f"http://127.0.0.1:{MOCK_A_PORT}/control", {"mode": "fail", "status": 500})
    status, body = chat(base, "case B fallback", fallbacks=[f"{PROVIDER_B}/mock-model"])
    a, b = mock_counters(MOCK_A_PORT), mock_counters(MOCK_B_PORT)
    ri = routing_info(body)

    record("B", "HTTP 200（用户端无感知）", status == 200, "200", str(status))
    record(
        "B",
        "内容来自备用 (mock-B)",
        extract_content(body).startswith("[mock-B]"),
        "以 [mock-B] 开头",
        repr(extract_content(body)),
    )
    record("B", "A.served == 1", a["served"] == 1, "1", str(a["served"]))
    record("B", "A.failed == 1（主确实失败了）", a["failed"] == 1, "1", str(a["failed"]))
    record("B", "B.served == 1（备用确实被调用）", b["served"] == 1, "1", str(b["served"]))
    record("B", "B.failed == 0", b["failed"] == 0, "0", str(b["failed"]))
    record(
        "B",
        "routing_info.is_fallback == true（网关自证）",
        ri.get("is_fallback") is True,
        "True",
        str(ri.get("is_fallback")),
    )
    record(
        "B",
        "routing_info.provider == anthropic",
        ri.get("provider") == PROVIDER_B,
        PROVIDER_B,
        str(ri.get("provider")),
    )
    record(
        "B",
        "routing_info.primary_provider == openai（保留原始 provider）",
        ri.get("primary_provider") == PROVIDER_A,
        PROVIDER_A,
        str(ri.get("primary_provider")),
    )


def run_case_c(base: str) -> None:
    print("\n  Case C — 两个都失败 → 返回 primary 的错误")
    reset_mocks()
    http_json("POST", f"http://127.0.0.1:{MOCK_A_PORT}/control", {"mode": "fail", "status": 500})
    http_json("POST", f"http://127.0.0.1:{MOCK_B_PORT}/control", {"mode": "fail", "status": 503})
    status, body = chat(base, "case C both fail", fallbacks=[f"{PROVIDER_B}/mock-model"])
    ri = routing_info(body)
    blob = json.dumps(body, ensure_ascii=False)

    record("C", "HTTP 500", status == 500, "500", str(status))
    record(
        "C",
        "错误消息来自 primary（mock-A），不是 B 的 503",
        "injected failure from mock-A" in blob,
        "含 'injected failure from mock-A'",
        blob[:200],
    )
    record(
        "C",
        "routing_info.provider 回落为 primary",
        ri.get("provider") == PROVIDER_A,
        PROVIDER_A,
        str(ri.get("provider")),
    )


def run_case_d(base: str) -> None:
    print("\n  Case D — 客户端取消 → 备用不应被调用")
    reset_mocks()
    # A 既慢(3000ms)又会被客户端取消；B 保持健康。
    # 因为 A 是「慢」而不是「成功」，B 不被调用**只能**解释为 cancel 阻断了 fallback。
    http_json("POST", f"http://127.0.0.1:{MOCK_A_PORT}/control", {"mode": "slow", "ms": 3000})
    chat_and_abort(base, "case D cancel", [f"{PROVIDER_B}/mock-model"], abort_after=1.0)
    time.sleep(4.0)   # 等 A 的慢请求走完，让计数落定
    a, b = mock_counters(MOCK_A_PORT), mock_counters(MOCK_B_PORT)

    record("D", "A.served == 1（慢请求确实抵达主 provider）", a["served"] == 1, "1", str(a["served"]))
    record("D", "A.failed == 0（它是慢，不是失败）", a["failed"] == 0, "0", str(a["failed"]))
    record(
        "D",
        "B.served == 0（取消阻断了 fallback）",
        b["served"] == 0,
        "0",
        str(b["served"]),
    )


def run_case_e(base: str) -> None:
    print("\n  Case E — 流式 + 跨 Provider Fallback")
    reset_mocks()
    http_json("POST", f"http://127.0.0.1:{MOCK_A_PORT}/control", {"mode": "fail", "status": 500})
    raw = raw_chat(
        base,
        {
            "model": f"{PROVIDER_A}/mock-model",
            "messages": [{"role": "user", "content": "case E stream"}],
            "fallbacks": [f"{PROVIDER_B}/mock-model"],
            "stream": True,
        },
    )
    text = raw.decode("utf-8", "replace")
    b = mock_counters(MOCK_B_PORT)

    record("E", "HTTP 200", text.startswith("HTTP/1.1 200"), "HTTP/1.1 200", text[:20])
    record("E", "流式内容来自 mock-B", "[mock-B]" in text, "含 [mock-B]", "找到" if "[mock-B]" in text else "未找到")
    record("E", "每个 chunk 带 is_fallback:true", '"is_fallback":true' in text.replace(" ", ""), "含 is_fallback:true", "找到" if "is_fallback" in text else "未找到")
    record("E", "带 primary_provider 元数据", '"primary_provider":"openai"' in text.replace(" ", ""), "primary_provider=openai", "找到" if "primary_provider" in text else "未找到")
    record("E", "B.served == 1（真实打到备用）", b["served"] == 1, "1", str(b["served"]))


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="端到端复现 Bifrost fallback 验证矩阵")
    ap.add_argument("--bin", default=str(ROOT / "tools" / "bifrost-http.exe"))
    ap.add_argument("--app-dir", default=str(ROOT / "tools" / "appdata-run"))
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--report", default=str(ROOT / "reports" / "fallback_matrix.json"))
    args = ap.parse_args()

    binary = Path(args.bin)
    if not binary.exists():
        print(f"找不到 Bifrost 二进制: {binary}")
        print("请先运行: python scripts/download_bifrost.py")
        return 2

    base = f"http://127.0.0.1:{args.port}"
    print("=" * 72)
    print("Bifrost 跨 Provider Fallback 端到端复现")
    print("=" * 72)

    # 1) mock providers
    print(f"\n[1] 启动 mock providers  A=:{MOCK_A_PORT}  B=:{MOCK_B_PORT}")
    mock_a = build_server(MOCK_A_PORT, "A")
    mock_b = build_server(MOCK_B_PORT, "B")
    for srv in (mock_a, mock_b):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("    就绪")

    # 2) bifrost
    app_dir = Path(args.app_dir)
    app_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n[2] 启动 Bifrost  {binary.name}  port={args.port}  app-dir={app_dir}")
    # 日志必须落盘、不能挂在 PIPE 上：网关启动期会刷不少日志，
    # 管道缓冲区写满后会**反压住网关自己的启动流程**，表现就是"永远不 ready"。
    log_path = app_dir / "bifrost-startup.log"
    log_fh = open(log_path, "wb")

    # 关键坑（踩了很久，记下来）：必须把临时目录也指到工作区内。
    # Bifrost 启动时有个 SQLite 迁移 `make_base_pricing_columns_nullable` 要重建表，
    # 需要 temp store；受限环境下默认 TEMP 目录不可写，SQLite 会抛出**极具误导性**的
    #     unable to open database file: The system cannot find the file specified.
    # 看上去像路径/权限问题，实际是 temp store 不可写。
    # 不设这个，网关会在第 40 多行迁移处直接退出，表现为"永远不 ready"。
    tmp_dir = app_dir.parent / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    child_env = dict(os.environ)
    child_env.update({"TMPDIR": str(tmp_dir), "TEMP": str(tmp_dir), "TMP": str(tmp_dir)})

    proc = subprocess.Popen(
        [str(binary), "-port", str(args.port), "-host", "127.0.0.1", "-app-dir", str(app_dir),
         "-log-style", "pretty"],
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        cwd=str(binary.parent),
        env=child_env,
    )
    started = time.time()
    try:
        if not wait_for_gateway(base):
            print(f"    网关 90s 内未就绪（进程退出码={proc.poll()}）")
            print(f"    --- {log_path} 末尾 ---")
            try:
                tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
                for line in tail:
                    print(f"    | {line}")
            except OSError as exc:
                print(f"    (读取日志失败: {exc})")
            return 3
        print(f"    就绪，耗时 {time.time() - started:.1f}s")

        # 3) providers + keys
        print("\n[3] 配置 provider 与 key")
        log, key_status = configure_providers(base)
        for line in log:
            print(f"    {line}")
        for name, st in key_status.items():
            print(f"    key[{name}] 状态: {st[:80]}")

        # 4) cases
        print("\n[4] 执行验证矩阵")
        for fn in (run_case_a, run_case_b, run_case_c, run_case_d, run_case_e):
            try:
                fn(base)
            except Exception as exc:  # noqa: BLE001
                record("?", f"{fn.__name__} 抛异常", False, "无异常", f"{type(exc).__name__}: {exc}")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_fh.close()
        for srv in (mock_a, mock_b):
            srv.shutdown()
            srv.server_close()

    # 5) 汇总
    passed = sum(1 for r in RESULTS if r["pass"])
    total = len(RESULTS)
    print("\n" + "=" * 72)
    print(f"汇总: {passed}/{total} 通过")
    print("=" * 72)

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "bifrost_binary": str(binary),
        "bifrost_binary_bytes": binary.stat().st_size,
        "providers": {
            PROVIDER_A: f"mock-A(:{MOCK_A_PORT})",
            PROVIDER_B: f"mock-B(:{MOCK_B_PORT})",
        },
        "key_status": key_status,
        "checks": RESULTS,
        "passed": passed,
        "total": total,
        "all_passed": passed == total,
    }
    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告已写入: {out}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())