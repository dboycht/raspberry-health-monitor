"""SQLite 历史存储（树莓派自带 sqlite3，零额外依赖）。

三张表：
- ``readings``：环境/体温等**周期性数值**（心率血氧另存，因为它是"事件式"数据）
- ``vitals``：心率血氧（含是否检测到手指——**没贴手指时值必须存 NULL**）
- ``alarms``：报警事件（含严重度与是否已推送到手机）

设计要点
--------
1. **所有时间戳都是 Unix 秒**（REAL），显示时才转本地时间，避免时区混乱；
2. **连接不跨线程共享**：SQLite 默认禁止跨线程使用连接。本模块每次操作**短连接**
   （``with self._connect() as conn``），这样采集线程与 HTTP 线程可以安全并发；
3. **写入绝不影响采集**：存储失败只记日志、不抛给采集循环（历史库坏掉不该让监护停摆）。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from ..hal.models import AlarmEvent, AmbientSample, MotionSample, PrecisionTempSample, VitalSignsSample

_LOG = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    ts            REAL NOT NULL,
    device        TEXT NOT NULL,
    metric        TEXT NOT NULL,
    value         REAL,
    unit          TEXT,
    ok            INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings (ts);
CREATE INDEX IF NOT EXISTS idx_readings_metric ON readings (metric, ts);

CREATE TABLE IF NOT EXISTS vitals (
    ts              REAL NOT NULL,
    device          TEXT NOT NULL,
    heart_rate_bpm  REAL,
    spo2_percent    REAL,
    finger_detected INTEGER NOT NULL DEFAULT 0,
    quality         REAL
);
CREATE INDEX IF NOT EXISTS idx_vitals_ts ON vitals (ts);

CREATE TABLE IF NOT EXISTS alarms (
    ts          REAL NOT NULL,
    code        TEXT NOT NULL,
    severity    INTEGER NOT NULL,
    message     TEXT NOT NULL,
    value       REAL,
    unit        TEXT,
    source      TEXT,
    detail      TEXT,
    pushed      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alarms_ts ON alarms (ts);
CREATE INDEX IF NOT EXISTS idx_alarms_code ON alarms (code, ts);
"""


