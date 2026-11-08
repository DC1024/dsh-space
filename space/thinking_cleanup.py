#!/usr/bin/env python3
"""thinking 观测档案的 12 小时保留窗口清理器。

只处理 data/observations/thinking.sqlite3（以及同目录的 -wal/-shm），
范围严格限定为 space 自己的 observations 数据：

* records 表：删除 receivedAt 早于保留窗口的 event / raw 行（分批、短事务）。
* states 表：只删除“已终止（terminal=true）”且所属会话整体超出保留窗口的
  native-attempt:* 状态；native-event:*（流式去重标记）、normalizer、
  observer-lifecycle 状态一律保留。
* 不 VACUUM、不删表、不删库、不改 meta、不touch 附件/截图/notes/备份。
* 删除后用 wal_checkpoint(TRUNCATE) 收缩 WAL；主库文件不会被重建，释放出来的
  页进入 freelist，由后续写入优先复用。

用法：
    python3 thinking_cleanup.py --dry-run       # 只统计，不删除
    python3 thinking_cleanup.py --once          # 立即执行一轮
    python3 thinking_cleanup.py --status        # 查看状态与下次执行时间
    python3 thinking_cleanup.py --daemon        # 常驻，每 12 小时执行一轮
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sqlite3
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent
OBS = ROOT / "data" / "observations"
DB = OBS / "thinking.sqlite3"
STATE = OBS / "cleanup-state.json"
AUDIT = OBS / "cleanup-audit.jsonl"
LOG = OBS / "cleanup.log"
LOCK = OBS / ".thinking-cleanup.lock"

DEFAULT_KEEP_HOURS = 12
DEFAULT_INTERVAL_HOURS = 12
BATCH = 2000
MAX_SECONDS = 900


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _log(message: str) -> None:
    line = f"[{_iso(_now())}] {message}"
    print(line, flush=True)
    try:
        with LOG.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, STATE)


def _audit(record: dict) -> None:
    try:
        with AUDIT.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _check_paths() -> None:
    """确认只操作本空间的 observations 库，别的一律拒绝。"""
    resolved = DB.resolve()
    allowed_root = OBS.resolve()
    if allowed_root not in resolved.parents:
        raise SystemExit(f"拒绝操作：{resolved} 不在 {allowed_root} 内")
    if not resolved.is_file():
        raise SystemExit(f"找不到数据库：{resolved}")


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB), timeout=15, isolation_level=None)
    con.execute("PRAGMA busy_timeout=15000")
    con.execute("PRAGMA synchronous=FULL")
    return con


def _sizes() -> dict:
    def size(path: pathlib.Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0
    return {
        "db": size(DB),
        "wal": size(DB.with_name(DB.name + "-wal")),
        "shm": size(DB.with_name(DB.name + "-shm")),
    }


def _session_activity(con: sqlite3.Connection) -> dict:
    """sid -> 最近一条记录的 receivedAt（删除前计算）。"""
    rows = con.execute(
        "SELECT sid, MAX(json_extract(payload,'$.receivedAt')) FROM records GROUP BY sid"
    ).fetchall()
    return {sid: last for sid, last in rows if sid}


def _boundary(con: sqlite3.Connection, cutoff: str) -> int:
    row = con.execute(
        "SELECT MAX(record_id) FROM records WHERE json_extract(payload,'$.receivedAt') < ?",
        (cutoff,),
    ).fetchone()
    return int(row[0] or 0)


def _plan(con: sqlite3.Connection, keep_hours: int) -> dict:
    cutoff = _iso(_now() - dt.timedelta(hours=keep_hours))
    activity = _session_activity(con)
    boundary = _boundary(con, cutoff)
    n, b = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(length(payload)),0) FROM records WHERE record_id <= ? AND json_extract(payload,'$.receivedAt') < ?",
        (boundary, cutoff),
    ).fetchone()
    late = con.execute(
        "SELECT COUNT(*) FROM records WHERE record_id > ? AND json_extract(payload,'$.receivedAt') < ?",
        (boundary, cutoff),
    ).fetchone()[0]
    inactive = {sid for sid, last in activity.items() if last and last < cutoff}
    sn = 0
    sb = 0
    if inactive:
        marks = ",".join("?" for _ in inactive)
        sn, sb = con.execute(
            f"""SELECT COUNT(*), COALESCE(SUM(length(payload)),0) FROM states
                WHERE key LIKE 'native-attempt:%' AND json_extract(payload,'$.terminal')=1
                  AND sid IN ({marks})""",
            tuple(inactive),
        ).fetchone()
    pages = con.execute("PRAGMA page_count").fetchone()[0]
    page_size = con.execute("PRAGMA page_size").fetchone()[0]
    free_pages = con.execute("PRAGMA freelist_count").fetchone()[0]
    live_pages = pages - free_pages
    return {
        "keep_hours": keep_hours,
        "cutoff": cutoff,
        "boundary_record_id": boundary,
        "records": int(n or 0),
        "record_bytes": int(b or 0),
        "late_records_above_boundary": int(late or 0),
        "inactive_sessions": len(inactive),
        "attempt_states": int(sn or 0),
        "attempt_state_bytes": int(sb or 0),
        "page_size": page_size,
        "page_count": pages,
        "freelist_pages": free_pages,
        "live_pages": live_pages,
        "db_bytes": pages * page_size,
        "sizes": _sizes(),
    }


def _delete_records(con: sqlite3.Connection, plan: dict, *, dry_run: bool) -> dict:
    cutoff, boundary = plan["cutoff"], plan["boundary_record_id"]
    deleted = 0
    deleted_bytes = 0
    batches = 0
    started = time.monotonic()
    while deleted < plan["records"] and time.monotonic() - started < MAX_SECONDS:
        rows = con.execute(
            """SELECT record_id, length(payload) FROM records
               WHERE record_id <= ? AND json_extract(payload,'$.receivedAt') < ?
               ORDER BY record_id LIMIT ?""",
            (boundary, cutoff, BATCH),
        ).fetchall()
        if not rows:
            break
        ids = [row[0] for row in rows]
        size = sum(row[1] for row in rows)
        if dry_run:
            deleted += len(ids)
            deleted_bytes += size
            batches += 1
            break
        marks = ",".join("?" for _ in ids)
        for attempt in range(3):
            try:
                con.execute("BEGIN IMMEDIATE")
                cur = con.execute(f"DELETE FROM records WHERE record_id IN ({marks})", tuple(ids))
                con.commit()
                break
            except sqlite3.OperationalError as error:
                try:
                    con.rollback()
                except sqlite3.Error:
                    pass
                if attempt == 2:
                    _log(f"records 批次放弃（数据库忙）：{error}")
                    return {"deleted": deleted, "deleted_bytes": deleted_bytes, "batches": batches}
                time.sleep(1.0)
        deleted += cur.rowcount
        deleted_bytes += size
        batches += 1
        time.sleep(0.05)
    return {"deleted": deleted, "deleted_bytes": deleted_bytes, "batches": batches}


def _delete_states(con: sqlite3.Connection, plan: dict, *, dry_run: bool) -> dict:
    if not plan["attempt_states"]:
        return {"deleted": 0, "deleted_bytes": 0}
    cutoff = plan["cutoff"]
    rows = con.execute(
        """SELECT sid FROM records GROUP BY sid
           HAVING MAX(json_extract(payload,'$.receivedAt')) < ?""",
        (cutoff,),
    ).fetchall()
    sids = [row[0] for row in rows if row[0]]
    if not sids:
        return {"deleted": 0, "deleted_bytes": 0}
    marks = ",".join("?" for _ in sids)
    total = con.execute(
        f"""SELECT COUNT(*), COALESCE(SUM(length(payload)),0) FROM states
            WHERE key LIKE 'native-attempt:%' AND json_extract(payload,'$.terminal')=1
              AND sid IN ({marks})""",
        tuple(sids),
    ).fetchone()
    if dry_run:
        return {"deleted": int(total[0] or 0), "deleted_bytes": int(total[1] or 0)}
    if not total[0]:
        return {"deleted": 0, "deleted_bytes": 0}
    con.execute("BEGIN IMMEDIATE")
    cur = con.execute(
        f"""DELETE FROM states
            WHERE key LIKE 'native-attempt:%' AND json_extract(payload,'$.terminal')=1
              AND sid IN ({marks})""",
        tuple(sids),
    )
    con.commit()
    return {"deleted": cur.rowcount, "deleted_bytes": int(total[1] or 0)}


def _checkpoint(con: sqlite3.Connection) -> dict:
    result = {"wal_before": _sizes()["wal"]}
    try:
        con.execute("PRAGMA busy_timeout=3000")
        row = con.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        result["passive"] = list(row)
        row = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        result["truncate"] = list(row)
    except sqlite3.Error as error:
        result["error"] = str(error)
    result["wal_after"] = _sizes()["wal"]
    return result


def run_once(keep_hours: int, *, dry_run: bool = False) -> dict:
    _check_paths()
    con = _connect()
    try:
        before = con.execute("PRAGMA freelist_count").fetchone()[0]
        plan = _plan(con, keep_hours)
        _log(
            ("[dry-run] " if dry_run else "")
            + f"保留 {keep_hours}h（cutoff {plan['cutoff']}）："
            f"records {plan['records']} 条 / {plan['record_bytes']/1048576:.1f} MiB，"
            f"terminal attempt states {plan['attempt_states']} 条 / {plan['attempt_state_bytes']/1048576:.1f} MiB，"
            f"当前库 {plan['sizes']['db']/1048576:.0f} MiB + wal {plan['sizes']['wal']/1048576:.0f} MiB"
        )
        records = _delete_states_and_records(con, plan, dry_run=dry_run)
        after = con.execute("PRAGMA freelist_count").fetchone()[0]
        checkpoint = {} if dry_run else _checkpoint(con)
        sizes = _sizes()
        result = {
            "time": _now().isoformat(),
            "dry_run": dry_run,
            "keep_hours": keep_hours,
            "cutoff": plan["cutoff"],
            "boundary_record_id": plan["boundary_record_id"],
            "records_deleted": records["records"]["deleted"],
            "records_bytes": records["records"]["deleted_bytes"],
            "states_deleted": records["states"]["deleted"],
            "states_bytes": records["states"]["deleted_bytes"],
            "records_remaining": con.execute("SELECT COUNT(*) FROM records").fetchone()[0],
            "states_remaining": con.execute("SELECT COUNT(*) FROM states").fetchone()[0],
            "freelist_pages_before": before,
            "freelist_pages_after": after,
            "db_bytes": sizes["db"],
            "wal_bytes": sizes["wal"],
            "checkpoint": checkpoint,
            "duration_s": round(plan.get("duration", 0), 1),
        }
        return result
    finally:
        con.close()


def _delete_states_and_records(con: sqlite3.Connection, plan: dict, *, dry_run: bool) -> dict:
    started = time.monotonic()
    states = _delete_states(con, plan, dry_run=dry_run)
    records = _delete_records(con, plan, dry_run=dry_run)
    plan["duration"] = time.monotonic() - started
    return {"states": states, "records": records}


def _next_run(now: dt.datetime, interval_hours: int) -> dt.datetime:
    return now + dt.timedelta(hours=interval_hours)


def _record_run(result: dict, interval_hours: int) -> dict:
    state = _load_state()
    state["last_run"] = result["time"]
    state["last_result"] = {
        key: result.get(key)
        for key in ("records_deleted", "records_bytes", "states_deleted", "states_bytes",
                    "records_remaining", "db_bytes", "wal_bytes", "dry_run")
    }
    nxt = _next_run(_now(), interval_hours)
    state["next_run"] = nxt.isoformat()
    state["interval_hours"] = interval_hours
    state["keep_hours"] = result["keep_hours"]
    state["pid"] = os.getpid()
    _save_state(state)
    _audit(result)
    return state


def cmd_once(keep_hours: int, interval_hours: int) -> int:
    result = run_once(keep_hours, dry_run=False)
    state = _record_run(result, interval_hours)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"下次执行：{state['next_run']}")
    return 0


def _connect_with_close() -> sqlite3.Connection:
    """dry-run 用：算完立刻关闭，避免留下连接。"""
    _check_paths()
    return _connect()


def cmd_status() -> int:
    state = _load_state()
    sizes = _sizes()
    con = _connect()
    try:
        counts = {
            "records": con.execute("SELECT COUNT(*) FROM records").fetchone()[0],
            "events": con.execute("SELECT COUNT(*) FROM records WHERE kind='event'").fetchone()[0],
            "raw": con.execute("SELECT COUNT(*) FROM records WHERE kind='raw'").fetchone()[0],
            "states": con.execute("SELECT COUNT(*) FROM states").fetchone()[0],
            "freelist_pages": con.execute("PRAGMA freelist_count").fetchone()[0],
        }
    finally:
        con.close()
    print(json.dumps({"state": state, "counts": counts, "sizes": sizes}, ensure_ascii=False, indent=2))
    return 0


def cmd_daemon(keep_hours: int, interval_hours: int) -> int:
    OBS.mkdir(parents=True, exist_ok=True)
    lock = LOCK.open("a+")
    try:
        import fcntl
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (ImportError, OSError):
        _log("已有清理进程在运行，本进程退出")
        return 0
    _log(f"清理守护启动：保留 {keep_hours}h，每 {interval_hours}h 一次，db={DB}")
    while True:
        try:
            state = _load_state()
            now = _now()
            due = True
            raw = state.get("next_run")
            if raw:
                try:
                    due = now >= dt.datetime.fromisoformat(raw)
                except ValueError:
                    due = True
            if due:
                result = run_once(keep_hours, dry_run=False)
                state = _record_run(result, interval_hours)
                _log(
                    f"本轮完成：records -{result['records_deleted']} 条 / "
                    f"{result['records_bytes']/1048576:.1f} MiB，states -{result['states_deleted']} 条 / "
                    f"{result['states_bytes']/1048576:.1f} MiB，库 {result['db_bytes']/1048576:.0f} MiB，"
                    f"wal {result['wal_bytes']/1048576:.0f} MiB；下次 {state['next_run']}"
                )
            else:
                time.sleep(min(30.0, max(1.0, (dt.datetime.fromisoformat(raw) - now).total_seconds())))
        except Exception as error:  # 守护不能因为单轮失败退出
            _log(f"本轮失败：{type(error).__name__}: {error}")
            time.sleep(60)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="thinking 观测档案定时清理")
    parser.add_argument("--once", action="store_true", help="立即执行一轮并记录")
    parser.add_argument("--dry-run", action="store_true", help="只统计不删除")
    parser.add_argument("--status", action="store_true", help="显示状态")
    parser.add_argument("--daemon", action="store_true", help="常驻定时执行")
    parser.add_argument("--keep-hours", type=int, default=DEFAULT_KEEP_HOURS)
    parser.add_argument("--interval-hours", type=int, default=DEFAULT_INTERVAL_HOURS)
    args = parser.parse_args(argv)
    if args.status:
        return cmd_status()
    if args.daemon:
        return cmd_daemon(args.keep_hours, args.interval_hours)
    if args.dry_run:
        con = _connect_with_close()
        try:
            print(json.dumps(_plan(con, args.keep_hours), ensure_ascii=False, indent=2))
        finally:
            con.close()
        return 0
    if args.once:
        return cmd_once(args.keep_hours, args.interval_hours)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
