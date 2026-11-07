"""数据备份：把 SQLite 数据库压缩成 gzip 快照。

设计要点（对应"备份体积小"）：
- 只备份数据库（作者/标签/文件索引/设置），**不含缩略图**（缩略图可再生）；
- gzip 压缩级别 9，SQLite 行式存储压缩比通常 8~15 倍（几十万条的库也只有几 MB）；
- 用 SQLite 官方 `VACUUM INTO` 生成一致性快照，WAL 未落盘的数据也包含在内；
- 按数量轮转，默认保留最近 5 份，自动淘汰最旧。
"""
import gzip
import os
import shutil
import sqlite3
import time

DEFAULT_KEEP = 5
PREFIX = "backup_"
SUFFIX = ".db.gz"


def backups_dir(data_dir: str) -> str:
    d = os.path.join(data_dir, "backups")
    os.makedirs(d, exist_ok=True)
    return d


def list_backups(data_dir: str) -> list:
    """返回 [(path, size_bytes, mtime)]，按时间从新到旧。"""
    d = backups_dir(data_dir)
    out = []
    try:
        for name in os.listdir(d):
            if name.startswith(PREFIX) and name.endswith(SUFFIX):
                p = os.path.join(d, name)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                out.append((p, st.st_size, st.st_mtime))
    except OSError:
        return []
    out.sort(key=lambda x: x[2], reverse=True)
    return out


def _snapshot(db_path: str, snapshot_path: str):
    """用 VACUUM INTO 生成一致性快照（含 WAL 中已提交但未 checkpoint 的数据）。"""
    if os.path.exists(snapshot_path):
        os.remove(snapshot_path)
    conn = sqlite3.connect(db_path, timeout=20)
    try:
        conn.execute("VACUUM INTO ?", (snapshot_path,))
    finally:
        conn.close()


def create_backup(data_dir: str, db_path: str, keep: int = DEFAULT_KEEP,
                  label: str = "") -> dict | None:
    """创建一份 gzip 备份；返回 {'path','size','raw_size','ratio'} 或 None（失败）。"""
    if not os.path.isfile(db_path):
        return None
    d = backups_dir(data_dir)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    tmp_snap = os.path.join(d, f".snap_{os.getpid()}_{stamp}.db")
    tag = f"_{label}" if label else ""
    final = os.path.join(d, f"{PREFIX}{stamp}{tag}{SUFFIX}")
    try:
        _snapshot(db_path, tmp_snap)
        raw = os.path.getsize(tmp_snap)
        with open(tmp_snap, "rb") as src, gzip.open(final, "wb", compresslevel=9) as dst:
            shutil.copyfileobj(src, dst, 1024 * 256)
        size = os.path.getsize(final)
        # 校验：能被 gzip 正常读回且头部是 SQLite 魔数
        with gzip.open(final, "rb") as fh:
            if fh.read(16) != b"SQLite format 3\x00":
                raise ValueError("backup integrity check failed")
        return {"path": final, "size": size, "raw_size": raw,
                "ratio": (raw / size) if size else 1.0}
    except Exception:
        try:
            if os.path.isfile(final):
                os.remove(final)
        except OSError:
            pass
        return None
    finally:
        try:
            if os.path.isfile(tmp_snap):
                os.remove(tmp_snap)
        except OSError:
            pass
        # 清理残留的半成品快照
        try:
            for name in os.listdir(d):
                if name.startswith(".snap_"):
                    try:
                        os.remove(os.path.join(d, name))
                    except OSError:
                        pass
        except OSError:
            pass


def prune(data_dir: str, keep: int = DEFAULT_KEEP) -> int:
    """只保留最近 keep 份；返回删除数量。"""
    backups = list_backups(data_dir)
    removed = 0
    for p, _, _ in backups[max(keep, 1):]:
        try:
            os.remove(p)
            removed += 1
        except OSError:
            pass
    return removed


def _replace_with_retry(src: str, dst: str, attempts: int = 12) -> bool:
    """Windows 下文件句柄释放有延迟，替换操作带重试。"""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return True
        except PermissionError:
            time.sleep(0.15 * (i + 1))
        except OSError:
            time.sleep(0.15 * (i + 1))
    return False


def restore(backup_path: str, db_path: str, db=None, _err: dict | None = None) -> bool:
    """把备份恢复到数据库路径。
    db: 可选的 Database 实例，会先关闭其连接（Windows 下必须释放文件句柄）。
    _err: 传入 dict 可拿到失败原因（用于测试/诊断）。"""
    tmp = db_path + ".restore.tmp"
    try:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
        data_dir = os.path.dirname(os.path.normpath(db_path))
        # 先备份当前库，防止恢复出错
        if os.path.isfile(db_path):
            create_backup(data_dir, db_path, label="before_restore")
        with gzip.open(backup_path, "rb") as src, open(tmp, "wb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 256)
        if os.path.getsize(tmp) < 512:
            os.remove(tmp)
            raise ValueError("backup too small")
        # 移除 WAL/SHM，避免与新库不一致
        for ext in ("-wal", "-shm"):
            p = db_path + ext
            if os.path.isfile(p):
                for _ in range(10):
                    try:
                        os.remove(p)
                        break
                    except OSError:
                        time.sleep(0.1)
        if not _replace_with_retry(tmp, db_path):
            raise PermissionError(f"cannot replace {db_path} (file locked)")
        return True
    except Exception as e:
        if _err is not None:
            import traceback
            _err["error"] = f"{type(e).__name__}: {e}"
            _err["trace"] = traceback.format_exc()
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


def compute_auto_backup_flag(data_dir: str, db_path: str, min_interval_sec: int = 300) -> bool:
    """是否需要自动备份：距上次备份超过间隔，或数据库有变化。"""
    backups = list_backups(data_dir)
    if not backups:
        return os.path.isfile(db_path)
    last_ts = backups[0][2]
    if time.time() - last_ts >= min_interval_sec:
        # 数据库比最近备份新才备份（避免无变化时反复写盘）
        try:
            return os.path.getmtime(db_path) > last_ts
        except OSError:
            return False
    return False
