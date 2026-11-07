"""作者识别与名字清洗。

识别信号（按强度排序）：
1. 文件内嵌元数据：EXIF Artist / IPTC By-line / XMP creator / 视频 artist 字段
2. 同目录 sidecar 文件：author.txt、作者.txt、artist.txt 等
3. 所在文件夹名（下载资源的文件夹名往往是作者/来源名）
4. 文件名前缀：[作者] 标题.jpg / (作者) 标题.jpg / 作者 - 标题.jpg

多信号归一化后互相印证可提升置信度；冲突或证据不足 → "待确认"，由用户一键归属。
"""
import os
import re
import unicodedata

GENERIC_DIR_NAMES = {
    "downloads", "download", "下载", "pictures", "图片", "images", "image", "photos", "photo",
    "照片", "videos", "video", "视频", "desktop", "桌面", "new folder", "新建文件夹", "未命名",
    "unnamed", "temp", "tmp", "临时", "wallpaper", "wallpapers", "壁纸", "camera", "相机",
    "wechat", "微信", "qq", "screenshot", "screenshots", "截图", "相册", "album", "albums",
    "素材", "assets", "movies", "电影", "media", "gallery", "图库", "收藏", "favorites",
    "received", "接收", "inbox", "资料", "files", "文档", "documents", "workspace", "工作",
    "c", "d", "e", "f", "root", "home", "users", "public", "库", "library",
    # 常见无意义短目录
    "sub", "misc", "img", "pic", "pics", "new", "old", "test", "data", "output", "out",
    "src", "res", "raw", "jpg", "png", "1", "2", "3", "movie", "video_1",
}

# 文件夹名里的常见噪音后缀，需剥离后才是作者名
NOISE_PATTERNS = [
    r"第[一二三四五六七八九十百千\d]+[期季弹话章集]",
    r"part\.?\s*\d+", r"vol\.?\s*\d+", r"set\.?\s*\d+", r"no\.?\s*\d+", r"ep\.?\s*\d+",
    r"ch\.?\s*\d+", r"p\.?\s*\d+", r"合集", r"系列", r"更新", r"作品集", r"图包",
    r"\(\d+[p张]?\)", r"\[\d+[p张]?\]", r"\d+[p张]$", r"\d{3,}",
    r"(hd|fhd|uhd|4k|2k|8k)$", r"high\s*res", r"完整版", r"全彩", r"中文", r"汉化", r"无修",
]

SIDECAR_NAMES = {"author.txt", "_author.txt", "作者.txt", "artist.txt", "by.txt",
                 "creator.txt", "!author.txt", "credit.txt"}

BRACKET_RE = re.compile(r"^[\[【\(（]([^\]】\)）]+)[\]】\)）]\s*")
DASH_RE = re.compile(r"^(.+?)\s*[-–—|·]\s*.+$")


def normalize(name: str) -> str:
    """归一化用于比较：小写、去重音、去所有分隔符。"""
    s = unicodedata.normalize("NFKD", name).lower()
    s = re.sub(r"[\s\-_.,·•:：|\[\]【】()（）{}]+", "", s)
    return s


