#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""toolkit_core.py — 大云壁画工具箱公共底座

本文件由 SERIES-SPEC v1.0 生成，请勿在单个仓库内单独修改。
需要变更请改主副本后同步到全部工具仓库。
同步版本：1.0.0

设计目标（只做这些，不做业务逻辑）：

  1. 安全路径解析 —— 杜绝静态文件路径越界（历史 P0 漏洞）
  2. 受限 CORS + OPTIONS —— 支持本机页面跨端口/文件调用
  3. 分块文件传输 —— 不再把大图整份读进内存
  4. 统一 JSON 响应与错误结构 —— 前端可以统一处理
  5. 统一启动横幅 —— 五个工具输出格式一致

使用方式（在各自的 server.py 里）::

    import toolkit_core as core

    class Handler(BaseHTTPRequestHandler):
        def do_OPTIONS(self):
            core.handle_options(self)

        def do_GET(self):
            rel = core.strip_url_path(self.path)
            try:
                target = core.safe_join(WEB_ROOT, rel)
            except core.PathEscapeError:
                core.api_error(self, "FORBIDDEN", "请求的路径不被允许。", status=403)
                return
            core.stream_file(self, target)
"""

from __future__ import annotations

import json
import mimetypes
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

# --------------------------------------------------------------------------
# 系列常量
# --------------------------------------------------------------------------

SERIES_NAME = "大云壁画工具箱"
SERIES_NAME_EN = "Dayun Mural Toolkit"
SERIES_SPEC = "SERIES-SPEC v1.0"
TOOLKIT_CORE_VERSION = "1.0.0"

#: 端口分配表（见 SERIES-SPEC §2），启动台用它显示与探测
PORT_MAP = {
    "launcher": 8700,
    "xiangliu-grid": 8765,
    "baize-review": 8766,
    "jingwei": 8786,
    "diffeye": 5055,
}

#: 传输与请求上限
DEFAULT_CHUNK = 1024 * 1024          # 分块传输 1 MB
MAX_JSON_BYTES = 1024 * 1024         # JSON 请求体 1 MB
ALLOWED_ORIGIN_HOSTS = ("127.0.0.1", "localhost", "::1")

__all__ = [
    "SERIES_NAME",
    "SERIES_NAME_EN",
    "SERIES_SPEC",
    "TOOLKIT_CORE_VERSION",
    "PORT_MAP",
    "DEFAULT_CHUNK",
    "MAX_JSON_BYTES",
    "ToolkitError",
    "PathEscapeError",
    "PayloadTooLargeError",
    "ValidationError",
    "NotFoundError",
    "is_inside",
    "safe_join",
    "strip_url_path",
    "safe_filename",
    "unique_path",
    "guess_content_type",
    "install_utf8_stdout",
    "stream_file",
    "serve_static",
    "json_response",
    "api_ok",
    "api_error",
    "api_exception",
    "read_json",
    "origin_allowed",
    "apply_cors",
    "handle_options",
    "print_banner",
    "health_payload",
]


# --------------------------------------------------------------------------
# 错误类型：统一带 code / message / field / detail / status
# --------------------------------------------------------------------------


class ToolkitError(Exception):
    """底座统一异常。``message`` 必须是能直接显示给用户的中文。"""

    code = "TOOLKIT_ERROR"
    status = 400
    message = "请求处理失败。"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        field: str | None = None,
        detail: str | None = None,
        status: int | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.field = field
        self.detail = detail
        self.status = status or self.status
        super().__init__(self.message)

    def as_dict(self) -> dict:
        payload: dict = {"code": self.code, "message": self.message}
        if self.field:
            payload["field"] = self.field
        if self.detail:
            payload["detail"] = self.detail
        return {"error": payload}


class PathEscapeError(ToolkitError):
    code = "PATH_ESCAPE"
    status = 403
    message = "请求的路径超出了允许的目录范围。"


class PayloadTooLargeError(ToolkitError):
    code = "PAYLOAD_TOO_LARGE"
    status = 413
    message = "请求内容过大，已拒绝。"


class ValidationError(ToolkitError):
    code = "INVALID_ARGUMENT"
    status = 400
    message = "参数不合法。"


class NotFoundError(ToolkitError):
    code = "NOT_FOUND"
    status = 404
    message = "找不到请求的资源。"


# --------------------------------------------------------------------------
# 路径安全
# --------------------------------------------------------------------------


def is_inside(child: str | Path, parent: str | Path) -> bool:
    """判断 ``child`` 解析后是否位于 ``parent`` 之内（真路径判断，非字符串前缀）。

    一律先 ``resolve()``，因此 ``..``、符号链接、Windows 反斜杠都会被展开后再比较。
    """

    child_path = Path(child).resolve()
    parent_path = Path(parent).resolve()
    if child_path == parent_path:
        return True
    try:
        child_path.relative_to(parent_path)
    except ValueError:
        return False
    return True


def urlparse_path(url_path: str) -> str:
    """安全地取出 URL 的 path 部分，解析失败时退回原串。"""

    try:
        return urlsplit(url_path).path or ""
    except ValueError:
        return url_path.split("?", 1)[0]


def strip_url_path(url_path: str) -> str:
    """从请求行里取出路径部分并去掉前导斜杠。

    ``BaseHTTPRequestHandler`` 的 ``self.path`` 形如 ``/a/b.png?x=1``；
    本函数只返回 ``a/b.png``。
    """

    return urlparse_path(url_path).lstrip("/")


def safe_join(root: str | Path, rel: str, *, must_exist: bool = False) -> Path:
    """把 URL 相对路径安全地拼到 ``root`` 之下。

    依次做：URL 解码 → 反斜杠归一 → 去掉前导斜杠 → ``resolve()`` →
    **必须落在 root 内**。任何越界尝试抛 :class:`PathEscapeError`。

    这是修复"``GET /..\\server.py`` 能读出源码"这类漏洞的唯一入口，
    所有静态文件与输出文件都必须经过它。
    """

    root_path = Path(root).resolve()
    raw = unquote(str(rel or "")).strip()
    # Windows 的反斜杠同样具有路径分隔语义，必须先归一，否则 `..\x` 会绕过检查
    raw = raw.replace("\\", "/").lstrip("/")

    if not raw:
        raise NotFoundError("请求路径为空。", detail=f"rel={rel!r}")

    candidate = (root_path / raw).resolve()

    if not is_inside(candidate, root_path):
        # detail 里只放请求方自己发来的相对路径，不回显服务端绝对路径，
        # 避免把本机目录结构带进错误信息、进而出现在截图或反馈里。
        raise PathEscapeError(
            "请求的路径超出了允许的目录范围。",
            detail=f"rel={rel!r}",
        )

    if must_exist and not candidate.exists():
        raise NotFoundError(f"找不到文件：{candidate.name}", detail=f"name={candidate.name!r}")

    return candidate


_ILLEGAL_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


def safe_filename(name: str, *, fallback: str = "file") -> str:
    """把上传文件名清洗成安全的纯文件名（保留中文，去掉路径与非法字符）。"""

    base = Path(str(name or "")).name
    base = _ILLEGAL_FILENAME.sub("_", base).strip(" .")
    if base.upper().split(".")[0] in _WINDOWS_RESERVED:
        base = f"_{base}"
    return base or fallback


def unique_path(directory: str | Path, filename: str) -> Path:
    """返回一个不会覆盖已有文件的路径；重名时自动追加 ``_1``、``_2``……"""

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    cleaned = safe_filename(filename)
    candidate = directory / cleaned
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    index = 1
    while True:
        candidate = directory / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


# --------------------------------------------------------------------------
# 传输与响应
# --------------------------------------------------------------------------


#: 关键扩展名显式指定 MIME，不依赖操作系统的 mimetypes 注册表。
#: 踩过的坑：Windows 上 mimetypes.guess_type("logo.svg") 返回 "image/svg"，
#: 而浏览器只认 "image/svg+xml"，结果所有 SVG 都显示成破图。
EXPLICIT_TYPES = {
    ".svg": "image/svg+xml",
    ".html": "text/html",
    ".htm": "text/html",
    ".css": "text/css",
    ".js": "application/javascript",
    ".mjs": "application/javascript",
    ".json": "application/json",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".xml": "application/xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".wasm": "application/wasm",
    ".zip": "application/zip",
    ".pdf": "application/pdf",
}

#: 这些类型补上 utf-8，中文才不会乱码
_TEXTUAL_TYPES = {
    "text/html", "text/css", "text/plain", "text/markdown", "text/csv",
    "application/javascript", "application/json", "application/xml",
    "image/svg+xml",
}


def guess_content_type(path: str | Path) -> str:
    """按扩展名判断 Content-Type；文本类型补上 utf-8。

    优先查 :data:`EXPLICIT_TYPES`，查不到才回退到 ``mimetypes``，
    避免被 Windows 注册表里不规范的类型（如 ``image/svg``）带偏。
    """

    suffix = Path(path).suffix.lower()
    ctype = EXPLICIT_TYPES.get(suffix)
    if not ctype:
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    if ctype in _TEXTUAL_TYPES:
        return f"{ctype}; charset=utf-8"
    return ctype


def install_utf8_stdout() -> None:
    """让 Windows 控制台也能正常打印中文横幅（失败时静默忽略）。"""

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def stream_file(
    handler,
    path: str | Path,
    content_type: str | None = None,
    *,
    chunk_size: int = DEFAULT_CHUNK,
    extra_headers: dict | None = None,
    download_name: str | None = None,
    status: int = 200,
) -> None:
    """分块发送文件，避免把大图整份读进内存。"""

    file_path = Path(path)
    if not file_path.is_file():
        raise NotFoundError(f"找不到文件：{file_path.name}", detail=f"name={file_path.name!r}")

    size = file_path.stat().st_size
    handler.send_response(status)
    apply_cors(handler)
    handler.send_header("Content-Type", content_type or guess_content_type(file_path))
    handler.send_header("Content-Length", str(size))
    if download_name:
        handler.send_header(
            "Content-Disposition",
            f'attachment; filename="{safe_filename(download_name)}"',
        )
    for key, value in (extra_headers or {}).items():
        handler.send_header(key, value)
    handler.end_headers()

    with file_path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            try:
                handler.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                # 用户关掉页面 / 取消下载，不是错误
                break


def serve_static(handler, web_root: str | Path, rel: str) -> bool:
    """在 ``web_root`` 内安全地提供静态文件。返回是否已响应。

    越界抛 :class:`PathEscapeError`；文件不存在返回 False（调用方决定 404 还是回退首页）。
    """

    target = safe_join(web_root, rel)
    if not target.is_file():
        return False
    stream_file(handler, target)
    return True


def json_response(handler, payload, status: int = 200) -> None:
    """发送 JSON 响应（自动带受限 CORS 头）。"""

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    apply_cors(handler)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        pass


def api_ok(handler, payload: dict | None = None) -> None:
    """统一成功响应：``{"ok": true, ...}``"""

    body: dict = {"ok": True}
    if payload:
        body.update(payload)
    json_response(handler, body, 200)


def api_error(
    handler,
    code: str,
    message: str,
    *,
    field: str | None = None,
    detail: str | None = None,
    status: int = 400,
) -> None:
    """统一错误响应，结构见 SERIES-SPEC §7。"""

    payload: dict = {"code": code, "message": message}
    if field:
        payload["field"] = field
    if detail:
        payload["detail"] = detail
    json_response(handler, {"error": payload}, status)


def api_exception(handler, exc: BaseException) -> None:
    """把异常翻译成统一错误响应。

    :class:`ToolkitError` 按自带状态码返回；其它异常按 500 返回并保留原始信息，
    方便用户直接把 ``detail`` 贴出来排查。
    """

    if isinstance(exc, ToolkitError):
        api_error(
            handler,
            exc.code,
            exc.message,
            field=exc.field,
            detail=exc.detail,
            status=exc.status,
        )
        return
    api_error(
        handler,
        "INTERNAL_ERROR",
        "工具内部出错了，请把下方详情发给开发者。",
        detail=f"{type(exc).__name__}: {exc}",
        status=500,
    )


def read_json(handler, *, limit: int = MAX_JSON_BYTES) -> dict:
    """读取并解析 JSON 请求体，带体积上限（SERIES-SPEC §7 / S3）。"""

    raw_length = handler.headers.get("Content-Length") or "0"
    try:
        length = int(raw_length)
    except ValueError:
        raise ValidationError("Content-Length 不合法。", detail=f"value={raw_length!r}")

    if length < 0:
        raise ValidationError("Content-Length 不合法。", detail=f"value={length}")
    if length > limit:
        raise PayloadTooLargeError(
            f"请求内容过大，上限 {limit // 1024} KB。",
            detail=f"content-length={length}",
        )

    body = handler.rfile.read(length) if length else b""
    if not body:
        return {}
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("请求内容不是合法的 JSON。", detail=str(exc))
    if not isinstance(payload, dict):
        raise ValidationError("请求内容必须是 JSON 对象。")
    return payload


# --------------------------------------------------------------------------
# CORS / OPTIONS
# --------------------------------------------------------------------------


def origin_allowed(origin: str | None) -> bool:
    """只放行本机来源：``http://127.0.0.1:*``、``http://localhost:*``、``Origin: null``。

    其它来源一律不放行，避免本机工具被外部网页调用。
    """

    if not origin:
        return False
    if origin == "null":  # 用 file:// 直接打开页面时的 Origin
        return True
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return parsed.scheme == "http" and (parsed.hostname or "") in ALLOWED_ORIGIN_HOSTS


def apply_cors(handler) -> None:
    """按请求的 Origin 决定是否附加 CORS 头。必须在 ``end_headers()`` 之前调用。"""

    origin = handler.headers.get("Origin")
    handler.send_header("Vary", "Origin")
    if origin_allowed(origin):
        handler.send_header("Access-Control-Allow-Origin", origin)
        handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        handler.send_header("Access-Control-Allow-Headers", "Content-Type")
        handler.send_header("Access-Control-Max-Age", "600")


def handle_options(handler) -> None:
    """统一处理 ``OPTIONS`` 预检：204 + CORS 头。"""

    handler.send_response(204)
    apply_cors(handler)
    handler.send_header("Content-Length", "0")
    handler.end_headers()


# --------------------------------------------------------------------------
# 启动横幅与健康检查
# --------------------------------------------------------------------------


def print_banner(
    tool_cn: str,
    tool_en: str,
    version: str,
    port: int,
    *,
    host: str = "127.0.0.1",
    extra: str | None = None,
) -> None:
    """打印全系列统一的启动横幅。"""

    install_utf8_stdout()
    line = "=" * 44
    print(line)
    print(f"  {SERIES_NAME} · {tool_cn} {tool_en} v{version}")
    print(line)
    print(f"  地址：http://{host}:{port}")
    print("  关闭此窗口即停止工具。")
    if extra:
        print(f"  {extra}")
    print()


def health_payload(tool: str, version: str, port: int, **extra) -> dict:
    """统一的 ``/api/health`` 响应体，供启动台与启动脚本探测。"""

    payload = {
        "status": "ok",
        "series": SERIES_NAME,
        "tool": tool,
        "version": version,
        "port": port,
        "core": TOOLKIT_CORE_VERSION,
    }
    payload.update(extra)
    return payload
