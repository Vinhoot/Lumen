"""媒体元数据提取：图片（PIL）+ 视频（ffmpeg/ffprobe）。"""
import os
import re
import subprocess

from PIL import Image

try:
    import imageio_ffmpeg
    FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:  # 依赖缺失时优雅降级
    FFMPEG_BIN = None
EXIF_ARTIST_TAGS = (0x013B, 0x8298, 315, 33432)  # Artist / Copyright 等
XP_AUTHOR_TAG = 0x9C9D  # 40093: Windows XPAuthor，UTF-16LE 字节串（支持中文作者）


def _decode_xp(value) -> str:
    """XPAuthor 等 Windows 专用标签：bytes(UTF-16LE) 或 int 元组 → 文本。"""
    try:
        if isinstance(value, (bytes, bytearray)):
            return bytes(value).decode("utf-16-le", "ignore").rstrip("\x00").strip()
        if isinstance(value, (tuple, list)):
            return bytes(value).decode("utf-16-le", "ignore").rstrip("\x00").strip()
    except Exception:
        pass
    return ""

# Windows: 子进程不创建控制台窗口（防止 ffmpeg 黑窗闪现）
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+)(?:\.(\d+))?")
_META_RE = re.compile(r"^\s{4}(\S[^:]{0,40})\s*:\s*(.+)$", re.M)


def _ffmpeg_meta(path: str) -> dict:
    """用 ffmpeg -i 解析容器元数据与视频信息（无需 ffprobe）。"""
    out = {}
    if not FFMPEG_BIN or not os.path.isfile(path):
        return out
    try:
        proc = subprocess.run(
            [FFMPEG_BIN, "-hide_banner", "-i", path],
            capture_output=True, text=True, timeout=20, encoding="utf-8", errors="ignore",
            creationflags=_NO_WINDOW)
        text = proc.stderr or ""
        m = _DURATION_RE.search(text)
        if m:
            out["duration"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + \
                              float("0." + (m.group(4) or "0"))
        res = re.search(r"Video:.*?\s(\d{2,5})x(\d{2,5})", text)
        if res:
            out["width"], out["height"] = int(res.group(1)), int(res.group(2))
        # 元数据块：artist / title / copyright 等
        tags = {}
        for m2 in _META_RE.finditer(text):
            tags[m2.group(1).lower()] = m2.group(2).strip()
        for key in ("artist", "author", "creator", "byline", "copyright", "composer"):
            if key in tags and tags[key] and tags[key] != "0":
                out["artist"] = tags[key]
                break
        if "title" in tags and tags["title"]:
            out["title"] = tags["title"]
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return out


def _image_meta(path: str) -> dict:
    out = {}
    try:
        with Image.open(path) as im:
            out["width"], out["height"] = im.size
            try:
                exif = im.getexif()
                for tag in EXIF_ARTIST_TAGS:
                    v = exif.get(tag)
                    if isinstance(v, str) and v.strip() and v.strip() not in ("0", ""):
                        out["artist"] = v.strip()
                        break
                # Windows XPAuthor（中文作者常见载体）
                if "artist" not in out:
                    xp = _decode_xp(exif.get(XP_AUTHOR_TAG))
                    if xp and xp != "0":
                        out["artist"] = xp
            except Exception:
                pass
            # XMP: dc:creator（需 defusedxml，缺失则跳过）
            if "artist" not in out:
                try:
                    import defusedxml  # noqa: F401
                    xmp = im.getxmp()
                    for creator in xmp.get("xmpmeta", {}).get("RDF", {}).get("Description", []):
                        c = creator.get("creator") or creator.get("Creator")
                        if c:
                            out["artist"] = (c if isinstance(c, str) else c[0]).strip()
                            break
                except Exception:
                    pass
    except (OSError, Image.UnidentifiedImageError):
        pass
    return out


def extract_meta(path: str, kind: str) -> dict:
    """返回 width/height/duration/artist/title（尽力而为，绝不抛异常）。"""
    try:
        if kind == "video":
            return _ffmpeg_meta(path)
        return _image_meta(path)
    except Exception:
        return {}
