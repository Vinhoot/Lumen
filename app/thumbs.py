"""缩略图生成与磁盘缓存（无 Qt 依赖，可独立测试）。

- 两个尺寸档：grid(320) / detail(960)，按需生成；
- 图片用 Pillow（含 EXIF 方向修正），视频用 ffmpeg 抽帧；
- 磁盘缓存按容量封顶，启动时清理最旧条目 → 控制磁盘与内存占用。
"""
import hashlib
import os
import threading
import time

from PIL import Image, ImageOps

from .metadata import FFMPEG_BIN
from .paths import dirs

GRID_SIZE = 320
DETAIL_SIZE = 960
CACHE_MAX_MB = 300
CACHE_MAX_FILES = 30000

# Windows: 子进程不创建控制台窗口（防止 ffmpeg 黑窗闪现）
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v",
              ".mpg", ".mpeg", ".ts", ".rmvb", ".3gp", ".asf"}

# 解码并发上限：缩略图请求可能瞬间上百个，无上限会让 CPU 抖动、滚动发卡
_GEN_SEM = threading.BoundedSemaphore(max(3, min(8, (os.cpu_count() or 4))))
_inflight: dict[str, threading.Event] = {}
_inflight_lock = threading.Lock()


def thumb_path(path: str, mtime: float, size: int) -> str:
    key = hashlib.md5(f"{os.path.normcase(path)}|{mtime:.3f}|{size}".encode("utf-8", "ignore")).hexdigest()
    return os.path.join(dirs()["thumbs"], f"{key}.jpg")


def _gen_image(src: str, dst: str, size: int) -> bool:
    try:
        with Image.open(src) as im:
            # JPEG draft：按缩小比例解码，大图缩略图生成可快数倍
            try:
                if im.format == "JPEG" and size < 1024:
                    im.draft("RGB", (size * 4, size * 4))
            except Exception:
                pass
            im = ImageOps.exif_transpose(im)
            if getattr(im, "n_frames", 1) > 1:  # GIF 等动图取首帧
                im.seek(0)
            im.thumbnail((size, size))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            im.save(dst, "JPEG", quality=80, optimize=True)
        return True
    except Exception:
        return False


def _gen_video(src: str, dst: str, size: int) -> bool:
    if not FFMPEG_BIN:
        return False
    import subprocess
    try:
        # 取 1 秒处（不足则 0.1 秒）抽一帧，短边缩放到 size；-f image2 显式指定输出格式（dst 可能为 .tmp）
        cmd = [FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-ss", "1", "-i", src,
               "-frames:v", "1", "-vf", f"scale='min({size},iw)':-2", "-q:v", "4",
               "-f", "image2", "-y", dst]
        r = subprocess.run(cmd, capture_output=True, timeout=15, creationflags=_NO_WINDOW)
        if r.returncode != 0 or not os.path.isfile(dst):
            cmd[4] = "0.1"
            subprocess.run(cmd, capture_output=True, timeout=15, creationflags=_NO_WINDOW)
        return os.path.isfile(dst) and os.path.getsize(dst) > 0
    except Exception:
        return False


def generate(path: str, mtime: float, kind: str, size: int = GRID_SIZE) -> str | None:
    """生成缩略图，返回缓存文件路径；失败返回 None。

    - 同一张图被并发请求时只解码一次，其余等待复用结果（滚动时不会重复解码）；
    - 全局信号量限制同时解码数，避免上百个请求把 CPU 打满。
    """
    dst = thumb_path(path, mtime, size)
    if os.path.isfile(dst):
        try:
            os.utime(dst, (time.time(), time.time()))  # 命中即刷访问时间（LRU 依据）
        except OSError:
            pass
        return dst

    with _inflight_lock:
        ev = _inflight.get(dst)
        owner = ev is None
        if owner:
            ev = threading.Event()
            _inflight[dst] = ev
    if not owner:
        ev.wait(30)                       # 等先到的那位解码完，直接复用
        return dst if os.path.isfile(dst) else None

    tmp = dst + ".tmp"
    try:
        with _GEN_SEM:
            if kind == "video":
                ok = _gen_video(path, tmp, size)
            else:
                ok = _gen_image(path, tmp, size)
            if ok:
                try:
                    os.replace(tmp, dst)
                except OSError:
                    ok = False
        return dst if ok else None
    finally:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
        with _inflight_lock:
            _inflight.pop(dst, None)
        ev.set()


def cleanup_cache(max_mb: int = CACHE_MAX_MB, max_files: int = CACHE_MAX_FILES):
    """按容量/数量封顶，删除最久未访问的缩略图。"""
    td = dirs()["thumbs"]
    try:
        entries = []
        total = 0
        for name in os.listdir(td):
            p = os.path.join(td, name)
            if not name.endswith(".jpg"):
                continue
            try:
                st = os.stat(p)
            except OSError:
                continue
            entries.append((st.st_atime, st.st_size, p))
            total += st.st_size
        if len(entries) <= max_files and total <= max_mb * 1024 * 1024:
            return
        entries.sort()  # 最旧在前
        while (len(entries) > max_files or total > max_mb * 1024 * 1024) and entries:
            _, sz, p = entries.pop(0)
            try:
                os.remove(p)
                total -= sz
            except OSError:
                pass
    except OSError:
        pass
