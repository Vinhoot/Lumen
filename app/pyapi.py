# -*- coding: utf-8 -*-
"""pywebview JS 桥：新版 Liquid Glass 前端的全部后端能力。

- 复用既有核心：Database / Scanner / thumbs / backup（每工作区独立库）；
- 扫描在后台线程执行，进度通过 window.evaluate_js 推送 'scan-progress' 事件；
- 数据变化推送 'data-changed' 事件，前端收到后自行刷新。
"""
import ctypes
import hashlib
import json
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes

from . import backup as backup_mod
from . import thumbs
from .db import Database
from .scanner import Scanner
from .workspace import WorkspaceManager

_scan_lock = threading.Lock()
_thumb_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="thumb")


def _folder_id(path: str) -> str:
    """文件夹分组的不透明 id（md5 前 12 位，前端只用于回传，不参与运算）。"""
    return hashlib.md5(os.path.normcase(path).encode("utf-8", "ignore")).hexdigest()[:12]


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def move_to_recycle_bin(paths) -> list:
    """把一批文件/目录移到 Windows 回收站（用户可自行还原）。返回已消失的路径。"""
    paths = [p for p in (paths or []) if p and os.path.exists(p)]
    if not paths:
        return []
    FO_DELETE = 3
    FOF_SILENT = 0x0004
    FOF_NOCONFIRMATION = 0x0010
    FOF_ALLOWUNDO = 0x0040
    FOF_NOERRORUI = 0x0400
    FOF_NOCONFIRMMKDIR = 0x0200
    flags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI | FOF_NOCONFIRMMKDIR
    gone = []
    for i in range(0, len(paths), 32):          # 分批，避免 pFrom 过长
        chunk = paths[i:i + 32]
        op = _SHFILEOPSTRUCTW()
        op.hwnd = None
        op.wFunc = FO_DELETE
        op.pFrom = "\0".join(chunk) + "\0\0"    # 双 null 结尾的路径列表
        op.pTo = None
        op.fFlags = flags
        try:
            ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        except Exception:
            pass
        for p in chunk:
            if not os.path.exists(p):
                gone.append(p)
    return gone


