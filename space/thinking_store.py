"""Transactional, append-only native thinking archive; no model/device access.

The database and rollback journal count toward the configured volume quota.
Normal ingestion leaves space for a durable capacity-pause event. Pausing never
deletes records, resets sequence numbers, or silently trims the native history.
"""
from __future__ import annotations

import contextlib
import datetime as _datetime
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid


class ThinkingCapacityError(RuntimeError):
    code = "ARCHIVE_CAPACITY_REACHED"

    def __init__(self, message="Thinking archive capacity reached", *, used_bytes=0, max_bytes=0):
        super().__init__(message)
        self.used_bytes = used_bytes
        self.max_bytes = max_bytes
        self.requiresPause = True


def _json(value):
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        # A future raw JSON extension can legally contain an escaped unpaired
        # surrogate. Retain that value as an escape instead of losing the frame.
        return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    return text


def _now():
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat()


def _key(value, name, limit=2048):
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError(f"Invalid {name}")
    return value


class ThinkingStore:
    def __init__(self, root, max_bytes=8 * 1024**3, *, journal_mode="DELETE"):
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 128 * 1024:
            raise ValueError("max_bytes must be an integer of at least 128 KiB")
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "thinking.sqlite3"
        self.max_bytes = max_bytes
        self.reserve_bytes = min(2 * 1024**2, max(64 * 1024, max_bytes // 32))
        if journal_mode not in ("DELETE", "WAL"):
            raise ValueError("Unsupported thinking archive journal mode")
        self.journal_mode = journal_mode
        self._wal_anchor = None
        self._checkpoint_lock = threading.Lock()
        self._last_checkpoint = 0.0
        with self._connection() as con:
            actual = con.execute("PRAGMA journal_mode=" + journal_mode).fetchone()[0]
            if actual.lower() != journal_mode.lower():
                raise RuntimeError("Requested thinking journal mode unavailable")
            if journal_mode == "WAL":
                # Retain an idle connection so closing a per-transaction writer
                # does not checkpoint/fsync the whole DB after every batch.
                # FULL remains enabled on every writer; committed raw/state/
                # events stay durable before publication. WAL/SHM are counted
                # by the existing whole-directory capacity guard.
                self._wal_anchor = sqlite3.connect(str(self.path), timeout=15, check_same_thread=False)
                self._wal_anchor.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
            con.execute("BEGIN IMMEDIATE")
            con.executescript("""
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions(
                    sid TEXT PRIMARY KEY, last_seq INTEGER NOT NULL DEFAULT 0,
                    last_raw_seq INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS states(
                    sid TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(sid,key));
                CREATE TABLE IF NOT EXISTS records(
                    record_id INTEGER PRIMARY KEY AUTOINCREMENT, sid TEXT NOT NULL,
                    kind TEXT NOT NULL, seq INTEGER NOT NULL, raw_key TEXT,
                    dedupe_key TEXT, payload TEXT NOT NULL,
                    UNIQUE(sid,kind,seq), UNIQUE(sid,raw_key), UNIQUE(sid,dedupe_key));
                CREATE INDEX IF NOT EXISTS records_sid_record ON records(sid,record_id);
            """)
            # executescript commits its opening transaction; take a new writer
            # lock before creating the stable instance identity.
            con.execute("BEGIN IMMEDIATE")
            con.execute("INSERT OR IGNORE INTO meta VALUES('sourceInstanceId',?)", (str(uuid.uuid4()),))
            con.execute("INSERT OR IGNORE INTO meta VALUES('paused','false')")
            self.sourceInstanceId = con.execute("SELECT value FROM meta WHERE key='sourceInstanceId'").fetchone()[0]
            self.source_instance_id = self.sourceInstanceId
            con.commit()

    def close(self):
        anchor, self._wal_anchor = self._wal_anchor, None
        if anchor is not None:
            anchor.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def _connection(self):
        con = sqlite3.connect(str(self.path), timeout=15, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=15000")
        con.execute("PRAGMA synchronous=FULL")
        if self.journal_mode == 'WAL':
            # FULL still syncs the committed WAL before publication. SQLite's
            # default auto-checkpoint also copies/fsyncs the main DB inside a
            # busy ingest COMMIT. Collectors explicitly checkpoint only after
            # every receive queue (including in-flight writes) is empty.
            con.execute("PRAGMA wal_autocheckpoint=0")
        return contextlib.closing(con)

    def checkpoint_idle(self):
        """Best-effort PASSIVE checkpoint; caller must establish writer idle.

        No waiting for readers, no truncation, and no weakening of WAL/FULL.
        WAL/SHM keep counting toward capacity if a reader prevents progress.
        """
        if self.journal_mode != 'WAL':
            return None
        now = time.monotonic()
        if now - self._last_checkpoint < 1.0:
            return None
        try:
            if Path(str(self.path) + '-wal').stat().st_size < 4 * 1024**2:
                return None
        except FileNotFoundError:
            return None
        if not self._checkpoint_lock.acquire(blocking=False):
            return None
        try:
            self._last_checkpoint = now
            with self._connection() as con:
                con.execute('PRAGMA busy_timeout=0')
                busy, frames, copied = con.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone()
            return {'busy': bool(busy), 'walFrames': frames, 'checkpointedFrames': copied,
                    'elapsedMs': round((time.monotonic() - now) * 1000, 3)}
        finally:
            self._checkpoint_lock.release()

    def _used_bytes(self, con=None):
        total = 0
        for directory, _dirs, files in os.walk(self.root):
            for name in files:
                filename = Path(directory) / name
                try:
                    total += filename.stat().st_size
                except FileNotFoundError:
                    pass  # A completed rollback journal may disappear mid-scan.
        if con is not None:
            projected = con.execute("PRAGMA page_count").fetchone()[0] * con.execute("PRAGMA page_size").fetchone()[0]
            try:
                physical = self.path.stat().st_size
            except FileNotFoundError:
                physical = 0
            total += max(0, projected - physical)
        return total

    @staticmethod
    def _paused(con):
        return con.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] == "true"

    def _capacity_error(self, con=None):
        return ThinkingCapacityError(used_bytes=self._used_bytes(con), max_bytes=self.max_bytes)

    def _check_capacity(self, con, extra_bytes=0):
        # Account for temporary page/journal growth before sending SQLite a
        # large value. The post-operation check also uses its logical page count.
        used = self._used_bytes(con)
        if used + extra_bytes > self.max_bytes - self.reserve_bytes:
            raise self._capacity_error(con)
        return used

    def _pause(self, con, sid):
        if self._paused(con):
            return
        con.execute("INSERT OR IGNORE INTO sessions(sid) VALUES(?)", (sid,))
        con.execute("UPDATE meta SET value='true' WHERE key='paused'")
        con.execute("INSERT OR REPLACE INTO meta VALUES('pauseReason','ARCHIVE_CAPACITY_REACHED')")
        con.execute("INSERT OR REPLACE INTO meta VALUES('pausedAt',?)", (_now(),))
        tx = _ThinkingTransaction(self, con, sid, bypass_quota=True)
        received_at = _now()
        tx.emit({"schemaVersion": 1, "kind": "status", "status": "paused", "code": "ARCHIVE_CAPACITY_REACHED",
                 "requiresPause": True, "at": received_at, "receivedAt": received_at,
                 "time": int(_datetime.datetime.now(_datetime.timezone.utc).timestamp() * 1000),
                 "text": "", "complete": True, "stream": False, "origin": "native-live",
                 "model": None, "provider": None, "attemptId": None, "turn": None, "step": None,
                 "revision": None, "chunkIndex": None, "blockIndex": None, "nativeSeq": None,
                 "source": "archive-capacity-guard",
                 "message": "Archive capacity reached; stop collection and further source activity. Original history retained."},
                dedupe_key=None)

    @contextlib.contextmanager
    def transaction(self, sid):
        sid = _key(sid, "session id", 512)
        with self._connection() as con:
            con.execute("BEGIN IMMEDIATE")
            if self._paused(con):
                con.rollback()
                raise self._capacity_error()
            con.execute("SAVEPOINT ingest")
            try:
                con.execute("INSERT OR IGNORE INTO sessions(sid) VALUES(?)", (sid,))
                tx = _ThinkingTransaction(self, con, sid, used_bytes=self._check_capacity(con))
                yield tx
                self._check_capacity(con)
                con.execute("RELEASE ingest")
                con.commit()
            except ThinkingCapacityError:
                # Keep BEGIN IMMEDIATE's writer exclusion while rolling back
                # raw/state/events and replacing them with the reserved status.
                con.execute("ROLLBACK TO ingest")
                con.execute("RELEASE ingest")
                self._pause(con, sid)
                con.commit()
                raise self._capacity_error()
            except sqlite3.DatabaseError as error:
                con.rollback()
                if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL:
                    con.execute("BEGIN IMMEDIATE")
                    self._pause(con, sid)
                    con.commit()
                    raise self._capacity_error() from error
                raise
            except BaseException:
                con.rollback()
                raise

    def info(self, sid, *, include_counts=True):
        sid = _key(sid, "session id", 512)
        with self._connection() as con:
            row = con.execute("SELECT * FROM sessions WHERE sid=?", (sid,)).fetchone()
            counts = (dict(con.execute("SELECT kind,COUNT(*) FROM records WHERE sid=? GROUP BY kind", (sid,)).fetchall())
                      if include_counts else None)
            paused = self._paused(con)
            reason = con.execute("SELECT value FROM meta WHERE key='pauseReason'").fetchone()
        used = self._used_bytes()
        return {"sourceInstanceId": self.sourceInstanceId, "sessionId": sid, "sid": sid,
                "lastSeq": row["last_seq"] if row else 0, "latestSeq": row["last_seq"] if row else 0,
                "lastRawSeq": row["last_raw_seq"] if row else 0,
                "eventCount": counts.get("event", 0) if counts is not None else None,
                "rawCount": counts.get("raw", 0) if counts is not None else None, "countsIncluded": include_counts,
                "paused": paused, "pauseReason": reason[0] if reason else None,
                "requiresPause": paused, "bytesUsed": used, "usedBytes": used,
                "maxBytes": self.max_bytes, "remainingBytes": max(0, self.max_bytes - used),
                "reserveBytes": self.reserve_bytes,
                "remainingRawBytes": max(0, self.max_bytes - self.reserve_bytes - used)}

    def get_state(self, sid, key, default=None):
        """Read committed normalizer state even after a capacity pause."""
        sid, key = _key(sid, "session id", 512), _key(key, "state key")
        with self._connection() as con:
            row = con.execute("SELECT payload FROM states WHERE sid=? AND key=?", (sid, key)).fetchone()
        return json.loads(row[0]) if row else default

    def page(self, sid, after=0, limit=200):
        sid = _key(sid, "session id", 512)
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValueError("after must be a nonnegative sequence")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._connection() as con:
            con.execute("BEGIN")
            session = con.execute("SELECT last_seq FROM sessions WHERE sid=?", (sid,)).fetchone()
            latest = session[0] if session else 0
            oldest = con.execute("SELECT MIN(seq) FROM records WHERE sid=? AND kind='event'", (sid,)).fetchone()[0]
            rows = con.execute("SELECT seq,payload FROM records WHERE sid=? AND kind='event' AND seq>? ORDER BY seq LIMIT ?", (sid, after, limit + 1)).fetchall()
            has_more = len(rows) > limit
            rows = rows[:limit]
            paused = self._paused(con)
            reason = con.execute("SELECT value FROM meta WHERE key='pauseReason'").fetchone()
            con.commit()
        expected, gaps = after + 1, []
        for row in rows:
            if row["seq"] > expected:
                gaps.append({"from": expected, "to": row["seq"] - 1})
            expected = row["seq"] + 1
        if not rows and after < latest:
            gaps.append({"from": after + 1, "to": latest})
        elif rows and not has_more and rows[-1]["seq"] < latest:
            gaps.append({"from": rows[-1]["seq"] + 1, "to": latest})
        ahead = after > latest
        return {"sourceInstanceId": self.sourceInstanceId, "sessionId": sid, "events": [json.loads(row["payload"]) for row in rows],
                "nextSeq": rows[-1]["seq"] if rows else after,
                "oldestAvailableSeq": oldest, "latestSeq": latest, "lastSeq": latest,
                "hasMore": has_more, "gap": bool(gaps or ahead),
                "gapReason": "cursor_ahead_of_archive" if ahead else "missing_archived_sequence" if gaps else None,
                "gapRanges": gaps, "paused": paused, "pauseReason": reason[0] if reason else None,
                "requiresPause": paused}

    def export(self, sid, include_raw=True):
        """Yield JSONL strings, in append order, through one initial high-water mark.

        Small complete SELECT batches release reader locks between yields. The
        archive is append-only, so this remains a stable snapshot without a
        multi-hour read transaction that would stop the live collector writer.
        """
        sid = _key(sid, "session id", 512)
        if not isinstance(include_raw, bool):
            raise ValueError("include_raw must be boolean")
        with self._connection() as con:
            through = con.execute("SELECT COALESCE(MAX(record_id),0) FROM records WHERE sid=?", (sid,)).fetchone()[0]
        yield _json({"kind": "export", "sessionId": sid, "sourceInstanceId": self.sourceInstanceId,
                     "throughRecordId": through, "includeRaw": include_raw}) + "\n"
        cursor = 0
        while cursor < through:
            with self._connection() as con:
                rows = con.execute("SELECT record_id,payload FROM records WHERE sid=? AND record_id>? AND record_id<=? AND (? OR kind='event') ORDER BY record_id LIMIT 100",
                                   (sid, cursor, through, int(include_raw))).fetchall()
            if not rows:
                break
            for row in rows:
                cursor = row["record_id"]
                yield row["payload"] + "\n"


class _ThinkingTransaction:
    def __init__(self, store, con, sid, bypass_quota=False, *, used_bytes=None):
        self.store, self.con, self.sid = store, con, sid
        self.bypass_quota = bypass_quota
        self._quota_remaining = (None if used_bytes is None else
                                 store.max_bytes - store.reserve_bytes - used_bytes)

    def _check(self, payload=""):
        if self.bypass_quota or not payload:
            return
        # Reserve the same conservative payload/page allowance before each
        # write, from a transaction-local budget. Repeating a whole-directory
        # scan and two PRAGMAs for every raw/event/state row made native bursts
        # fall behind. Never share this budget across transactions or readers.
        extra = len(payload.encode("utf-8")) * 2 + 8192
        if self._quota_remaining is None or extra > self._quota_remaining:
            # Close to capacity, refresh the real size instead of pausing on
            # overestimated growth (e.g. replacement states / reused pages).
            used = self.store._check_capacity(self.con, extra)
            self._quota_remaining = self.store.max_bytes - self.store.reserve_bytes - used
        self._quota_remaining -= extra
        # transaction() still scans actual directory + logical DB size before
        # COMMIT. Any unexpected file/page growth rolls back the complete batch
        # and commits only the reserved durable capacity-pause status.

    def get_state(self, key, default=None):
        key = _key(key, "state key")
        row = self.con.execute("SELECT payload FROM states WHERE sid=? AND key=?", (self.sid, key)).fetchone()
        return json.loads(row[0]) if row else default

    def gap_frames(self, code):
        """Read original carrier frames referenced by persisted gap notices.

        Used only to verify an explicit recovery; never fabricate a carrier or
        edit the historical notice/raw row. The transaction sees one snapshot.
        """
        code = _key(code, "gap code", 100)
        rows = self.con.execute("SELECT payload FROM records WHERE sid=? AND kind='event' AND payload LIKE ? ORDER BY seq",
                                (self.sid, '%"code":' + _json(code) + '%')).fetchall()
        frames = {}
        for row in rows:
            event = json.loads(row[0])
            key = event.get('rawKey')
            if event.get('code') != code or not isinstance(key, str) or key in frames:
                continue
            raw = self.con.execute("SELECT payload FROM records WHERE sid=? AND raw_key=?", (self.sid, key)).fetchone()
            if raw:
                frames[key] = json.loads(raw[0])['frame']
        return frames

    def attempt_frames(self, attempt_id):
        """Stream original carriers mentioning an attempt, in archive order."""
        attempt_id = _key(attempt_id, 'attempt id')
        cursor = self.con.execute("SELECT payload FROM records WHERE sid=? AND kind='raw' AND payload LIKE ? ORDER BY seq",
                                  (self.sid, '%"attemptId":' + _json(attempt_id) + '%'))
        for row in cursor:
            raw = json.loads(row[0])
            yield raw['rawKey'], raw['frame']

    def native_events(self, first, last):
        """Original events in a range, including complete reconnect carriers."""
        rows = self.con.execute("SELECT payload FROM records WHERE sid=? AND kind='raw' AND (payload LIKE ? OR payload LIKE ?) ORDER BY seq",
                                (self.sid, '%"type":"event"%', '%"type":"snapshot"%'))
        for row in rows:
            value = json.loads(row[0])['frame']
            events = ([value.get('event')] if value.get('type') == 'event' else
                      [record.get('event', record) for record in value.get('records', []) if isinstance(record, dict)]
                      if value.get('type') == 'snapshot' else [])
            for event in events:
                seq = event.get('seq') if isinstance(event, dict) else None
                if isinstance(seq, int) and not isinstance(seq, bool) and first <= seq <= last:
                    yield event

    def set_state(self, key, value):
        key = _key(key, "state key")
        payload = _json(value)
        self._check(payload)
        self.con.execute("INSERT INTO states(sid,key,payload) VALUES(?,?,?) ON CONFLICT(sid,key) DO UPDATE SET payload=excluded.payload", (self.sid, key, payload))
        self._check()

    def record_raw(self, raw_key, frame):
        raw_key = _key(raw_key, "raw key")
        previous = self.con.execute("SELECT payload FROM records WHERE sid=? AND raw_key=?", (self.sid, raw_key)).fetchone()
        if previous:
            _json(frame)  # Reject non-JSON values even for a reused key.
            if json.loads(previous[0])["frame"] != frame:
                raise ValueError("raw_key already exists with different native data")
            return False
        seq = self.con.execute("SELECT last_raw_seq FROM sessions WHERE sid=?", (self.sid,)).fetchone()[0] + 1
        value = {"kind": "raw", "sourceInstanceId": self.store.sourceInstanceId, "sessionId": self.sid,
                 "rawSeq": seq, "rawKey": raw_key, "receivedAt": _now(), "frame": frame}
        payload = _json(value)
        self._check(payload)
        self.con.execute("INSERT INTO records(sid,kind,seq,raw_key,payload) VALUES(?,'raw',?,?,?)", (self.sid, seq, raw_key, payload))
        self.con.execute("UPDATE sessions SET last_raw_seq=? WHERE sid=?", (seq, self.sid))
        self._check()
        return True

    def emit(self, eventdict, dedupe_key=None):
        if not isinstance(eventdict, dict):
            raise ValueError("event must be a dictionary")
        if dedupe_key is not None:
            dedupe_key = _key(dedupe_key, "event dedupe key")
            if self.con.execute("SELECT 1 FROM records WHERE sid=? AND dedupe_key=?", (self.sid, dedupe_key)).fetchone():
                return None
        for name in ("sessionId", "sid"):
            if name in eventdict and eventdict[name] != self.sid:
                raise ValueError("event session does not match transaction")
        seq = self.con.execute("SELECT last_seq FROM sessions WHERE sid=?", (self.sid,)).fetchone()[0] + 1
        event = dict(eventdict)
        event.update({"sourceInstanceId": self.store.sourceInstanceId, "sessionId": self.sid, "sid": self.sid,
                      "seq": seq, "id": f"{self.store.sourceInstanceId}:{self.sid}:{seq}"})
        payload = _json(event)
        self._check(payload)
        self.con.execute("INSERT INTO records(sid,kind,seq,dedupe_key,payload) VALUES(?,'event',?,?,?)", (self.sid, seq, dedupe_key, payload))
        self.con.execute("UPDATE sessions SET last_seq=? WHERE sid=?", (seq, self.sid))
        self._check()
        return event
