"""扫描器：手动添加 + 目录扫描 + 增量更新 + 失效清理。

- 指纹 = size+mtime 摘要，不读内容，扫描快、占用低；
- 元数据提取多线程并行（图片 PIL / 视频 ffmpeg），写库单事务批量落盘；
- 回调进度（节流），支持暂停/停止；停止时已提取的文件仍然入库。
"""
import hashlib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import authoring
from .db import Database, IMAGE_EXTS, VIDEO_EXTS
from .metadata import extract_meta

PALETTE = ["#e6194b", "#3cb44b", "#ffe119", "#4363d8", "#f58231", "#911eb4",
           "#46f0f0", "#f032e6", "#bcf60c", "#fabebe", "#008080", "#e6beff",
           "#9a6324", "#fffac8", "#800000", "#aaffc3", "#808000", "#ffd8b1",
           "#000075", "#808080", "#5f4b8b", "#00a8a8"]

CONFIDENT_THRESHOLD = 0.80
BATCH_FLUSH = 400          # 每攒多少条记录批量落库一次
PROGRESS_EVERY = 25        # 进度回调节流：每 N 个文件通知一次 UI（UI 侧还有时间节流）
SCAN_WORKERS = min(16, max(8, (os.cpu_count() or 8)))

# 素材过滤：软件美术资源/图标等小图不入库（手动添加的文件不受此限制）
MIN_IMAGE_DIM = 224        # 最大边低于此像素 → 视为图标素材
MIN_IMAGE_BYTES = 20 * 1024  # 且体积小于 20KB → 基本可判定为非照片素材


def is_asset_image(kind: str, size: int, meta: dict) -> bool:
    """判定是否为小尺寸素材图（图标/UI 切图等），扫描目录时跳过。"""
    if kind != "image":
        return False
    w = meta.get("width") or 0
    h = meta.get("height") or 0
    m = max(w, h)
    if m and m < MIN_IMAGE_DIM:
        return True
    if size < MIN_IMAGE_BYTES and (not m or m < 512):
        return True
    return False


def ext_kind(path: str) -> str | None:
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    return None


def fingerprint(path: str, size: int, mtime: float) -> str:
    return hashlib.md5(f"{os.path.normcase(path)}|{size}|{mtime:.3f}".encode("utf-8", "ignore")).hexdigest()


class ScanCancelled(Exception):
    pass