class Store:
    """历史数据存储。

    Args:
        path: 数据库文件路径。``":memory:"`` 表示内存库（测试用）。
        retention_days: 数据保留天数，超过的旧数据在 :meth:`prune` 时删除。
    """

    def __init__(self, path: str | Path = "data/history.db", retention_days: float = 30.0) -> None:
        self.path = str(path)
        self.retention_days = float(retention_days)
        self._lock = threading.Lock()
        self._write_errors = 0
        # ⚠️ ``:memory:`` 库是"每连接一份"的：如果沿用"每次短连接"的策略，
        # 每个新连接都会看到一个**全新的空库**，于是建表白建、写入全部失败。
        # 因此内存库必须保持**一条常驻连接**；文件库才用短连接（跨线程更安全）。
        self._persistent: Optional[sqlite3.Connection] = None
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        else:
            self._persistent = sqlite3.connect(self.path, timeout=5.0)
            self._persistent.row_factory = sqlite3.Row
        self._init_schema()

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """取得一个可用连接。

        - 文件库：每次操作一条**短连接**（SQLite 连接不能跨线程共享）；
        - 内存库：复用同一条常驻连接（否则会看到空库）。
        """
        if self._persistent is not None:
            try:
                yield self._persistent
                self._persistent.commit()
            except Exception:
                self._persistent.rollback()
                raise
            return
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def save_sample(self, sample: Any) -> int:
        """按样本类型自动分派写入，返回写入行数（失败返回 0 并记日志）。

        **绝不抛异常给采集循环**——历史库写不进去不该让监护停摆。
        """
        try:
            if isinstance(sample, VitalSignsSample):
                return self._save_vitals(sample)
            if isinstance(sample, AmbientSample):
                rows = 0
                if sample.temperature_c is not None:
                    rows += self._save_reading(sample.ts, sample.device, "ambient_temp_c", sample.temperature_c, "°C", sample.ok)
                if sample.humidity_percent is not None:
                    rows += self._save_reading(sample.ts, sample.device, "humidity_percent", sample.humidity_percent, "%", sample.ok)
                return rows
            if isinstance(sample, PrecisionTempSample):
                rows = 0
                if sample.temperature_c is not None:
                    rows += self._save_reading(sample.ts, sample.device, "body_temp_c", sample.temperature_c, "°C", sample.ok)
                if sample.voltage_v is not None:
                    rows += self._save_reading(sample.ts, sample.device, "tmp36_voltage_v", sample.voltage_v, "V", sample.ok)
                return rows
            if isinstance(sample, MotionSample):
                return self._save_reading(
                    sample.ts, sample.device, "motion",
                    1.0 if sample.detected else 0.0, "bool", sample.ok,
                )
            return 0
        except Exception as exc:  # noqa: BLE001 - 存储失败必须降级为"记日志"
            self._write_errors += 1
            _LOG.warning("写入历史库失败（已忽略，不影响监护）：%s", exc)
            return 0

    def _save_reading(self, ts: float, device: str, metric: str, value: Optional[float], unit: str, ok: bool) -> int:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO readings (ts, device, metric, value, unit, ok) VALUES (?,?,?,?,?,?)",
                (ts, device, metric, value, unit, 1 if ok else 0),
            )
            return 1

    def _save_vitals(self, v: VitalSignsSample) -> int:
        # ⚠️ 没检测到手指时值必须是 NULL：存 0 会让历史曲线出现"心率掉到 0"的假象
        hr = v.heart_rate_bpm if v.finger_detected else None
        spo2 = v.spo2_percent if v.finger_detected else None
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO vitals (ts, device, heart_rate_bpm, spo2_percent, finger_detected, quality)"
                " VALUES (?,?,?,?,?,?)",
                (v.ts, v.device, hr, spo2, 1 if v.finger_detected else 0, v.quality),
            )
            return 1

    def save_alarm(self, event: AlarmEvent, pushed: bool = False) -> int:
        """写入一条报警事件。"""
        import json

        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    "INSERT INTO alarms (ts, code, severity, message, value, unit, source, detail, pushed)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        event.ts, event.code.value, int(event.severity), event.message,
                        event.value, event.unit, event.source,
                        json.dumps(event.detail, ensure_ascii=False), 1 if pushed else 0,
                    ),
                )
                return 1
        except Exception as exc:  # noqa: BLE001
            self._write_errors += 1
            _LOG.warning("写入报警历史失败（已忽略）：%s", exc)
            return 0

    def mark_pushed(self, ts: float, code: str) -> int:
        """把某条报警标记为"已推送到手机"。"""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE alarms SET pushed = 1 WHERE ts = ? AND code = ?", (ts, code)
            )
            return cur.rowcount

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def recent_readings(self, metric: str, limit: int = 100, since: Optional[float] = None) -> List[Dict[str, Any]]:
        """取某指标最近 ``limit`` 条（按时间正序返回，方便直接画曲线）。"""
        sql = "SELECT ts, value, ok, device FROM readings WHERE metric = ?"
        params: List[Any] = [metric]
        if since is not None:
            sql += " AND ts >= ?"
            params.append(since)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(int(limit))
        with self._lock, self._connect() as conn:
            rows = [dict(r) for r in conn.execute(sql, params)]
        rows.reverse()
        return rows

    def recent_vitals(self, limit: int = 100, since: Optional[float] = None) -> List[Dict[str, Any]]:
        """取最近的心率血氧记录（按时间正序）。"""
        sql = "SELECT ts, heart_rate_bpm, spo2_percent, finger_detected, quality FROM vitals"
        params: List[Any] = []
        if since is not None:
            sql += " WHERE ts >= ?"
            params.append(since)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(int(limit))
        with self._lock, self._connect() as conn:
            rows = [dict(r) for r in conn.execute(sql, params)]
        rows.reverse()
        return rows

    def recent_alarms(self, limit: int = 50, since: Optional[float] = None) -> List[Dict[str, Any]]:
        """取最近的报警事件（按时间**倒序**，最近的在前）。"""
        import json

        sql = "SELECT * FROM alarms"
        params: List[Any] = []
        if since is not None:
            sql += " WHERE ts >= ?"
            params.append(since)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(int(limit))
        with self._lock, self._connect() as conn:
            rows = [dict(r) for r in conn.execute(sql, params)]
        for row in rows:
            try:
                row["detail"] = json.loads(row.get("detail") or "{}")
            except json.JSONDecodeError:
                row["detail"] = {}
            row["pushed"] = bool(row.get("pushed"))
        return rows

    def count(self, table: str = "readings") -> int:
        """表内行数（自检与测试用）。表名白名单校验，防止 SQL 注入。"""
        if table not in ("readings", "vitals", "alarms"):
            raise ValueError(f"未知表名 {table!r}")
        with self._lock, self._connect() as conn:
            row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
        return int(row["c"])

    # ------------------------------------------------------------------
    # 维护
    # ------------------------------------------------------------------

    def prune(self, now: Optional[float] = None) -> int:
        """删除超过保留期的旧数据，返回删除行数。"""
        cutoff = (now if now is not None else time.time()) - self.retention_days * 86400.0
        removed = 0
        with self._lock, self._connect() as conn:
            for table in ("readings", "vitals", "alarms"):
                cur = conn.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
                removed += cur.rowcount
        return removed

    def stats(self) -> Dict[str, Any]:
        """库状态（给自检与调试用）。"""
        return {
            "path": self.path,
            "retention_days": self.retention_days,
            "readings": self.count("readings"),
            "vitals": self.count("vitals"),
            "alarms": self.count("alarms"),
            "write_errors": self._write_errors,
        }

    def close(self) -> None:
        """关闭常驻连接（只有内存库有；文件库是短连接，无需关闭）。"""
        if self._persistent is not None:
            self._persistent.close()
            self._persistent = None

    def drop_all_tables(self) -> None:
        """**仅供测试**：把表全部删掉，用于验证"存储故障不影响采集"。"""
        with self._lock, self._connect() as conn:
            conn.executescript("DROP TABLE IF EXISTS readings; DROP TABLE IF EXISTS vitals; DROP TABLE IF EXISTS alarms;")


__all__ = ["Store"]