def clean_folder_name(folder: str) -> str | None:
    """剥离噪音，返回可能的作者名；generic 目录返回 None。"""
    name = os.path.basename(folder.rstrip("\\/")).strip()
    if not name:
        return None
    key = normalize(name)
    if key in GENERIC_DIR_NAMES or name.lower() in GENERIC_DIR_NAMES:
        return None
    cleaned = name
    for pat in NOISE_PATTERNS:
        cleaned = re.sub(pat, "", cleaned, flags=re.I)
    cleaned = re.sub(r"[\[\]【】()（）{}【】]", " ", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -_—–·,，")
    if len(cleaned) < 2 or len(cleaned) > 40:
        return None
    if normalize(cleaned) in GENERIC_DIR_NAMES:
        return None
    if re.fullmatch(r"[0-9\s\-_.]+", cleaned):
        return None
    return cleaned


def clean_title(filename: str, author: str | None = None) -> str:
    """从文件名提炼作品标题。"""
    base = os.path.splitext(os.path.basename(filename))[0]
    t = base
    if author:
        for pat in (re.escape(author) + r"\s*[-–—_|·]+\s*",
                    r"[\[【\(（]\s*" + re.escape(author) + r"\s*[\]】\)）]\s*"):
            t = re.sub(pat, "", t, flags=re.I)
    t = re.sub(r"[\[\]【】()（）{}]", " ", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" -_—–·")
    return t if t else base


def filename_prefix_author(filename: str) -> tuple[str | None, str]:
    """从 [作者] 标题 / (作者) 标题 / 作者 - 标题 提取作者。"""
    base = os.path.splitext(os.path.basename(filename))[0]
    m = BRACKET_RE.match(base)
    if m and 1 < len(m.group(1)) <= 40:
        return m.group(1).strip(), "filename-bracket"
    m = DASH_RE.match(base)
    if m:
        head = m.group(1).strip()
        if 1 < len(head) <= 40 and not re.fullmatch(r"[0-9\s]+", head):
            return head, "filename-dash"
    return None, ""


def read_sidecar_author(directory: str) -> str | None:
    """读取同目录 author.txt / 作者.txt 等 sidecar。"""
    try:
        for name in SIDECAR_NAMES:
            p = os.path.join(directory, name)
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8", errors="ignore") as fh:
                    text = fh.read(2000).strip()
                for line in text.splitlines():
                    line = line.strip().lstrip("#*•-").strip()
                    if line and len(line) <= 40:
                        return line
    except OSError:
        pass
    return None


def _folder_signal(directory: str) -> tuple[str | None, float]:
    """文件夹名作者信号。短目录（sub/img/movie 等）自动向上看一级（如 作者/sub/img.jpg）。"""
    parent_name = os.path.basename(directory.rstrip("\\/"))
    cleaned = clean_folder_name(directory)
    if cleaned and len(cleaned) > 4 and len(parent_name) > 5:
        return cleaned, 0.75
    if len(parent_name) <= 5:
        c2 = clean_folder_name(os.path.dirname(directory))
        if c2 and len(c2) > 4:
            return c2, 0.70
    return None, 0.0


def prepare_dir_ctx(directory: str) -> dict:
    """预计算目录级信号（sidecar + 文件夹名），供同目录多文件复用。"""
    return {"side": read_sidecar_author(directory),
            "folder": _folder_signal(directory)}


def infer_author(path: str, meta_author: str | None = None,
                 ctx: dict | None = None) -> tuple[int | None, float, str]:
    """返回 (author_id|None, confidence, candidate_display_name)。
    author_id 由调用方按名字落库；candidate 为可展示的原始名字。
    ctx 为 prepare_dir_ctx 的目录级缓存，同目录文件可复用。"""
    directory = os.path.dirname(path)
    filename = os.path.basename(path)
    # key(归一化) -> (置信度, 来源, 展示名)
    signals: dict[str, tuple[float, str, str]] = {}

    if meta_author and meta_author.strip():
        signals[normalize(meta_author)] = (0.95, "metadata", meta_author.strip())

    if ctx is None:
        ctx = prepare_dir_ctx(directory)
    side = ctx["side"]
    folder, folder_conf = ctx["folder"]

    if side:
        signals.setdefault(normalize(side), (0.90, "sidecar", side))
    if folder:
        signals.setdefault(normalize(folder), (folder_conf, "folder", folder))

    prefix, src = filename_prefix_author(filename)
    if prefix:
        key = normalize(prefix)
        conf = 0.60
        if folder and key == normalize(folder):
            conf = 0.85
        if key not in signals or signals[key][0] < conf:
            signals[key] = (conf, src, prefix)

    if not signals:
        return None, 0.0, ""

    ranked = sorted(signals.items(), key=lambda kv: -kv[1][0])
    (top_key, (top_conf, top_src, top_display)) = ranked[0]

    # 权威信号（内嵌元数据/sidecar，置信 ≥0.90）压过其他信号；否则检测冲突
    second_conf = ranked[1][1][0] if len(ranked) > 1 else 0.0
    conflict = top_conf < 0.90 and (top_conf - second_conf) < 0.25

    if conflict or top_conf < 0.80:
        # 待确认：给出疑似候选展示名，不落库作者
        return None, min(top_conf, 0.79), top_display

    return None, min(top_conf, 1.0), top_display  # author_id 由调用方 name->id 解析
