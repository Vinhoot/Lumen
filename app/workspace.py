# -*- coding: utf-8 -*-
"""工作区管理：多套互相隔离的媒体库（工作/生活/…）。

- 元库 data/meta.db 只存工作区清单与全局设置（当前工作区、主题）；
- 每个工作区一个独立 SQLite 库 data/ws_<id>/mediavault.db（完全复用既有 Database），
  备份也按工作区独立轮转；
- 首次运行时若存在旧版单库 data/mediavault.db，自动迁移为「工作」工作区，旧文件保留不删。
"""
import os
import sqlite3
import threading
import time

from .db import Database
from .paths import dirs

META_SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings(
  key TEXT PRIMARY KEY,
  value TEXT
);
"""


class WorkspaceManager:
    def __init__(self):
        d = dirs()
        self.data_dir = d["data"]
        self.meta_path = os.path.join(self.data_dir, "meta.db")
        self._lock = threading.RLock()
        self._dbs: dict[int, Database] = {}
        self._conn = sqlite3.connect(self.meta_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(META_SCHEMA)
        self._conn.commit()
        self._migrate_legacy()
        if not self.get_setting("defaultsSeeded"):
            names = {w["name"] for w in self.list_workspaces()}
            if "工作" not in names:
                self.create_workspace("工作")
            if "生活" not in names:
                self.create_workspace("生活")
            self.set_setting("defaultsSeeded", "1")
        if not self.list_workspaces():
            wid = self.create_workspace("工作")["id"]
        if self.get_setting("currentWorkspaceId") is None:
            self.set_setting("currentWorkspaceId", str(self.list_workspaces()[0]["id"]))
        if self.get_setting("theme") is None:
            self.set_setting("theme", "dark")

    # ---------- 元库 ----------
    def get_setting(self, key, default=None):
        r = self._conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set_setting(self, key, value):
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
            self._conn.commit()

    def _migrate_legacy(self):
        """旧版单库 data/mediavault.db → 「工作」工作区（仅首次）。"""
        legacy = os.path.join(self.data_dir, "mediavault.db")
        if not os.path.isfile(legacy):
            return
        if self.list_workspaces():
            return
        try:
            n = sqlite3.connect(legacy).execute("SELECT COUNT(*) FROM files").fetchone()[0]
        except Exception:
            n = 0
        ws = self.create_workspace("工作")
        dst = self.db_path(ws["id"])
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            import shutil
            shutil.copy2(legacy, dst)
            for ext in ("-wal", "-shm"):
                p = legacy + ext
                if os.path.isfile(p):
                    try:
                        shutil.copy2(p, dst + ext)
                    except OSError:
                        pass
        except OSError:
            pass

    # ---------- 工作区 CRUD ----------
    def list_workspaces(self):
        return [dict(r) for r in self._conn.execute(
            "SELECT id, name, created_at FROM workspaces ORDER BY id")]

    def create_workspace(self, name):
        name = (name or "").strip() or "未命名"
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO workspaces(name, created_at) VALUES(?,?)", (name, time.time()))
            self._conn.commit()
            return {"id": cur.lastrowid, "name": name}

    def rename_workspace(self, wid, name):
        name = (name or "").strip()
        if not name:
            return
        with self._lock:
            self._conn.execute("UPDATE workspaces SET name=? WHERE id=?", (name, wid))
            self._conn.commit()

    def delete_workspace(self, wid):
        """删除工作区：库文件移入回收目录（不直接删，防止误删）。"""
        with self._lock:
            self._conn.execute("DELETE FROM workspaces WHERE id=?", (wid,))
            self._conn.commit()
        db = self._dbs.pop(wid, None)
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
        wdir = self.ws_dir(wid)
        if os.path.isdir(wdir):
            try:
                trash = os.path.join(self.data_dir, "_trash",
                                     time.strftime("ws_%Y%m%d_%H%M%S_") + str(wid))
                os.makedirs(os.path.dirname(trash), exist_ok=True)
                os.replace(wdir, trash)
            except OSError:
                pass

    # ---------- 当前工作区 ----------
    def current_id(self) -> int:
        try:
            wid = int(self.get_setting("currentWorkspaceId", "0") or 0)
        except ValueError:
            wid = 0
        ids = [w["id"] for w in self.list_workspaces()]
        if wid not in ids:
            wid = ids[0] if ids else 0
            if wid:
                self.set_setting("currentWorkspaceId", str(wid))
        return wid

    def set_current(self, wid):
        self.set_setting("currentWorkspaceId", str(wid))

    def name_of(self, wid) -> str:
        r = self._conn.execute("SELECT name FROM workspaces WHERE id=?", (wid,)).fetchone()
        return r["name"] if r else str(wid)

    # ---------- 路径 / 数据库实例 ----------
    def ws_dir(self, wid) -> str:
        return os.path.join(self.data_dir, f"ws_{wid}")

    def db_path(self, wid) -> str:
        return os.path.join(self.ws_dir(wid), "mediavault.db")

    def db(self, wid=None) -> Database:
        wid = wid or self.current_id()
        with self._lock:
            inst = self._dbs.get(wid)
            if inst is None:
                os.makedirs(self.ws_dir(wid), exist_ok=True)
                inst = Database(self.db_path(wid))
                self._dbs[wid] = inst
            return inst

    def close_all(self):
        with self._lock:
            for db in self._dbs.values():
                try:
                    db.close()
                except Exception:
                    pass
            try:
                self._conn.commit()
                self._conn.close()
            except Exception:
                pass
