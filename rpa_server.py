#!/usr/bin/env python3
"""RPA 定制镜像的 HTTP 服务（运行在 rpa-chrome-server 容器内）：会话文件浏览 + 浏览器快照。

供财喵 dashboard 运维使用，仅经 compose 网络被 api 访问（不发布宿主端口）：

1. 会话文件浏览：只读暴露 RPA 会话目录（默认 /tmp/.sessions），
   提供 JSON 目录列表 + 文件下载/预览接口，路径严格限制在根目录内
   （.. / 绝对路径 / symlink 逃逸均拒绝），可用 token 鉴权。
2. 浏览器快照：POST /api/snapshot 在【容器内】直连本地 Chrome CDP
   （127.0.0.1:9222），枚举业务页面逐个截图。默认不落盘、图片 base64 内联
   返回（调用方可直接预览）；调用方可传 save_to_disk=true 同时落盘并返回路径。
   CDP 端口不出容器 —— 网络侧永远接触不到 Chrome。

接口：
    GET  /api/list?path=<相对路径>&token=...
         {"ok":true,"root":<root>,"path":<当前相对>,"parent":<上级相对>,
          "files":[{name,type,size,mtime}]}
    GET  /api/file?path=<相对路径>&token=...
         文件字节流（Content-Type 按扩展名，图片可直接内联预览）
    POST /api/snapshot
         body(可选): {"save_to_disk": bool}     # 默认 false
         → {"ok":true,"endpoint":...,"ts":...,"total_pages":N,"save_to_disk":bool,
            "pages":[{index,url,path,image_base64}],
            "failed":[{index,url,error}], "dir":...}
         image_base64 始终返回（PNG 字节 base64，可直接 <img> 预览）；
         save_to_disk=true 时同时落盘到 screenshots/，path 为相对 root 的路径
         （如 screenshots/20260818-...-0.png），可直接经 /api/file 取回；
         save_to_disk=false 时 path 与 dir 均为 ""。

安全：token 匹配（query `token=` 或 header `X-Auth-Token`）才放行；
未设置 token 时打印警告并全放行（仅建议内网调试）。
"""
import argparse
import asyncio
import base64
import json
import mimetypes
import os
import tempfile
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# mimetypes 对 .png/.jpg 有内置映射，这里补几个常见扩展
for _ext, _ct in {".webp": "image/webp", ".svg": "image/svg+xml"}.items():
    mimetypes.add_type(_ct, _ext)

# ── RPA 浏览器快照 ──────────────────────────────────────────────
# 在【容器内】直连本地 Chrome CDP（127.0.0.1:9222，playwright 随镜像安装），
# 枚举业务页面逐个截图。save_to_disk=false（默认）不落盘，图片 base64 内联返回；
# true 时同时落盘到 screenshots 目录并返回相对 root 的路径。
# 注意：只 disconnect，不 close browser —— connect_over_cdp 的 close 会关掉
# worker 共享的 Chrome。
_SKIP_PREFIX = ("chrome://", "devtools://", "edge://", "about:", "chrome-extension://")


async def _capture_snapshot(save_to_disk: bool = False) -> dict:
    from playwright.async_api import async_playwright

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    shot_dir: Path | None = None
    if save_to_disk:
        shot_dir = Path(tempfile.gettempdir()) / ".sessions" / "screenshots"
        shot_dir.mkdir(parents=True, exist_ok=True)

    pages: list = []
    captured: list[dict] = []
    failed: list[dict] = []

    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:9222", timeout=15000)
        for ctx in browser.contexts:
            for page in ctx.pages:
                url = page.url or ""
                if url.startswith(_SKIP_PREFIX):
                    continue
                pages.append(page)

        for idx, page in enumerate(pages):
            try:
                img = await page.screenshot(full_page=False)  # 不传 path → 返回字节，不落盘
            except Exception as e:  # noqa: BLE001
                failed.append({"index": idx, "url": page.url or "", "error": f"{type(e).__name__}: {e}"})
                continue
            entry = {
                "index": idx,
                "url": page.url or "",
                "path": "",
                "image_base64": base64.b64encode(img).decode("ascii"),
            }
            if shot_dir is not None:
                name = f"{ts}-rpa-snapshot-{idx}.png"
                (shot_dir / name).write_bytes(img)
                # 相对 root 的路径，dashboard 可直接经 /api/file?path=... 取回
                entry["path"] = f"screenshots/{name}"
            captured.append(entry)
    finally:
        await pw.stop()

    return {
        "endpoint": "http://127.0.0.1:9222",
        "ts": ts,
        "total_pages": len(pages),
        "save_to_disk": save_to_disk,
        "pages": captured,
        "failed": failed,
        "dir": str(shot_dir) if shot_dir else "",
    }


