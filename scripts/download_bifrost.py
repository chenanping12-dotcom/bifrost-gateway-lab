#!/usr/bin/env python3
"""下载 Bifrost v2.0.0 单文件网关二进制（Windows/amd64，约 112 MB）。

    python scripts/download_bifrost.py

为什么不用 `npx`：那次实测里 npx 的下载器 3 分钟只到 34%，而且**中断会留下损坏的
二进制**——它用 `existsSync` 判断是否已下载，下次会直接把损坏文件当好的执行。
所以这里改成可续传的直接下载，并在下载完成后**校验字节数**。

支持断点续传：重复执行会从已下载的字节继续，不会从头再来。
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "tools" / "bifrost-http.exe"
URL = "https://downloads.getmaxim.ai/bifrost/v2.0.0/windows/amd64/bifrost-http.exe"

# 已核实的官方体积。字节数对得上，就说明下到的是同一个构建、且没被截断。
EXPECTED_BYTES = 117_577_216


def main() -> int:
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    have = TARGET.stat().st_size if TARGET.exists() else 0

    if have == EXPECTED_BYTES:
        print(f"已存在且字节数正确: {TARGET}  ({have:,} bytes)")
        return 0
    if have > EXPECTED_BYTES:
        print(f"本地文件比预期大（{have:,} > {EXPECTED_BYTES:,}），删除后重下")
        TARGET.unlink()
        have = 0

    if have:
        print(f"从 {have:,} 字节处续传 ...")
    else:
        print(f"开始下载 {URL}")

    req = urllib.request.Request(URL)
    if have:
        req.add_header("Range", f"bytes={have}-")

    mode = "ab" if have else "wb"
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, open(TARGET, mode) as fh:
            total = int(resp.headers.get("Content-Length") or 0) + have
            done = have
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if total:
                    pct = done * 100 / total
                    print(
                        f"\r  {done / 1e6:7.1f} / {total / 1e6:7.1f} MB  ({pct:5.1f}%)",
                        end="",
                        flush=True,
                    )
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"\n下载中断: {exc}")
        print("直接重跑本脚本即可续传。")
        return 1

    print()
    size = TARGET.stat().st_size
    if size != EXPECTED_BYTES:
        print(f"字节数不符: 得到 {size:,}，预期 {EXPECTED_BYTES:,}")
        print("文件可能被截断，请重跑本脚本续传。")
        return 1

    print(f"完成: {TARGET}  ({size:,} bytes)  字节数与官方构建一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())