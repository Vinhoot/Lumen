"""SQLite 数据层：媒体文件、作者、标签、扫描目录、设置。

设计要点（对应"系统占用要小"）：
- WAL 模式 + 索引，查询零全表扫描；
- 所有列表查询分页/限量，UI 模型按需取数；
- 每连接短事务，写入后立即释放锁。
"""
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

from .paths import dirs

SCHEMA = """
CREATE TABLE IF NOT EXISTS folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    recursive INTEGER NOT NULL DEFAULT 1,
    added_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS authors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    color TEXT NOT NULL DEFAULT '',
    manual INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    folder TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'image',          -- image | video
    ext TEXT NOT NULL DEFAULT '',
    size INTEGER NOT NULL DEFAULT 0,
    mtime REAL NOT NULL DEFAULT 0,
    width INTEGER, height INTEGER,
    duration REAL,
    fingerprint TEXT NOT NULL DEFAULT '',        -- path+mtime+size 摘要，增量扫描依据
    author_id INTEGER REFERENCES authors(id) ON DELETE SET NULL,
    author_manual INTEGER NOT NULL DEFAULT 0,    -- 1=用户手动指定，重扫不覆盖
    author_confidence REAL NOT NULL DEFAULT 0,   -- 0~1；<0.7 进入"待确认"
    author_candidate TEXT NOT NULL DEFAULT '',   -- 疑似作者名（待确认时显示）
    title TEXT NOT NULL DEFAULT '',
    added_at REAL NOT NULL,
    last_seen REAL NOT NULL,
    missing INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    color TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS file_tags (
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (file_id, tag_id)
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS idx_files_author ON files(author_id);
CREATE INDEX IF NOT EXISTS idx_files_kind ON files(kind);
CREATE INDEX IF NOT EXISTS idx_files_missing ON files(missing);
CREATE INDEX IF NOT EXISTS idx_files_folder ON files(folder);
CREATE INDEX IF NOT EXISTS idx_file_tags_tag ON file_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_files_missing_mtime ON files(missing, mtime);
CREATE INDEX IF NOT EXISTS idx_files_missing_added ON files(missing, added_at);
CREATE INDEX IF NOT EXISTS idx_files_missing_size ON files(missing, size);
"""


def _like_prefix(path: str) -> str:
    """SQL LIKE 前缀（转义 ! % _），用于「某目录及其子目录」匹配。"""
    esc = (path.replace("!", "!!").replace("%", "!%").replace("_", "!_"))
    return esc + os.sep + "%"


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".avif", ".ico", ".jfif", ".heic"}
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpg", ".mpeg", ".ts", ".rmvb", ".3gp", ".asf"}