def _to_rel(rel: str) -> str:
    """把用户输入的路径规整成根目录下的相对路径（容忍前导 / 与反斜杠）。"""
    rel = (rel or "").replace("\\", "/").strip()
    while rel.startswith("/"):
        rel = rel[1:]
    return rel


def _safe_resolve(root: Path, rel: str):
    """把相对路径解析到 root 内；越界（.. / 绝对路径 / symlink 逃逸）返回 None。"""
    target = (root / _to_rel(rel)).resolve()
    if target != root and root not in target.parents:
        return None
    return target


class Handler(BaseHTTPRequestHandler):
    server_version = "RPAServer/1.0"
    root: Path = Path("/tmp/.sessions")
    token: str = ""

    # ── 工具 ───────────────────────────────────────────────────
    def _ok(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code: int, msg: str) -> None:
        self._ok(code, json.dumps({"ok": False, "error": msg}, ensure_ascii=False).encode())

    def _authorized(self) -> bool:
        if not self.token:
            return True
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        return q.get("token") == [self.token] or self.headers.get("X-Auth-Token") == self.token

    def do_GET(self):
        if not self._authorized():
            self._err(403, "forbidden")
            return
        parts = urllib.parse.urlsplit(self.path)
        qs = urllib.parse.parse_qs(parts.query)
        if parts.path == "/api/list":
            self._handle_list(qs)
        elif parts.path == "/api/file":
            self._handle_file(qs)
        else:
            self._err(404, "not found")

    def _read_json_body(self) -> dict:
        """读取可选的 JSON 请求体；无 body / 非法 JSON 时返回 {}。"""
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return {}
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return {}
        return body if isinstance(body, dict) else {}

    def do_POST(self):
        if not self._authorized():
            self._err(403, "forbidden")
            return
        parts = urllib.parse.urlsplit(self.path)
        if parts.path != "/api/snapshot":
            self._err(404, "not found")
            return
        save_to_disk = bool(self._read_json_body().get("save_to_disk"))
        try:
            result = asyncio.run(_capture_snapshot(save_to_disk=save_to_disk))
        except Exception as e:  # noqa: BLE001
            self._err(500, f"snapshot failed: {type(e).__name__}: {e}")
            return
        self._ok(200, json.dumps(result, ensure_ascii=False).encode())

    # ── 目录列表 ───────────────────────────────────────────────
    def _handle_list(self, qs):
        rel = qs.get("path", ["/"])[0].strip() or "/"
        target = _safe_resolve(self.root, rel)
        if target is None:
            self._err(400, "path escapes root")
            return
        if not target.is_dir():
            self._err(404, "directory not found")
            return
        files = []
        for p in sorted(target.iterdir()):
            try:
                st = p.stat()
            except OSError:
                continue
            is_dir = p.is_dir()
            files.append({
                "name": p.name,
                "type": "dir" if is_dir else ("link" if p.is_symlink() else "file"),
                "size": None if is_dir else st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(timespec="seconds"),
            })
        rel_norm = "" if target == self.root else str(target.relative_to(self.root))
        parent = rel_norm.rsplit("/", 1)[0] if "/" in rel_norm else ""
        self._ok(200, json.dumps({
            "ok": True, "root": str(self.root),
            "path": rel_norm, "parent": parent, "files": files,
        }, ensure_ascii=False).encode())

    # ── 文件内容 ───────────────────────────────────────────────
    def _handle_file(self, qs):
        rel = qs.get("path", [""])[0].strip()
        if not rel:
            self._err(400, "path required")
            return
        target = _safe_resolve(self.root, rel)
        if target is None:
            self._err(400, "path escapes root")
            return
        if not target.is_file():
            self._err(404, "file not found")
            return
        try:
            data = target.read_bytes()
        except OSError as e:
            self._err(500, str(e))
            return
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._ok(200, data, ctype)

    def log_message(self, *args):  # 静默，避免刷屏
        pass


def main():
    ap = argparse.ArgumentParser(description="RPA 定制镜像服务（会话文件浏览 + 浏览器快照）")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=9224)
    ap.add_argument("--root", default=os.environ.get("RPA_FILE_ROOT", "/tmp/.sessions"))
    ap.add_argument("--token", default=os.environ.get("RPA_FILE_TOKEN", ""))
    args = ap.parse_args()
    Handler.root = Path(args.root).resolve()
    Handler.root.mkdir(parents=True, exist_ok=True)
    Handler.token = args.token or ""
    if Handler.token:
        print(f"[rpa-server] serving {Handler.root} on {args.host}:{args.port} (token ON)", flush=True)
    else:
        print(f"[rpa-server] ⚠️ serving {Handler.root} on {args.host}:{args.port} (NO AUTH)", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
