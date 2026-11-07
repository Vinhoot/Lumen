# -*- coding: utf-8 -*-
"""内嵌 HTTP 服务：前端静态资源 + 缩略图 + 媒体流（Range，视频可拖进度条）。

只监听 127.0.0.1；页面与媒体同源，WebView2 里 <video>/<img> 直接可用。
"""
import mimetypes
import json
import os
import secrets
import threading
import traceback
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_thumb_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="th")

_VIDEO_MIME = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm",
    ".mkv": "video/x-matroska", ".mov": "video/quicktime", ".avi": "video/x-msvideo",
    ".wmv": "video/x-ms-wmv", ".flv": "video/x-flv", ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg", ".ts": "video/mp2t", ".3gp": "video/3gpp",
}
_CHUNK = 1024 * 512


class Handler(BaseHTTPRequestHandler):
    api = None       # PyApi 实例
    web_dir = None   # 前端静态目录
    token = ""       # 兼容模式的一次性令牌（URL 里带 ?k=，防止本机其它程序乱调 RPC）

    def log_message(self, *args):
        pass

    # ---------- 工具 ----------
    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_file(self, path, ctype, cache="max-age=86400"):
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            self._send(404, b"")
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(data)

    # ---------- 路由 ----------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path == "/" or path == "/index.html":
                self._static("index.html", "text/html; charset=utf-8", cache="no-store")
            elif path in ("/app.css", "/app.js"):
                ctype = ("text/css; charset=utf-8" if path.endswith(".css")
                         else "application/javascript; charset=utf-8")
                self._static(path.lstrip("/"), ctype, cache="no-store")
            elif path.startswith("/thumb/"):
                self._thumb(path)
            elif path.startswith("/media/"):
                self._media(path)
            else:
                self._send(404, b"")
        except Exception:
            try:
                self._send(500, b"")
            except Exception:
                pass

    def _static(self, rel, ctype, cache="no-store"):
        p = os.path.join(self.web_dir, rel)
        self._send_file(p, ctype, cache=cache)

    # ---------- 兼容模式的 RPC（浏览器界面 → 后端方法） ----------
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/rpc":
            self._send(404, b"")
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            req = json.loads(raw.decode("utf-8") or "{}")
            if self.token and req.get("t") != self.token:
                self._send(403, b'{"error":"forbidden"}', "application/json; charset=utf-8")
                return
            name = req.get("m") or ""
            args = req.get("a") or []
            if isinstance(args, dict):
                args = [args]
            if not isinstance(args, list):
                args = [args]
            # 只放行 PyApi 上真实存在的公开方法（下划线开头的内部方法一律拒绝）
            if not name or name.startswith("_") or not hasattr(self.api, name):
                self._send(400, json.dumps({"error": "no such method: " + str(name)}).encode(),
                           "application/json; charset=utf-8")
                return
            fn = getattr(self.api, name)
            if not callable(fn):
                self._send(400, b'{"error":"not callable"}', "application/json; charset=utf-8")
                return
            result = fn(*args)
            body = json.dumps({"result": result}, ensure_ascii=False, default=str)
            self._send(200, body.encode("utf-8"), "application/json; charset=utf-8")
        except Exception:
            err = traceback.format_exc(limit=4)
            try:
                self._send(500, json.dumps({"error": err}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception:
                pass

    def _thumb(self, path):
        # /thumb/<wsid>/<fid>[?size=320]
        try:
            _p, wsid, fid = path.rsplit("/", 2)
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            size = int(q.get("size", ["320"])[0])
        except ValueError:
            self._send(404, b"")
            return
        fut = _thumb_pool.submit(self.api.getThumbFile, int(wsid), int(fid), size)
        try:
            result = fut.result(timeout=25)
        except Exception:
            result = None
        if result and os.path.isfile(result):
            self._send_file(result, "image/jpeg", cache="max-age=86400")
        else:
            self._send(404, b"")

    def _media(self, path):
        """原始媒体流：支持 Range（视频拖动进度条必需）。"""
        try:
            _p, wsid, fid = path.rsplit("/", 2)
        except ValueError:
            self._send(404, b"")
            return
        fpath, kind, ext = self.api.getMediaPath(int(wsid), int(fid))
        if not fpath:
            self._send(404, b"")
            return
        ext = (ext or os.path.splitext(fpath)[1]).lower()
        ctype = _VIDEO_MIME.get(ext) or mimetypes.guess_type(fpath)[0] \
            or "application/octet-stream"
        try:
            total = os.path.getsize(fpath)
        except OSError:
            self._send(404, b"")
            return

        range_header = self.headers.get("Range")
        start, end = 0, total - 1
        code = 200
        if range_header:
            # 形如 bytes=START-END（Chrome 视频请求）
            try:
                unit, rng = range_header.split("=", 1)
                s, _, e = rng.partition("-")
                if s:
                    start = int(s)
                if e:
                    end = min(int(e), total - 1)
                if start > end or start >= total:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{total}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                code = 206
            except ValueError:
                pass

        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.end_headers()
        try:
            with open(fpath, "rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(_CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass


def start(api, web_dir, host="127.0.0.1", port=0, with_token=False):
    Handler.api = api
    Handler.web_dir = web_dir
    Handler.token = secrets.token_urlsafe(12) if with_token else ""
    server = ThreadingHTTPServer((host, port), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True, name="media-server").start()
    tail = f"?k={Handler.token}" if Handler.token else ""
    return f"http://{host}:{port}/{tail}", server