class Scanner:
    def __init__(self, db: Database):
        self.db = db
        self.cancelled = False
        self._pause = threading.Event()
        self._pause.set()

    # ---------------- 暂停 / 停止 ----------------
    def cancel(self):
        """请求停止：暂停状态下也会立即退出等待。"""
        self.cancelled = True
        self._pause.set()

    def pause(self):
        self._pause.clear()

    def resume(self):
        self._pause.set()

    @property
    def paused(self) -> bool:
        return not self._pause.is_set()

    def _check_controls(self):
        self._pause.wait()
        if self.cancelled:
            raise ScanCancelled()

    # ---------------- 目录枚举 ----------------
    def walk_roots(self, roots, skip_hidden=True, skip_sysdirs=True):
        """roots: [(path, recursive)] → 产出媒体文件路径列表。支持暂停/停止。"""
        found = []
        seen_dirs = set()
        for root, recursive in roots:
            root = os.path.normpath(root)
            if not os.path.isdir(root):
                continue
            stack = [root]
            while stack:
                self._check_controls()
                d = stack.pop()
                if d in seen_dirs:
                    continue
                seen_dirs.add(d)
                try:
                    with os.scandir(d) as it:
                        for entry in it:
                            self._check_controls()
                            try:
                                if entry.is_dir(follow_symlinks=False):
                                    if recursive:
                                        if skip_hidden and entry.name.startswith("."):
                                            continue
                                        if skip_sysdirs and entry.name.lower() in {
                                                "$recycle.bin", "system volume information", "node_modules",
                                                "__pycache__", "windows", "program files", "programdata",
                                                ".git", "appdata"}:
                                            continue
                                        stack.append(entry.path)
                                elif entry.is_file(follow_symlinks=False):
                                    if ext_kind(entry.name):
                                        found.append(entry.path)
                            except OSError:
                                continue
                except OSError:
                    continue
        return found

    def collect_candidates(self, paths):
        """手动添加的文件：过滤扩展名并去重。"""
        out = []
        seen = set()
        for p in paths:
            p = os.path.normpath(p)
            if p in seen:
                continue
            seen.add(p)
            if os.path.isfile(p) and ext_kind(p):
                out.append(p)
        return out

    # ---------------- 主流程 ----------------
    def _prepare_file(self, path, existing, missing_set, force, dir_ctx_cache,
                      apply_asset_filter=True):
        """并行阶段：stat + 指纹 + 元数据 + 作者推断 → 记录 dict（无 DB 操作）。"""
        if self.cancelled:
            return None
        try:
            st = os.stat(path)
        except OSError:
            return None
        fp = fingerprint(path, st.st_size, st.st_mtime)
        was_missing = path in missing_set
        if not force and existing.get(path) == fp and not was_missing:
            return None
        kind = ext_kind(path)
        meta = extract_meta(path, kind)
        if apply_asset_filter and is_asset_image(kind, st.st_size, meta):
            return None  # 小尺寸素材图（图标/UI 资源）不入库
        d = os.path.dirname(path)
        ctx = dir_ctx_cache.get(d)
        if ctx is None:
            ctx = authoring.prepare_dir_ctx(d)
            dir_ctx_cache[d] = ctx
        author_id, confidence, candidate = authoring.infer_author(path, meta.get("artist"), ctx)
        title = meta.get("title") or authoring.clean_title(path, None)
        return {
            "path": path, "folder": d, "kind": kind,
            "ext": os.path.splitext(path)[1].lower(),
            "size": st.st_size, "mtime": st.st_mtime,
            "width": meta.get("width"), "height": meta.get("height"),
            "duration": meta.get("duration"), "fingerprint": fp,
            "author_id": None, "author_confidence": confidence,
            "author_candidate": candidate, "title": title,
            "last_seen": time.time(),
            "_new": (path not in existing and not was_missing),
        }

    def scan(self, roots, manual_files=None, progress=None, force=False):
        """roots: [(path, recursive)]；manual_files: 手动添加的路径列表。
        返回统计 dict；被停止时返回 partial + cancelled=True。"""
        self.cancelled = False
        self._pause.set()
        added = updated = uncertain = missing = 0
        manual_files = manual_files or []
        records: list = []
        ex = ThreadPoolExecutor(max_workers=SCAN_WORKERS)

        def flush():
            nonlocal added, updated
            if records:
                a, u = self.db.bulk_upsert_files(records)
                added += a
                updated += u
                records.clear()

        try:
            all_paths = list(self.walk_roots(roots))
            manual_set = set(self.collect_candidates(manual_files))
            all_paths += list(manual_set)

            # 去重，保留顺序
            uniq, seen = [], set()
            for p in all_paths:
                if p not in seen:
                    seen.add(p)
                    uniq.append(p)

            existing = self.db.existing_fingerprints()
            missing_set = self.db.missing_paths()
            authors = self.db.authors_map()
            total = len(uniq)
            dir_ctx_cache: dict = {}

            # 并行元数据提取（图片/视频互不阻塞），主线程负责节流进度与批量落库
            # 手动添加的文件不做素材过滤（尊重用户明确意图）
            # 分块提交：5 万文件不会一次性创建 5 万个 Future（内存平稳）
            completed = 0
            CHUNK = 1500
            for base in range(0, total, CHUNK):
                chunk = uniq[base:base + CHUNK]
                futs = {ex.submit(self._prepare_file, p, existing, missing_set, force,
                                  dir_ctx_cache, p not in manual_set): p for p in chunk}
                for fut in as_completed(futs):
                    self._check_controls()
                    path = futs[fut]
                    rec = fut.result()
                    completed += 1
                    if rec is not None:
                        if rec["author_confidence"] >= CONFIDENT_THRESHOLD:
                            name = rec["author_candidate"]
                            aid = authors.get(name)
                            if aid is None:
                                color = PALETTE[abs(hash(name)) % len(PALETTE)]
                                aid = self.db.author_id_by_name(name, color)
                                authors[name] = aid
                            rec["author_id"] = aid
                            rec["author_candidate"] = ""
                        elif rec["author_candidate"]:
                            uncertain += 1
                        records.append(rec)
                        if len(records) >= BATCH_FLUSH:
                            flush()
                    if progress and (completed % PROGRESS_EVERY == 0 or completed == total):
                        progress(completed, total, path)
            flush()

            # 失效检测：本次扫描根目录下，库中已索引但已不存在的路径
            roots_paths = [os.path.normpath(r) for r, _ in roots]
            if roots_paths:
                seen_gone = set()
                gone = []
                for r in roots_paths:
                    for p in self.db.paths_under(r):
                        if p in seen_gone:
                            continue
                        seen_gone.add(p)
                        if not os.path.exists(p):
                            gone.append(p)
                if gone:
                    self.db.mark_missing(gone)
                    missing = len(gone)

            ex.shutdown(wait=False)
            return {"added": added, "updated": updated, "uncertain": uncertain, "missing": missing}
        except ScanCancelled:
            flush()  # 已提取完成的文件仍然入库
            ex.shutdown(wait=False, cancel_futures=True)
            return {"added": added, "updated": updated, "uncertain": uncertain,
                    "missing": 0, "cancelled": True}

    # ---------------- 增量快速检查（启动时用） ----------------
    def quick_check_new(self):
        """只对比指纹，不提取元数据：返回疑似新文件数（供“发现新文件”提示）。"""
        roots = self.db.list_watch_folders()
        n = 0
        existing = self.db.existing_fingerprints()
        for path in self.walk_roots([(r["path"], r["recursive"]) for r in roots]):
            if self.cancelled:
                break
            try:
                st = os.stat(path)
            except OSError:
                continue
            if existing.get(path) != fingerprint(path, st.st_size, st.st_mtime):
                n += 1
        return n