class PyApi:
    def __init__(self):
        self.wm = WorkspaceManager()
        self._window = None
        self._scanners: dict[int, Scanner] = {}
        self._scan_state = {"running": False, "phase": "", "done": 0,
                            "total": 0, "current": "", "newFiles": 0}
        self._maximized = False
        self._tag_cache = None      # (wsid, {tag_id: tag})，标签变更后置空
        # 缩略图请求走 HTTP，每个请求都是新线程：给 (wsid,fid)→文件信息 做进程内缓存，
        # 避免每张缩略图都新建一次 SQLite 连接（大网格滚动时的主要开销）
        self._file_cache: dict = {}
        # 兼容模式（WebView2 不可用、界面跑在 Edge 浏览器里）没有 evaluate_js 通道，
        # 事件先入队，由前端 pollEvents 定时取走后再派发同样的 CustomEvent。
        self._events: list = []
        self._events_lock = threading.Lock()
        # 兼容模式下的选取框走 PowerShell/WinForms；无人值守验证时用 MM_NO_DIALOG=1 关掉
        self._allow_dialogs = os.environ.get("MM_NO_DIALOG") != "1"

    # ================= 内部工具 =================
    def set_window(self, window):
        self._window = window

    def _push(self, event: str, detail=None):
        if self._window is None:
            # 兼容模式：入队等前端来取（只留最近 200 条，避免长时间无人取导致膨胀）
            with self._events_lock:
                self._events.append({"event": event, "detail": detail})
                if len(self._events) > 200:
                    del self._events[:len(self._events) - 200]
            return
        try:
            payload = json.dumps(detail, ensure_ascii=False)
            self._window.evaluate_js(
                f"window.dispatchEvent(new CustomEvent('{event}',{{detail:{payload}}}))")
        except Exception:
            pass

    def pollEvents(self):
        """兼容模式用：取走未派发的事件（原生窗口下永远为空）。"""
        with self._events_lock:
            out = self._events
            self._events = []
        return out

    def _changed(self):
        self._push("data-changed", {"ts": time.time()})

    def _db(self) -> Database:
        return self.wm.db()

    def _scanner(self) -> Scanner:
        wid = self.wm.current_id()
        sc = self._scanners.get(wid)
        if sc is None:
            sc = Scanner(self._db())
            self._scanners[wid] = sc
        return sc

    # ================= 启动 / 工作区 =================
    def workspaceCounts(self):
        """每个工作区的媒体数量（切换器上直接显示，避免「扫到别的库里」的困惑）。"""
        out = {}
        for w in self.wm.list_workspaces():
            try:
                n = self.wm.db(w["id"]).count_files()
            except Exception:
                n = 0
            out[str(w["id"])] = n
        return out

    def getBootstrap(self):
        return {
            "workspaces": self.wm.list_workspaces(),
            "currentWorkspaceId": self.wm.current_id(),
            "theme": self.wm.get_setting("theme", "dark"),
            "thumbBase": "/thumb",
            "groupBy": self.wm.get_setting("groupBy", "folder"),
            "dateGran": self.wm.get_setting("dateGran", "month"),
            "counts": self.workspaceCounts(),
        }

    def createWorkspace(self, name):
        ws = self.wm.create_workspace(name)
        self._changed()
        return ws

    def switchWorkspace(self, wid):
        self.wm.set_current(int(wid))
        self._tag_cache = None
        return {"ok": True}

    def renameWorkspace(self, wid, name):
        self.wm.rename_workspace(int(wid), name)
        self._changed()
        return {"ok": True}

    def deleteWorkspace(self, wid):
        self.wm.delete_workspace(int(wid))
        cur = self.wm.current_id()
        self._changed()
        return {"ok": True, "currentWorkspaceId": cur}

    # ================= 侧栏 =================
    def _folder_tree(self, db):
        """把「有文件的目录」补全为完整层级树（父目录聚合子目录数量，可点击看全部）。"""
        roots = [os.path.normpath(w["path"]) for w in db.list_watch_folders()]
        merged: dict[str, dict] = {}

        def bump(path, own):
            chain = [path]
            while True:
                parent = os.path.dirname(chain[-1])
                if not parent or parent == chain[-1]:
                    break
                if not any(parent == r or parent.startswith(r + os.sep) for r in roots):
                    break
                chain.append(parent)
            for i, q in enumerate(chain):
                cur = merged.get(q)
                if cur is None:
                    merged[q] = cur = {"own": 0, "count": 0}
                cur["count"] += own
                if i == 0:
                    cur["own"] += own

        for p, c in db.list_folders():
            bump(os.path.normpath(p), c)

        out = []
        for p, v in merged.items():
            base = None
            for r in roots:
                if (p == r or p.startswith(r + os.sep)) and (base is None or len(r) > len(base)):
                    base = r
            if base is None:
                depth = 0
            else:
                rel = os.path.relpath(p, base)
                depth = 0 if rel == "." else rel.count(os.sep) + 1
            parent = os.path.dirname(p)
            out.append({"id": _folder_id(p), "path": p,
                        "name": os.path.basename(p) or p,
                        "parent": parent if parent in merged and parent != p else None,
                        "depth": depth, "own": v["own"], "count": v["count"]})
        out.sort(key=lambda x: (x["path"].lower()))
        children = {f["parent"] for f in out if f["parent"]}
        for f in out:
            f["hasChildren"] = f["path"] in children
        return out

    def getSidebar(self):
        db = self._db()
        st = db.stats()
        folders = self._folder_tree(db)
        authors = [{"id": a["id"], "name": a["name"], "count": a.get("cnt", 0),
                    "color": a.get("color", "")}
                   for a in db.list_authors() if a.get("cnt", 0) > 0]
        tags = [{"id": t["id"], "name": t["name"],
                 "color": t.get("color") or "#0A84FF", "count": t.get("cnt", 0)}
                for t in db.list_tags()]
        with db.conn() as c:
            untagged = c.execute(
                "SELECT COUNT(*) FROM files f WHERE f.missing=0 AND NOT EXISTS("
                "SELECT 1 FROM file_tags ft WHERE ft.file_id=f.id)").fetchone()[0]
        return {
            "folders": folders, "authors": authors, "tags": tags,
            "counts": {
                "all": st["total"],
                "images": st.get("image", (0, 0))[0],
                "videos": st.get("video", (0, 0))[0],
                "untagged": untagged,
                "unknownAuthor": st["unclassified"] + st["uncertain"],
            },
        }

    # ================= 媒体查询（分页 + SQL 层分组：5 万级库也能秒开） =================
    PAGE_SIZE = 300

    def _tag_objs(self) -> dict:
        """id → 标签 缓存（标签名/颜色很少变，避免每次查询都全表取一遍）。"""
        wid = self.wm.current_id()
        cache = getattr(self, "_tag_cache", None)
        if cache and cache[0] == wid:
            return cache[1]
        objs = {t["id"]: t for t in self._db().list_tags()}
        self._tag_cache = (wid, objs)
        return objs

    @staticmethod
    def _group_title(group_by, date_gran, key):
        """分组键 → (标题, 副标题)。"""
        if group_by == "date":
            try:
                if date_gran == "year":
                    return "%s年" % key, ""
                parts = [int(x) for x in str(key).split("-")]
                if date_gran == "day" and len(parts) >= 3:
                    return "%d年%d月%d日" % (parts[0], parts[1], parts[2]), ""
                if len(parts) >= 2:
                    return "%d年%d月" % (parts[0], parts[1]), ""
            except Exception:
                pass
            return str(key), ""
        if group_by == "none":
            return "全部媒体", ""
        if group_by == "type":
            return {"image": "图片", "video": "视频"}.get(key, "其他"), ""
        if group_by == "author":
            return (key or "未识别作者"), ""
        return (os.path.basename(key) or key), key

    def queryMedia(self, filters, offset=0, limit=None):
        """取一页媒体（按分组键连续排列）。

        - 所有筛选（含按作者/标签名搜索、无标签）都下推到 SQL，不再"取 5000 条内存过滤"；
        - 首屏附带每个分组的真实总数，组标题显示全量；
        - offset/limit 供前端滚动续取，DOM 不会一次性撑爆。
        """
        filters = filters or {}
        db = self._db()
        kind = filters.get("type") if filters.get("type") in ("image", "video") else None
        special = filters.get("special")
        folder_path = (filters.get("folderPath") or "").strip() or None
        folder_id = filters.get("folderId")
        if not folder_path and folder_id:
            for p, _c in db.list_folders():
                if _folder_id(p) == folder_id:
                    folder_path = p
                    break
        tag_ids = [int(t) for t in (filters.get("tagIds") or [])]
        search = (filters.get("search") or "").strip() or None
        search_field = filters.get("searchField") or "name"
        if search_field not in ("name", "author", "tag"):
            search_field = "name"
        author_id = filters.get("authorId")
        author_id = int(author_id) if author_id else None
        group_by = filters.get("groupBy") or "folder"
        if group_by not in ("folder", "date", "author", "type", "none"):
            group_by = "folder"
        date_gran = filters.get("dateGran") or "month"
        if date_gran not in ("year", "month", "day"):
            date_gran = "month"
        try:
            offset = max(0, int(offset or 0))
        except (TypeError, ValueError):
            offset = 0
        try:
            limit = int(limit or self.PAGE_SIZE)
        except (TypeError, ValueError):
            limit = self.PAGE_SIZE
        limit = max(50, min(1000, limit))

        q = dict(kind=kind, author_id=author_id, tag_ids=tag_ids, search=search,
                 search_field=search_field, folder=folder_path, folder_recursive=True,
                 group_by=group_by, date_gran=date_gran)
        if special == "unknownAuthor":
            q["no_author"] = True
        elif special == "untagged":
            q["untagged"] = True

        rows, total = db.query_page(sort="mtime", desc=True,
                                    limit=limit, offset=offset, **q)

        wsid = self.wm.current_id()
        tag_objs = self._tag_objs()
        ids = [r["id"] for r in rows]
        names = db.author_names(list({r["author_id"] for r in rows if r.get("author_id")}))
        tag_map = db.tag_map_for_files(ids)
        gcount = db.group_counts(**q) if offset == 0 else {}

        groups: list = []
        index: dict = {}
        for r in rows:
            key = r.get("gkey")
            key = "" if key is None else key
            g = index.get(key)
            if g is None:
                title, sub = self._group_title(group_by, date_gran, key)
                g = {"key": key, "folderId": _folder_id(key), "folderName": title,
                     "path": sub, "count": int(gcount.get(key, 0)), "items": []}
                index[key] = g
                groups.append(g)
            aid = r.get("author_id")
            # 预热缩略图用的文件信息缓存（有上限，超出丢最早的 5000 条）
            if len(self._file_cache) > 15000:
                for k in list(self._file_cache)[:5000]:
                    self._file_cache.pop(k, None)
            self._file_cache[(wsid, r["id"])] = (r["path"], r.get("mtime") or 0, r["kind"])
            g["items"].append({
                "id": r["id"],
                "name": os.path.basename(r["path"]),
                "path": r["path"],
                "type": r["kind"],
                "ext": r.get("ext", ""),
                "size": r.get("size", 0),
                "mtime": r.get("mtime", 0),
                "width": r.get("width") or 0,
                "height": r.get("height") or 0,
                "duration": r.get("duration") or 0,
                "authorId": aid,
                "authorName": names.get(aid, "") if aid else (r.get("author_candidate") or ""),
                "authorUncertain": bool(not aid and r.get("author_candidate")),
                "thumbUrl": f"/thumb/{wsid}/{r['id']}",
                "mediaUrl": f"/media/{wsid}/{r['id']}",
                "tags": [{"id": t, "name": tag_objs[t]["name"],
                          "color": tag_objs[t].get("color") or "#0A84FF"}
                         for t in tag_map.get(r["id"], []) if t in tag_objs],
            })
        for g in groups:
            if not g["count"]:
                g["count"] = len(g["items"])
        return {"groups": groups, "total": total,
                "offset": offset, "limit": limit,
                "hasMore": offset + len(rows) < total,
                "groupBy": group_by, "dateGran": date_gran}

    # ================= 扫描 =================
    def _ps_dialog(self, kind: str):
        """兼容模式（界面跑在浏览器里，没有 pywebview 窗口）的系统选择框。

        用 PowerShell + WinForms 弹原生对话框，输出按 UTF-8 取回，支持中文路径。
        失败一律返回 []，让调用方按"用户取消"处理。
        """
        head = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
                "Add-Type -AssemblyName System.Windows.Forms | Out-Null;")
        if kind == "folder":
            body = ("$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
                    "$d.Description='选择要扫描的文件夹';"
                    "$d.ShowNewFolderButton=$false;"
                    "if($d.ShowDialog() -eq 'OK'){Write-Output $d.SelectedPath}")
        else:
            body = ("$d=New-Object System.Windows.Forms.OpenFileDialog;"
                    "$d.Multiselect=$true;"
                    "$d.Filter='媒体文件|*.jpg;*.jpeg;*.png;*.gif;*.webp;*.bmp;*.mp4;"
                    "*.mkv;*.avi;*.mov;*.wmv;*.flv;*.webm;*.m4v|所有文件|*.*';"
                    "if($d.ShowDialog() -eq 'OK'){$d.FileNames | ForEach-Object {Write-Output $_}}")
        try:
            import subprocess
            r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", head + body],
                               capture_output=True, timeout=600)
            out = r.stdout.decode("utf-8", "replace").replace("\r", "").strip()
            return [os.path.normpath(p) for p in out.split("\n") if p.strip()]
        except Exception:
            return []

    def _pick_folder(self, title=None):
        """弹系统文件夹选择器（修复：必须传合法 directory，否则 .NET 8 参数错误）。"""
        if self._window is None:
            got = self._ps_dialog("folder")
            return got[0] if got else None
        import webview as _wv
        home = os.path.expanduser("~")
        if not os.path.isdir(home):
            home = os.environ.get("SystemDrive", "C:") + os.sep
        res = self._window.create_file_dialog(
            _wv.FOLDER_DIALOG, directory=home, allow_multiple=False)
        paths = list(res or [])
        return os.path.normpath(paths[0]) if paths else None

    def pickAndScan(self, mode):
        if self._window is None and not self._allow_dialogs:
            return {"started": False, "reason": "no-dialog"}
        try:
            import webview as _wv
            if mode == "folder":
                folder = self._pick_folder()
                if not folder:
                    return {"started": False}
                db = self._db()
                db.add_folder(folder, recursive=True)
                if not self._start_scan(roots=[(folder, True)]):
                    return {"started": False, "reason": "busy"}
                return {"started": True}
            if self._window is None:
                paths = self._ps_dialog("files")
            else:
                res = self._window.create_file_dialog(
                    _wv.OPEN_DIALOG, allow_multiple=True,
                    file_types=("媒体文件 (*.jpg;*.jpeg;*.png;*.gif;*.webp;*.bmp;*.mp4;"
                                "*.mkv;*.avi;*.mov;*.wmv;*.flv;*.webm;*.m4v)", "所有文件 (*.*)"))
                paths = list(res or [])
            if not paths:
                return {"started": False}
            if not self._start_scan(manual_files=paths):
                return {"started": False, "reason": "busy"}
            return {"started": True}
        except Exception:
            return {"started": False, "error": traceback.format_exc(limit=3)}

    # ---------- 扫描源管理（一键重扫可指定一个或多个目录） ----------
    def listSources(self):
        """当前工作区记住的扫描目录：含每个目录已入库的文件数。"""
        db = self._db()
        out = []
        with db.conn() as c:
            for r in db.list_watch_folders():
                p = os.path.normpath(r["path"])
                if r["recursive"]:
                    like = (p.replace("!", "!!").replace("%", "!%")
                            .replace("_", "!_") + os.sep + "%")
                    cnt = c.execute(
                        "SELECT COUNT(*) FROM files WHERE missing=0 AND "
                        "(folder=? OR folder LIKE ? ESCAPE '!')",
                        (p, like)).fetchone()[0]
                else:
                    cnt = c.execute(
                        "SELECT COUNT(*) FROM files WHERE missing=0 AND folder=?",
                        (p,)).fetchone()[0]
                out.append({"id": r["id"], "path": r["path"],
                            "recursive": bool(r["recursive"]), "count": cnt})
        return {"sources": out}

    def addSource(self):
        """弹文件夹选择器并加入扫描源（不立即扫描，由前端决定是否触发）。"""
        folder = self._pick_folder()
        if not folder:
            return {"added": False}
        self._db().add_folder(folder, recursive=True)
        self._changed()
        return {"added": True, "path": folder}

    def addSourceAndScan(self):
        """弹文件夹选择器 → 加入扫描源 → 立即扫描该目录。"""
        folder = self._pick_folder()
        if not folder:
            return {"started": False}
        self._db().add_folder(folder, recursive=True)
        started = self._start_scan(roots=[(folder, True)])
        self._changed()
        return {"started": bool(started), "path": folder,
                "reason": "" if started else "busy"}

    def removeSource(self, source_id):
        """移除扫描源（保留已入库的文件记录）。"""
        self._db().remove_folder(int(source_id))
        self._changed()
        return {"ok": True}

    def rescan(self, paths=None):
        """重扫：不传 paths 扫全部扫描源；传列表则只扫指定目录。"""
        db = self._db()
        watches = db.list_watch_folders()
        if paths:
            sel = {os.path.normpath(str(p)) for p in paths}
            roots = [(r["path"], bool(r["recursive"])) for r in watches
                     if os.path.normpath(r["path"]) in sel]
        else:
            roots = [(r["path"], bool(r["recursive"])) for r in watches]
        if not roots:
            return {"started": False, "reason": "no-sources"}
        if not self._start_scan(roots=roots):
            return {"started": False, "reason": "busy"}
        return {"started": True}

    def _start_scan(self, roots=None, manual_files=None):
        roots = roots or []
        manual_files = manual_files or []
        with _scan_lock:
            if self._scan_state["running"]:
                return False
            self._scan_state.update(running=True, phase="正在枚举文件…", done=0,
                                    total=0, current="", newFiles=0,
                                    wsId=self.wm.current_id(),
                                    wsName=self.wm.name_of(self.wm.current_id()))
            self._push("scan-progress", dict(self._scan_state))
            scanner = self._scanner()

            last_push = [0.0]

            def progress(i, total, path):
                self._scan_state.update(phase="正在扫描", done=i,
                                        total=total, current=path)
                now = time.time()
                # 时间节流（而不是每 N 个文件）：5 万文件也只推 ~7 次/秒
                if i == total or now - last_push[0] >= 0.15:
                    last_push[0] = now
                    self._push("scan-progress", dict(self._scan_state))

            def work():
                try:
                    result = scanner.scan(roots, manual_files, progress=progress)
                    self._scan_state.update(
                        running=False, phase="完成",
                        newFiles=int(result.get("added", 0)),
                        current="")
                    self._push("scan-progress", dict(self._scan_state))
                    self._changed()
                    self._pregen_thumbs()
                except Exception as e:
                    self._scan_state.update(running=False, phase=f"失败: {e}")
                    self._push("scan-progress", dict(self._scan_state))

            threading.Thread(target=work, daemon=True, name="scan").start()
            return True

    def getScanStatus(self):
        return dict(self._scan_state)

    def _pregen_thumbs(self, limit=1500):
        """扫描完成后后台预生成缩略图（命中缓存的自动跳过）。

        并发由 thumbs 内部信号量统一收紧，这里只负责投递；
        先补最近的 1500 条（用户最常看），其余走按需生成。
        """
        db = self._db()
        rows = db.query_files(sort="added", desc=True, limit=limit)
        for r in rows:
            dst = thumbs.thumb_path(r["path"], r["mtime"], thumbs.GRID_SIZE)
            if os.path.isfile(dst):
                continue

            def job(p=r["path"], mt=r["mtime"], k=r["kind"]):
                try:
                    thumbs.generate(p, mt, k, thumbs.GRID_SIZE)
                except Exception:
                    pass

            _thumb_pool.submit(job)

    # ================= 标签 =================
    def createTag(self, name, color):
        db = self._db()
        tid = db.create_tag((name or "").strip(), color or "#0A84FF")
        t = db.get_tag(tid)
        self._tag_cache = None
        self._changed()
        return {"id": tid, "name": t["name"], "color": t.get("color") or "#0A84FF"}

    def renameTag(self, tag_id, name, color):
        db = self._db()
        if name:
            db.rename_tag(int(tag_id), name.strip())
        if color:
            db.set_tag_color(int(tag_id), color)
        self._tag_cache = None
        self._changed()
        return {"ok": True}

    def deleteTag(self, tag_id):
        self._db().delete_tag(int(tag_id))
        self._tag_cache = None
        self._changed()
        return {"ok": True}

    def assignTags(self, fileIds, tagIds, mode="add"):
        """批量打/移除标签（分块写入，5 万级也不阻塞 UI）。"""
        db = self._db()
        ids = [int(i) for i in fileIds]
        tids = [int(t) for t in tagIds]
        if mode == "add":
            for i in range(0, len(ids), 2000):
                db.add_file_tags(ids[i:i + 2000], tids)
        else:
            for t in tids:
                for i in range(0, len(ids), 2000):
                    db.remove_file_tag(ids[i:i + 2000], t)
        self._changed()
        return {"ok": True}

    # ================= 作者 =================
    def createAuthor(self, name):
        db = self._db()
        aid = db.create_author((name or "").strip())
        self._changed()
        return {"id": aid, "name": (name or "").strip()}

    def assignAuthor(self, fileIds, authorId=None, newName=None):
        """批量设作者（分块，避免一次塞入过多参数）。"""
        db = self._db()
        ids = [int(i) for i in fileIds]
        aid = None
        if newName:
            aid = db.create_author(newName.strip())
        elif authorId is not None:
            aid = int(authorId)
        for i in range(0, len(ids), 2000):
            chunk = ids[i:i + 2000]
            if aid is None:
                db.clear_author(chunk)
            else:
                db.assign_author(chunk, aid, manual=True)
        self._changed()
        return {"ok": True}

    def renameAuthor(self, authorId, name):
        name = (name or "").strip()
        if not name:
            return {"ok": False}
        self._db().rename_author(int(authorId), name)
        self._changed()
        return {"ok": True}

    def deleteAuthor(self, authorId):
        """删除作者：其名下文件退回「未识别」（磁盘文件不动）。"""
        self._db().delete_author(int(authorId))
        self._changed()
        return {"ok": True}

    # ================= 文件操作 =================
    def openFile(self, fid):
        f = self._db().get_file(int(fid))
        if f and os.path.isfile(f["path"]):
            try:
                os.startfile(f["path"])  # noqa: S606
            except OSError:
                pass
        return {"ok": True}

    def revealFile(self, fid):
        f = self._db().get_file(int(fid))
        if f and os.path.isfile(f["path"]):
            import subprocess
            try:
                subprocess.Popen(["explorer", "/select,", os.path.normpath(f["path"])],
                                 creationflags=0x08000000)
            except OSError:
                pass
        return {"ok": True}

    def revealPath(self, path):
        """在资源管理器中打开文件夹（或定位文件）。"""
        p = os.path.normpath(str(path or ""))
        if not p or not os.path.exists(p):
            return {"ok": False}
        try:
            if os.path.isdir(p):
                os.startfile(p)  # noqa: S606
            else:
                import subprocess
                subprocess.Popen(["explorer", "/select,", p], creationflags=0x08000000)
        except OSError:
            return {"ok": False}
        return {"ok": True}

    # ---------- 删除（移到回收站 / 仅从库中移除） ----------
    def deleteFiles(self, fileIds, mode="trash"):
        """删除选中文件：mode='trash' 移到回收站并移出库；'unlist' 仅移出库（磁盘不动）。"""
        db = self._db()
        ids = [int(i) for i in (fileIds or [])]
        files = db.files_by_ids(ids)   # 一次批量取，避免逐条查询
        if not files:
            return {"ok": False, "trashed": 0, "removed": 0}
        if mode == "unlist":
            db.remove_files([f["id"] for f in files])
            self._changed()
            return {"ok": True, "trashed": 0, "removed": len(files), "failed": 0}
        gone_paths = move_to_recycle_bin([f["path"] for f in files])
        gone_ids = [f["id"] for f in files if f["path"] in gone_paths]
        if gone_ids:
            db.remove_files(gone_ids)
        self._changed()
        return {"ok": True, "trashed": len(gone_ids), "removed": len(gone_ids),
                "failed": len(files) - len(gone_ids)}

    def folderFileIds(self, folder_id=None, path=None):
        """某个文件夹（含子目录）下的全部已入库文件 id / 路径。"""
        db = self._db()
        if not path and folder_id:
            for p, _c in db.list_folders():
                if _folder_id(p) == folder_id:
                    path = p
                    break
        if not path:
            return [], ""
        p = os.path.normpath(str(path))
        like = (p.replace("!", "!!").replace("%", "!%").replace("_", "!_")
                + os.sep + "%")
        with db.conn() as c:
            rows = c.execute(
                "SELECT id, path FROM files WHERE folder=? OR folder LIKE ? ESCAPE '!'",
                (p, like)).fetchall()
        return [r["id"] for r in rows], p

    def deleteFolder(self, folder_id=None, path=None, mode="trash"):
        """删除整个文件夹：trash=把目录移到回收站并移出库；unlist=只移出库。"""
        db = self._db()
        ids, p = self.folderFileIds(folder_id, path)
        if not p:
            return {"ok": False, "removed": 0, "reason": "not-found"}
        if mode == "unlist":
            db.remove_files(ids)
            self._changed()
            return {"ok": True, "removed": len(ids), "trashed": 0, "path": p}
        if os.path.isdir(p):
            move_to_recycle_bin([p])
        # 目录移走后，其下所有记录都不再存在 → 一并清理；若目录还在则只清理已消失的文件
        if not os.path.exists(p):
            db.remove_files(ids)
            removed = len(ids)
        else:
            gone = []
            for i in ids:
                f = db.get_file(i)
                if f and not os.path.exists(f["path"]):
                    gone.append(i)
            db.remove_files(gone)
            removed = len(gone)
        # 该目录（及其子目录）不再作为扫描源
        for w in db.list_watch_folders():
            wp = os.path.normpath(w["path"])
            if wp == p or wp.startswith(p + os.sep):
                db.remove_folder(w["id"])
        self._changed()
        return {"ok": True, "removed": removed, "trashed": 1 if not os.path.exists(p) else 0,
                "path": p, "folderGone": not os.path.exists(p)}

    def getThumbFile(self, wsid, fid, size=None):
        """供 HTTP 层调用：生成/命中缩略图，返回文件路径或 None。"""
        key = (int(wsid), int(fid))
        hit = self._file_cache.get(key)
        if hit and os.path.isfile(hit[0]):
            return thumbs.generate(hit[0], hit[1], hit[2], size or thumbs.GRID_SIZE)
        db = self.wm.db(int(wsid))
        f = db.get_file(int(fid))
        if not f or not os.path.isfile(f["path"]):
            return None
        self._file_cache[key] = (f["path"], f["mtime"], f["kind"])
        return thumbs.generate(f["path"], f["mtime"], f["kind"],
                               size or thumbs.GRID_SIZE)

    def getMediaPath(self, wsid, fid):
        db = self.wm.db(int(wsid))
        f = db.get_file(int(fid))
        if f and os.path.isfile(f["path"]):
            return f["path"], f["kind"], f["ext"]
        return None, None, None

    # ================= 备份 =================
    def _all_backup_targets(self):
        """[(ws_id, data_dir, db_path), ...] + 元库（ws_id=0）。"""
        out = [(0, self.wm.data_dir, self.wm.meta_path)]
        for w in self.wm.list_workspaces():
            out.append((w["id"], self.wm.ws_dir(w["id"]), self.wm.db_path(w["id"])))
        return out

    @staticmethod
    def _backup_kind(base: str) -> str:
        """从备份文件名解析类型：manual / auto / before_restore / legacy(旧版无标签)。"""
        stem = base[len(backup_mod.PREFIX):-len(backup_mod.SUFFIX)]
        tail = stem.rsplit('_', 1)[-1]
        return tail if tail in ("manual", "auto", "before_restore") else "legacy"

    def listBackups(self):
        items = []
        for wid, data_dir, _dbp in self._all_backup_targets():
            if wid == 0:
                continue  # 全局元库备份只做存档，不在 UI 提供恢复入口
            ws_name = self.wm.name_of(wid)
            for p, s, t in backup_mod.list_backups(data_dir):
                base = os.path.basename(p)
                items.append({"name": f"{ws_name}::{base}",
                              "path": p, "size": s, "created": t,
                              "kind": self._backup_kind(base),
                              "createdText": time.strftime("%Y-%m-%d %H:%M:%S",
                                                           time.localtime(t))})
        items.sort(key=lambda x: x["created"], reverse=True)
        return items[:20]

    def backupNow(self):
        total = 0
        names = []
        for wid, data_dir, db_path in self._all_backup_targets():
            if not os.path.isfile(db_path):
                continue
            res = backup_mod.create_backup(data_dir, db_path, label="manual")
            if res:
                backup_mod.prune(data_dir, 5)
                total += res["size"]
                names.append(os.path.basename(res["path"]))
        return {"ok": bool(names), "name": names[-1] if names else "",
                "size": total}

    def restoreBackup(self, name):
        ws_name, _, base = (name or "").partition("::")
        for wid, data_dir, db_path in self._all_backup_targets():
            cur = "全局" if wid == 0 else self.wm.name_of(wid)
            if cur != ws_name:
                continue
            for p, _s, _t in backup_mod.list_backups(data_dir):
                if os.path.basename(p) == base:
                    db_inst = self.wm._dbs.get(wid) if wid else None
                    ok = backup_mod.restore(p, db_path, db=db_inst)
                    if ok and wid:
                        self.wm._dbs.pop(wid, None)  # 下次访问时重建连接
                    self._changed()
                    return {"ok": ok}
        return {"ok": False}

    # ================= 设置 / 窗口 =================
    def setTheme(self, theme):
        if theme in ("dark", "light"):
            self.wm.set_setting("theme", theme)
        return {"ok": True}

    def setGroupBy(self, groupBy, dateGran=None):
        """记住分组方式（folder/date/author/type/none）+ 日期粒度（year/month/day）。"""
        if groupBy in ("folder", "date", "author", "type", "none"):
            self.wm.set_setting("groupBy", groupBy)
        if dateGran in ("year", "month", "day"):
            self.wm.set_setting("dateGran", dateGran)
        return {"ok": True}

    def winMinimize(self):
        try:
            self._window.minimize()
        except Exception:
            pass
        return {"ok": True}

    def winToggleMaximize(self):
        """切换最大化（以 Windows 真实窗口状态为准，避免手动状态与实际不同步）。"""
        mx = False
        try:
            hwnd = self._hwnd()
            if hwnd:
                import ctypes
                mx = bool(ctypes.windll.user32.IsZoomed(hwnd))
                if mx:
                    self._window.restore()
                else:
                    self._window.maximize()
                mx = not mx
            else:
                self._window.maximize()
                mx = True
        except Exception:
            pass
        return {"ok": True, "maximized": mx}

    def _hwnd(self):
        """取当前窗口的 Win32 句柄（无边框窗口自绘边框缩放需要）。"""
        try:
            form = self._window.native
            return int(form.Handle.ToInt64())
        except Exception:
            try:
                import ctypes
                return ctypes.windll.user32.FindWindowW(None, "Lumen") or None
            except Exception:
                return None

    def winStartResize(self, edge):
        """无边框窗口 8 向缩放：Python 侧跟踪鼠标并 SetWindowPos（平滑、无 JS 往返）。

        edge: l / r / t / b / lt / rt / lb / rb
        """
        hwnd = self._hwnd()
        if not hwnd:
            return {"ok": False}
        edge = str(edge)

        def run():
            try:
                import ctypes
                import ctypes.wintypes as wt
                user32 = ctypes.windll.user32
                rc = wt.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rc))
                x0, y0 = rc.left, rc.top
                w0, h0 = rc.right - rc.left, rc.bottom - rc.top
                pt = wt.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                cx0, cy0 = pt.x, pt.y
                minw, minh = 880, 560
                SWP = 0x0004 | 0x0010  # NOZORDER | NOACTIVATE
                while user32.GetAsyncKeyState(0x01) & 0x8000:  # 左键仍按住
                    user32.GetCursorPos(ctypes.byref(pt))
                    dx, dy = pt.x - cx0, pt.y - cy0
                    x, y, w, h = x0, y0, w0, h0
                    if "l" in edge:
                        x = x0 + dx
                        w = w0 - dx
                    if "r" in edge:
                        w = w0 + dx
                    if "t" in edge:
                        y = y0 + dy
                        h = h0 - dy
                    if "b" in edge:
                        h = h0 + dy
                    if w < minw:
                        if "l" in edge:
                            x = x0 + (w0 - minw)
                        w = minw
                    if h < minh:
                        if "t" in edge:
                            y = y0 + (h0 - minh)
                        h = minh
                    user32.SetWindowPos(hwnd, 0, int(x), int(y), int(w), int(h), SWP)
                    time.sleep(0.012)
            except Exception:
                pass

        threading.Thread(target=run, daemon=True, name="win-resize").start()
        return {"ok": True}

    def winClose(self):
        try:
            self._window.destroy()
        except Exception:
            pass
        return {"ok": True}

    # ================= 退出 =================
    def shutdown_backup(self):
        """窗口关闭时调用：按需在退出前自动备份（轻量、仅元数据）。"""
        try:
            for wid, data_dir, db_path in self._all_backup_targets():
                if not os.path.isfile(db_path):
                    continue
                if backup_mod.compute_auto_backup_flag(data_dir, db_path,
                                                       min_interval_sec=120):
                    res = backup_mod.create_backup(data_dir, db_path, label="auto")
                    if res:
                        backup_mod.prune(data_dir, 5)
        except Exception:
            pass
        try:
            thumbs.cleanup_cache()
        except Exception:
            pass
        self.wm.close_all()