class Database:
    def __init__(self, db_path: str | None = None):
        self._path = db_path or dirs()["db"]
        self._lock = threading.Lock()
        self._local = threading.local()
        with self.conn() as c:
            self._migrate(c)
            c.executescript(SCHEMA)

    def _migrate(self, c: sqlite3.Connection):
        """老库升级：files 表补 folder 列并从 path 回填（须在 SCHEMA 之前执行）。"""
        tbl = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='files'").fetchone()
        if tbl is None:
            return
        cols = [r[1] for r in c.execute("PRAGMA table_info(files)").fetchall()]
        if "folder" not in cols:
            c.execute("ALTER TABLE files ADD COLUMN folder TEXT NOT NULL DEFAULT ''")
            rows = c.execute("SELECT id, path FROM files WHERE folder=''").fetchall()
            c.executemany("UPDATE files SET folder=? WHERE id=?",
                          [(os.path.dirname(r["path"]), r["id"]) for r in rows])

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        # 5 万级批量写入时排序/索引会溢出到临时文件（受 TEMP 目录策略影响且慢）；
        # 放到内存里既快又不受外部临时目录限制
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-32000")   # 32MB 页缓存
        return conn

    @contextmanager
    def conn(self):
        """线程本地连接；自动提交/回滚。"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # ---------- folders ----------
    def add_folder(self, path: str, recursive: bool = True) -> bool:
        path = os.path.normpath(path)
        with self.conn() as c:
            cur = c.execute("INSERT OR IGNORE INTO folders(path, recursive, added_at) VALUES(?,?,?)",
                            (path, 1 if recursive else 0, time.time()))
            return cur.rowcount > 0

    def remove_folder(self, folder_id: int):
        with self.conn() as c:
            c.execute("DELETE FROM folders WHERE id=?", (folder_id,))

    def list_watch_folders(self):
        """记住的扫描目录（folders 表）。"""
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM folders ORDER BY added_at")]

    # ---------- files ----------
    def existing_fingerprints(self):
        with self.conn() as c:
            rows = c.execute("SELECT path, fingerprint FROM files WHERE missing=0").fetchall()
            return {r["path"]: r["fingerprint"] for r in rows}

    def all_indexed_paths(self):
        with self.conn() as c:
            return {r["path"] for r in c.execute("SELECT path FROM files")}

    def missing_paths(self):
        """已标记失效的路径集合（重新出现时需「复活」，不能因指纹相同被跳过）。"""
        with self.conn() as c:
            return {r["path"] for r in c.execute("SELECT path FROM files WHERE missing=1")}

    def paths_under(self, folder: str):
        """某个目录（含子目录）下已入库的全部路径（失效检测用，走索引）。"""
        with self.conn() as c:
            return [r["path"] for r in c.execute(
                "SELECT path FROM files WHERE folder=? OR folder LIKE ? ESCAPE '!'",
                (folder, _like_prefix(folder)))]

    def files_by_ids(self, ids):
        """按 id 批量取记录（分块，避开 SQLite 变量上限）。"""
        out = []
        ids = [int(i) for i in (ids or [])]
        with self.conn() as c:
            for i in range(0, len(ids), 400):
                chunk = ids[i:i + 400]
                q = ",".join("?" * len(chunk))
                out += [dict(r) for r in c.execute(
                    f"SELECT * FROM files WHERE id IN ({q})", chunk)]
        return out

    def _do_upsert(self, c, f: dict):
        """单条 upsert（在给定连接/事务内执行）；返回 (file_id, is_new)。"""
        row = c.execute("SELECT id FROM files WHERE path=?", (f["path"],)).fetchone()
        if row:
            c.execute("""UPDATE files SET folder=?, kind=?, ext=?, size=?, mtime=?, width=?, height=?,
                         duration=?, fingerprint=?, last_seen=?, missing=0,
                         author_confidence=CASE WHEN author_manual=1 THEN author_confidence ELSE ? END,
                         author_candidate=CASE WHEN author_manual=1 THEN author_candidate ELSE ? END,
                         author_id=CASE WHEN author_manual=1 THEN author_id ELSE ? END
                         WHERE id=?""",
                      (f.get("folder", ""), f["kind"], f["ext"], f["size"], f["mtime"],
                       f.get("width"), f.get("height"),
                       f.get("duration"), f["fingerprint"], f["last_seen"],
                       f.get("author_confidence", 0), f.get("author_candidate", ""),
                       f.get("author_id"), row["id"]))
            return row["id"], False
        cur = c.execute("""INSERT INTO files(path, folder, kind, ext, size, mtime, width, height, duration,
                           fingerprint, author_id, author_manual, author_confidence, author_candidate,
                           title, added_at, last_seen)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?)""",
                        (f["path"], f.get("folder", ""), f["kind"], f["ext"], f["size"], f["mtime"],
                         f.get("width"), f.get("height"), f.get("duration"), f["fingerprint"],
                         f.get("author_id"), f.get("author_confidence", 0),
                         f.get("author_candidate", ""), f.get("title", ""),
                         f.get("added_at", time.time()), f["last_seen"]))
        return cur.lastrowid, True

    def upsert_file(self, f: dict):
        """插入或更新一条媒体记录；返回 (file_id, is_new)。"""
        with self.conn() as c:
            return self._do_upsert(c, f)

    # 单条语句完成「插入或更新」：比先 SELECT 再 UPDATE 少一半语句开销
    _UPSERT_SQL = """INSERT INTO files(path, folder, kind, ext, size, mtime, width, height,
                       duration, fingerprint, author_id, author_manual, author_confidence,
                       author_candidate, title, added_at, last_seen, missing)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,0)
                     ON CONFLICT(path) DO UPDATE SET
                       folder=excluded.folder, kind=excluded.kind, ext=excluded.ext,
                       size=excluded.size, mtime=excluded.mtime, width=excluded.width,
                       height=excluded.height, duration=excluded.duration,
                       fingerprint=excluded.fingerprint, last_seen=excluded.last_seen, missing=0,
                       author_confidence=CASE WHEN files.author_manual=1
                         THEN files.author_confidence ELSE excluded.author_confidence END,
                       author_candidate=CASE WHEN files.author_manual=1
                         THEN files.author_candidate ELSE excluded.author_candidate END,
                       author_id=CASE WHEN files.author_manual=1
                         THEN files.author_id ELSE excluded.author_id END"""

    def bulk_upsert_files(self, records: list) -> tuple[int, int]:
        """批量 upsert（单事务 + 单条 ON CONFLICT，5 万级也只跑一遍）。

        records 每项可带 `_new`（扫描阶段已知该路径是否新），用于统计新增/更新。
        """
        if not records:
            return 0, 0
        added = sum(1 for f in records if f.get("_new", True))
        rows = [(f["path"], f.get("folder", ""), f["kind"], f["ext"], f["size"],
                 f["mtime"], f.get("width"), f.get("height"), f.get("duration"),
                 f["fingerprint"], f.get("author_id"), f.get("author_confidence", 0),
                 f.get("author_candidate", ""), f.get("title", ""),
                 f.get("added_at", time.time()), f["last_seen"]) for f in records]
        with self.conn() as c:
            c.executemany(self._UPSERT_SQL, rows)
        return added, len(records) - added

    def authors_map(self) -> dict:
        """name -> id 全量映射（扫描时缓存，避免逐文件查询）。"""
        with self.conn() as c:
            return {r["name"]: r["id"] for r in c.execute("SELECT id, name FROM authors")}

    def mark_missing(self, paths):
        if not paths:
            return
        with self.conn() as c:
            c.executemany("UPDATE files SET missing=1 WHERE path=?", [(p,) for p in paths])

    def clear_missing(self, also_files: bool = False):
        """移除已失效的条目（文件已不存在）。"""
        with self.conn() as c:
            if also_files:
                c.execute("DELETE FROM files WHERE missing=1")
            c.execute("DELETE FROM authors WHERE id IN "
                      "(SELECT a.id FROM authors a LEFT JOIN files f ON f.author_id=a.id "
                      " WHERE f.id IS NULL AND a.manual=0)")

    def get_file(self, file_id: int):
        with self.conn() as c:
            r = c.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
            return dict(r) if r else None

    def get_file_by_path(self, path: str):
        with self.conn() as c:
            r = c.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()
            return dict(r) if r else None

    # ---------- authors ----------
    def author_id_by_name(self, name: str, color: str = ""):
        with self.conn() as c:
            r = c.execute("SELECT id FROM authors WHERE name=?", (name,)).fetchone()
            if r:
                return r["id"]
            cur = c.execute("INSERT INTO authors(name, color, manual) VALUES(?,?,0)", (name, color))
            return cur.lastrowid

    def get_or_create_author(self, name: str, color: str = "", manual: bool = False):
        with self.conn() as c:
            r = c.execute("SELECT id FROM authors WHERE name=?", (name,)).fetchone()
            if r:
                if manual:
                    c.execute("UPDATE authors SET manual=1 WHERE id=?", (r["id"],))
                return r["id"]
            cur = c.execute("INSERT INTO authors(name, color, manual) VALUES(?,?,?)",
                            (name, color, 1 if manual else 0))
            return cur.lastrowid

    def create_author(self, name: str, color: str = "") -> int:
        return self.get_or_create_author(name, color, manual=True)

    def rename_author(self, author_id: int, new_name: str):
        with self.conn() as c:
            exists = c.execute("SELECT id FROM authors WHERE name=?", (new_name,)).fetchone()
            if exists and exists["id"] != author_id:
                # 同名作者 → 合并：迁移文件后删除旧作者
                c.execute("UPDATE files SET author_id=? WHERE author_id=?", (exists["id"], author_id))
                c.execute("DELETE FROM authors WHERE id=?", (author_id,))
            else:
                c.execute("UPDATE authors SET name=?, manual=1 WHERE id=?", (new_name, author_id))

    def set_author_color(self, author_id: int, color: str):
        with self.conn() as c:
            c.execute("UPDATE authors SET color=? WHERE id=?", (color, author_id))

    def get_author(self, author_id: int):
        with self.conn() as c:
            r = c.execute("SELECT * FROM authors WHERE id=?", (author_id,)).fetchone()
            return dict(r) if r else None

    def get_tag(self, tag_id: int):
        with self.conn() as c:
            r = c.execute("SELECT * FROM tags WHERE id=?", (tag_id,)).fetchone()
            return dict(r) if r else None

    def author_names(self, ids) -> dict:
        out = {}
        if not ids:
            return out
        q = ",".join("?" * len(ids))
        with self.conn() as c:
            for r in c.execute(f"SELECT id, name FROM authors WHERE id IN ({q})", ids):
                out[r["id"]] = r["name"]
        return out

    def delete_author(self, author_id: int):
        with self.conn() as c:
            c.execute("UPDATE files SET author_id=NULL, author_manual=0 WHERE author_id=?", (author_id,))
            c.execute("DELETE FROM authors WHERE id=?", (author_id,))

    def list_authors(self, counts: bool = True):
        with self.conn() as c:
            if counts:
                return [dict(r) for r in c.execute(
                    """SELECT a.*, COUNT(f.id) AS cnt FROM authors a
                       LEFT JOIN files f ON f.author_id=a.id AND f.missing=0
                       GROUP BY a.id ORDER BY a.name COLLATE NOCASE""")]
            return [dict(r) for r in c.execute("SELECT * FROM authors ORDER BY name COLLATE NOCASE")]

    def assign_author(self, file_ids, author_id: int, manual: bool = True):
        with self.conn() as c:
            c.executemany("UPDATE files SET author_id=?, author_manual=?, author_confidence=1.0 WHERE id=?",
                          [(author_id, 1 if manual else 0, i) for i in file_ids])

    # ---------- tags ----------
    def create_tag(self, name: str, color: str = ""):
        with self.conn() as c:
            cur = c.execute("INSERT OR IGNORE INTO tags(name, color) VALUES(?,?)", (name, color))
            if cur.rowcount == 0:
                c.execute("UPDATE tags SET color=? WHERE name=?", (color, name))
            return c.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()["id"]

    def rename_tag(self, tag_id: int, new_name: str):
        with self.conn() as c:
            c.execute("UPDATE tags SET name=? WHERE id=?", (new_name, tag_id))

    def set_tag_color(self, tag_id: int, color: str):
        with self.conn() as c:
            c.execute("UPDATE tags SET color=? WHERE id=?", (color, tag_id))

    def delete_tag(self, tag_id: int):
        with self.conn() as c:
            c.execute("DELETE FROM file_tags WHERE tag_id=?", (tag_id,))
            c.execute("DELETE FROM tags WHERE id=?", (tag_id,))

    def list_tags(self):
        with self.conn() as c:
            return [dict(r) for r in c.execute(
                """SELECT t.*, COUNT(ft.file_id) AS cnt FROM tags t
                   LEFT JOIN file_tags ft ON ft.tag_id=t.id
                   GROUP BY t.id ORDER BY t.name COLLATE NOCASE""")]

    def add_file_tags(self, file_ids, tag_ids):
        with self.conn() as c:
            c.executemany("INSERT OR IGNORE INTO file_tags(file_id, tag_id) VALUES(?,?)",
                          [(f, t) for f in file_ids for t in tag_ids])

    def remove_file_tag(self, file_ids, tag_id: int):
        with self.conn() as c:
            c.executemany("DELETE FROM file_tags WHERE file_id=? AND tag_id=?", [(f, tag_id) for f in file_ids])

    def tags_for_file(self, file_id: int):
        with self.conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT t.* FROM tags t JOIN file_tags ft ON ft.tag_id=t.id WHERE ft.file_id=?", (file_id,))]

    def tag_map_for_files(self, file_ids):
        """file_id -> [tag_id]，用于网格显示标签角标（单次查询）。"""
        out = {i: [] for i in file_ids}
        if not file_ids:
            return out
        q = ",".join("?" * len(file_ids))
        with self.conn() as c:
            for r in c.execute(f"SELECT file_id, tag_id FROM file_tags WHERE file_id IN ({q})", file_ids):
                out[r["file_id"]].append(r["tag_id"])
        return out

    def close(self):
        """关闭本线程的数据库连接（恢复备份/退出时调用，释放 Windows 文件句柄）。"""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.commit()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
            try:
                del self._local.conn
            except Exception:
                pass

    # ---------- 查询 / 筛选 ----------
    def list_folders(self):
        """folder -> 文件数（仅未失效文件）。"""
        with self.conn() as c:
            return [(r["folder"], r["cnt"]) for r in c.execute(
                "SELECT folder, COUNT(*) AS cnt FROM files WHERE missing=0 AND folder != '' "
                "GROUP BY folder")]
    def count_by_author(self, author_id: int | None, include_uncertain=False):
        with self.conn() as c:
            if author_id is None:
                return c.execute("SELECT COUNT(*) FROM files WHERE missing=0 AND author_id IS NULL").fetchone()[0]
            if include_uncertain:
                return c.execute("SELECT COUNT(*) FROM files WHERE missing=0 AND author_id=? "
                                 "AND author_manual=0 AND author_confidence<0.7", (author_id,)).fetchone()[0]
            return c.execute("SELECT COUNT(*) FROM files WHERE missing=0 AND author_id=?", (author_id,)).fetchone()[0]

    def query_files(self, kind=None, author_id: int | None = None, tag_ids=None,
                    search=None, missing=False, uncertain=False, unclassified=False,
                    folder: str | None = None, folder_recursive=False,
                    sort="added", desc=True, limit=500, offset=0):
        """统一筛选入口。tag_ids 多选 = 同时具备所有标签（AND）。"""
        where = ["missing=?"]
        params: list = [1 if missing else 0]
        if kind:
            where.append("kind=?")
            params.append(kind)
        if author_id is not None:
            where.append("author_id=?")
            params.append(author_id)
        if folder is not None:
            if folder_recursive:
                where.append("(folder=? OR folder LIKE ? ESCAPE '!')")
                params += [folder, _like_prefix(folder)]
            else:
                where.append("folder=?")
                params.append(folder)
        if uncertain:
            where.append("author_id IS NULL AND author_candidate != ''")
        if unclassified:
            where.append("author_id IS NULL AND author_candidate = ''")
        if search:
            where.append("(path LIKE ? OR title LIKE ?)")
            like = f"%{search}%"
            params += [like, like]
        if tag_ids:
            where.append("""id IN (SELECT ft.file_id FROM file_tags ft
                                 WHERE ft.tag_id IN (%s)
                                 GROUP BY ft.file_id HAVING COUNT(DISTINCT ft.tag_id)=%d)"""
                         % (",".join("?" * len(tag_ids)), len(tag_ids)))
            params += list(tag_ids)
        order = {"added": "added_at", "mtime": "mtime", "size": "size",
                 "name": "path", "author": "author_id"}.get(sort, "added_at")
        dirn = "DESC" if desc else "ASC"
        sql = (f"SELECT id, path, folder, kind, ext, size, mtime, width, height, duration, author_id, "
               f"author_confidence, author_candidate, title, added_at FROM files "
               f"WHERE {' AND '.join(where)} ORDER BY {order} {dirn} LIMIT ? OFFSET ?")
        params += [limit, offset]
        with self.conn() as c:
            return [dict(r) for r in c.execute(sql, params)]

    def count_files(self, kind=None, author_id: int | None = None, tag_ids=None,
                    search=None, missing=False, uncertain=False, unclassified=False,
                    folder: str | None = None, folder_recursive=False):
        where = ["missing=?"]
        params: list = [1 if missing else 0]
        if kind:
            where.append("kind=?")
            params.append(kind)
        if author_id is not None:
            where.append("author_id=?")
            params.append(author_id)
        if folder is not None:
            if folder_recursive:
                where.append("(folder=? OR folder LIKE ? ESCAPE '!')")
                params += [folder, _like_prefix(folder)]
            else:
                where.append("folder=?")
                params.append(folder)
        if uncertain:
            where.append("author_id IS NULL AND author_candidate != ''")
        if unclassified:
            where.append("author_id IS NULL AND author_candidate = ''")
        if search:
            where.append("(path LIKE ? OR title LIKE ?)")
            like = f"%{search}%"
            params += [like, like]
        if tag_ids:
            where.append("""id IN (SELECT ft.file_id FROM file_tags ft
                                 WHERE ft.tag_id IN (%s)
                                 GROUP BY ft.file_id HAVING COUNT(DISTINCT ft.tag_id)=%d)"""
                         % (",".join("?" * len(tag_ids)), len(tag_ids)))
            params += list(tag_ids)
        with self.conn() as c:
            return c.execute(f"SELECT COUNT(*) FROM files WHERE {' AND '.join(where)}", params).fetchone()[0]

    # ---------- 分页 + 分组查询（5 万级库的主力路径） ----------
    def _where(self, kind=None, author_id=None, tag_ids=None, search=None,
               search_field="name", missing=False, uncertain=False,
               unclassified=False, untagged=False, no_author=False, folder=None,
               folder_recursive=False, alias="f"):
        """统一的 WHERE 片段：所有筛选（含「按作者/标签名搜索」）都下推到 SQL。

        这样分页永远只取一页，不再"取 5000 条再在内存里过滤"。
        """
        w = [f"{alias}.missing=?"]
        p: list = [1 if missing else 0]
        if kind:
            w.append(f"{alias}.kind=?")
            p.append(kind)
        if author_id is not None:
            w.append(f"{alias}.author_id=?")
            p.append(int(author_id))
        if folder is not None:
            if folder_recursive:
                w.append(f"({alias}.folder=? OR {alias}.folder LIKE ? ESCAPE '!')")
                p += [folder, _like_prefix(folder)]
            else:
                w.append(f"{alias}.folder=?")
                p.append(folder)
        if uncertain:
            w.append(f"{alias}.author_id IS NULL AND {alias}.author_candidate != ''")
        if unclassified:
            w.append(f"{alias}.author_id IS NULL AND {alias}.author_candidate = ''")
        if no_author:
            w.append(f"{alias}.author_id IS NULL")
        if untagged:
            w.append(f"NOT EXISTS (SELECT 1 FROM file_tags ft WHERE ft.file_id={alias}.id)")
        if search:
            like = f"%{search}%"
            if search_field == "author":
                w.append(f"({alias}.author_id IN (SELECT id FROM authors WHERE name LIKE ?) "
                         f"OR {alias}.author_candidate LIKE ?)")
                p += [like, like]
            elif search_field == "tag":
                w.append(f"EXISTS (SELECT 1 FROM file_tags ft JOIN tags t ON t.id=ft.tag_id "
                         f"WHERE ft.file_id={alias}.id AND t.name LIKE ?)")
                p.append(like)
            else:
                w.append(f"({alias}.path LIKE ? OR {alias}.title LIKE ?)")
                p += [like, like]
        if tag_ids:
            ids = [int(t) for t in tag_ids]
            w.append(f"{alias}.id IN (SELECT ft.file_id FROM file_tags ft "
                     f"WHERE ft.tag_id IN ({','.join('?' * len(ids))}) "
                     f"GROUP BY ft.file_id HAVING COUNT(DISTINCT ft.tag_id)={len(ids)})")
            p += ids
        return w, p

    @staticmethod
    def _group_expr(group_by, date_gran):
        """分组键的 SQL 表达式（与前端 groupBy/dateGran 完全对应）。"""
        if group_by == "date":
            fmt = {"year": "%Y", "month": "%Y-%m", "day": "%Y-%m-%d"}.get(date_gran, "%Y-%m")
            return f"strftime('{fmt}', f.mtime, 'unixepoch', 'localtime')"
        if group_by == "author":
            return "COALESCE(a.name, NULLIF(f.author_candidate,''), '')"
        if group_by == "type":
            return "f.kind"
        if group_by == "none":
            return "''"
        return "f.folder"

    def query_page(self, kind=None, author_id=None, tag_ids=None, search=None,
                   search_field="name", missing=False, uncertain=False,
                   unclassified=False, untagged=False, no_author=False, folder=None,
                   folder_recursive=False, group_by="folder", date_gran="month",
                   sort="mtime", desc=True, limit=300, offset=0):
        """按「分组键 + 排序键」在 SQL 层排好序，取一页；返回 (rows, total)。

        rows 里带 gkey（分组键），调用方据此切分成连续分组。
        """
        w, p = self._where(kind=kind, author_id=author_id, tag_ids=tag_ids, search=search,
                           search_field=search_field, missing=missing, uncertain=uncertain,
                           unclassified=unclassified, untagged=untagged, no_author=no_author,
                           folder=folder, folder_recursive=folder_recursive)
        where = " AND ".join(w)
        ge = self._group_expr(group_by, date_gran)
        scol = {"added": "f.added_at", "mtime": "f.mtime", "size": "f.size",
                "name": "f.path", "author": "f.author_id"}.get(sort, "f.mtime")
        dirn = "DESC" if desc else "ASC"
        if group_by == "date":
            order = f"gkey DESC, {scol} {dirn}, f.id DESC"
        elif group_by == "none":
            order = f"{scol} {dirn}, f.id DESC"
        else:
            order = f"gkey COLLATE NOCASE ASC, {scol} {dirn}, f.id DESC"
        base = f"FROM files f LEFT JOIN authors a ON a.id=f.author_id WHERE {where}"
        sql = ("SELECT f.id, f.path, f.folder, f.kind, f.ext, f.size, f.mtime, f.width, "
               "f.height, f.duration, f.author_id, f.author_confidence, f.author_candidate, "
               f"f.title, f.added_at, {ge} AS gkey {base} ORDER BY {order} LIMIT ? OFFSET ?")
        with self.conn() as c:
            rows = [dict(r) for r in c.execute(sql, p + [int(limit), int(offset)])]
            total = c.execute("SELECT COUNT(*) " + base, p).fetchone()[0]
        return rows, total

    def group_counts(self, kind=None, author_id=None, tag_ids=None, search=None,
                     search_field="name", uncertain=False, unclassified=False,
                     untagged=False, no_author=False, folder=None, folder_recursive=False,
                     group_by="folder", date_gran="month"):
        """每个分组的真实总数（分页时组标题要显示全量，而不是"已加载"）。"""
        w, p = self._where(kind=kind, author_id=author_id, tag_ids=tag_ids, search=search,
                           search_field=search_field, uncertain=uncertain,
                           unclassified=unclassified, untagged=untagged, no_author=no_author,
                           folder=folder, folder_recursive=folder_recursive)
        ge = self._group_expr(group_by, date_gran)
        base = f"FROM files f LEFT JOIN authors a ON a.id=f.author_id WHERE {' AND '.join(w)}"
        with self.conn() as c:
            return {r["k"]: r["c"] for r in c.execute(
                f"SELECT {ge} AS k, COUNT(*) AS c {base} GROUP BY k", p)}

    def stats(self):
        with self.conn() as c:
            out = {}
            for r in c.execute("SELECT kind, COUNT(*) AS n, COALESCE(SUM(size),0) AS s "
                               "FROM files WHERE missing=0 GROUP BY kind"):
                out[r["kind"]] = (r["n"], r["s"])
            out["uncertain"] = c.execute(
                "SELECT COUNT(*) FROM files WHERE missing=0 AND author_id IS NULL "
                "AND author_candidate != ''").fetchone()[0]
            out["unclassified"] = c.execute(
                "SELECT COUNT(*) FROM files WHERE missing=0 AND author_id IS NULL "
                "AND author_candidate = ''").fetchone()[0]
            out["total"] = c.execute("SELECT COUNT(*) FROM files WHERE missing=0").fetchone()[0]
            return out

    def uncategorized_ids(self, limit=2000):
        with self.conn() as c:
            return [r["id"] for r in c.execute(
                "SELECT id FROM files WHERE missing=0 AND author_id IS NULL LIMIT ?", (limit,))]

    def set_uncertain(self, file_ids, candidate: str = ""):
        """把文件置为待确认状态（保留疑似候选名）。"""
        with self.conn() as c:
            c.executemany("UPDATE files SET author_id=NULL, author_manual=0, author_confidence=0.5, "
                          "author_candidate=? WHERE id=?", [(candidate, i) for i in file_ids])

    def clear_author(self, file_ids):
        with self.conn() as c:
            c.executemany("UPDATE files SET author_id=NULL, author_manual=0, author_confidence=0, "
                          "author_candidate='' WHERE id=?", [(i,) for i in file_ids])

    def remove_files(self, file_ids):
        with self.conn() as c:
            c.executemany("DELETE FROM files WHERE id=?", [(i,) for i in file_ids])

    # ---------- settings ----------
    def get_setting(self, key: str, default=None):
        with self.conn() as c:
            r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return r["value"] if r else default

    def set_setting(self, key: str, value):
        with self.conn() as c:
            c.execute("INSERT INTO settings(key, value) VALUES(?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
