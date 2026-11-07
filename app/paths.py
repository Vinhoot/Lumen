"""应用数据目录管理（数据库、缩略图缓存、日志）。

打包/源码两种运行方式都兼容：
- 源码运行：数据放在项目根目录 data\\（便携）
- 打包为 exe：数据放在 exe 同级目录的 data\\（便携，卸载即删）
若目标目录不可写，则回退到 %APPDATA%\\Lumen。
"""
import os
import sys


def _base_dir() -> str:
    """可写基准目录：exe 所在目录（打包）或项目根目录（源码）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def app_data_dir() -> str:
    """应用数据目录，优先跟随便携目录，其次用户 AppData。"""
    override = os.environ.get("MM_DATA_DIR")  # 诊断/多库切换用
    if override:
        os.makedirs(override, exist_ok=True)
        return override
    base = _base_dir()
    portable = os.path.join(base, "data")
    try:
        os.makedirs(portable, exist_ok=True)
        probe = os.path.join(portable, ".writable")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("1")
        os.remove(probe)
        return portable
    except OSError:
        fallback = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "Lumen")
        os.makedirs(fallback, exist_ok=True)
        return fallback


def ensure_dirs() -> dict:
    data = app_data_dir()
    thumbs = os.path.join(data, "thumbs")
    os.makedirs(thumbs, exist_ok=True)
    return {
        "data": data,
        "thumbs": thumbs,
        "db": os.path.join(data, "mediavault.db"),
        "log": os.path.join(data, "mediavault.log"),
    }


DIRS = None


def dirs() -> dict:
    global DIRS
    if DIRS is None:
        DIRS = ensure_dirs()
    return DIRS


def asset_path(rel: str) -> str:
    """定位随程序分发的静态资源（打包后位于 _MEIPASS / _internal）。

    兼容两种写法：rel 相对项目根（如 "app/web"）或相对 app 包（如 "web"）。
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        cands = [os.path.join(base, rel)]
    else:
        app_dir = os.path.dirname(os.path.abspath(__file__))
        root = os.path.dirname(app_dir)
        cands = [os.path.join(app_dir, rel), os.path.join(root, rel)]
    for c in cands:
        if os.path.exists(c):
            return c
    return cands[0]
