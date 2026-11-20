"""到站台账：批量入账、修订/撤销规则、月汇总与持久化。

业务规则（与需求逐条对应）：

- 批量提交归档流水号、修订号、归属月、线路、车站，也可提交撤销；
  同一流水号每批限一条（在 Pydantic 模型层校验）。
- 每个流水号只按最高修订号计数；撤销后流水记录与修订号保留，只是不再计数。
- 低于现行修订号的报送、与现行记录完全相同的原样重送，均跳过不改账。
- 与现行记录同修订号但内容不同（同版异文），整批退回，不写任何数据。
- 跨月更正（更高修订号改归属月）在同一事务内从旧月扣一笔、向新月加一笔。
- 流水、受影响月份的月汇总、全局版号在同一个 SQLite 事务内提交；
  失败则回滚，原账保留。数据落盘，进程重启后仍可使用。
- 月汇总独立维护，联发预检只读汇总与入账标记，无需扫描流水。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

# 跳过原因（机器可读码）
SKIP_LOWER_REVISION = "lower_revision"
SKIP_IDENTICAL_RESUBMISSION = "identical_resubmission"
SKIP_ALREADY_REVOKED = "already_revoked"

# 整批退回原因
CONFLICT_SAME_REVISION = "same_revision_conflict"
CONFLICT_SERIAL_NOT_FOUND = "serial_not_found"


class LedgerConflict(Exception):
    """整批必须退回的冲突；携带错误码与涉事流水号。"""

    def __init__(self, code: str, serial_no: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.serial_no = serial_no
        self.message = message


@dataclass(frozen=True)
class BatchItem:
    """批内一条报送（已通过 Pydantic 形状校验）。"""

    serial_no: str
    revision: int
    action: str  # "upsert" | "revoke"
    month: str | None
    line_id: str | None
    station_id: str | None


@dataclass(frozen=True)
class SerialState:
    """某流水号的现行（最高修订号）记录。"""

    serial_no: str
    revision: int
    status: str  # "active" | "revoked"
    month: str | None
    line_id: str | None
    station_id: str | None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS serial_records (
    serial_no  TEXT PRIMARY KEY,
    revision   INTEGER NOT NULL,
    status     TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
    month      TEXT,
    line_id    TEXT,
    station_id TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS monthly_summary (
    month      TEXT NOT NULL,
    line_id    TEXT NOT NULL,
    station_id TEXT NOT NULL,
    count      INTEGER NOT NULL CHECK (count >= 0),
    PRIMARY KEY (month, line_id, station_id)
);
CREATE TABLE IF NOT EXISTS booked_months (
    month TEXT PRIMARY KEY
);
"""


class LedgerStore:
    """台账存储。单连接配合进程内锁，保证批量提交的原子性。"""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('ledger_version', '0')"
        )
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ 读取

    def get_version(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'ledger_version'"
            ).fetchone()
            return int(row["value"])

    def is_booked(self, month: str) -> bool:
        """该月份是否曾入账（清空后仍为已入账）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM booked_months WHERE month = ?", (month,)
            ).fetchone()
            return row is not None

    def get_serial(self, serial_no: str) -> SerialState | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT serial_no, revision, status, month, line_id, station_id "
                "FROM serial_records WHERE serial_no = ?",
                (serial_no,),
            ).fetchone()
            if row is None:
                return None
            return SerialState(**dict(row))

    def list_serials(self) -> list[SerialState]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT serial_no, revision, status, month, line_id, station_id "
                "FROM serial_records ORDER BY serial_no"
            ).fetchall()
            return [SerialState(**dict(r)) for r in rows]

    def get_month_summary(self, month: str) -> list[dict[str, Any]]:
        """返回该月汇总行（含 count=0 的行；未入账或清空时为空列表）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT line_id, station_id, count FROM monthly_summary "
                "WHERE month = ? ORDER BY line_id, station_id",
                (month,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_arrival_map(self, month: str) -> dict[tuple[str, str], int]:
        """预检专用：读取 (线路, 车站) -> 到站次数，只查月汇总，不扫流水。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT line_id, station_id, count FROM monthly_summary WHERE month = ?",
                (month,),
            ).fetchall()
            return {(r["line_id"], r["station_id"]): r["count"] for r in rows}

    # ------------------------------------------------------------------ 写入

    def submit_batch(self, items: list[BatchItem]) -> dict[str, Any]:
        """提交一批报送。

        先基于当前账做整包校验：任何一条触发整批退回（同版异文、撤销不存在
        的流水号），直接抛 LedgerConflict，不写库。校验通过后在单个事务内
        落流水、改月汇总、递增版号，任一语句失败则回滚。
        """
        with self._lock:
            serials = [it.serial_no for it in items]
            placeholders = ",".join("?" for _ in serials)
            rows = self._conn.execute(
                "SELECT serial_no, revision, status, month, line_id, station_id "
                f"FROM serial_records WHERE serial_no IN ({placeholders})",
                serials,
            ).fetchall()
            existing = {r["serial_no"]: SerialState(**dict(r)) for r in rows}

            # 第一阶段：纯内存判定，产出执行计划或整批退回
            plan: list[tuple[str, BatchItem, SerialState | None]] = []
            skipped: list[dict[str, Any]] = []
            for item in items:
                ex = existing.get(item.serial_no)
                if ex is None:
                    if item.action == "revoke":
                        raise LedgerConflict(
                            CONFLICT_SERIAL_NOT_FOUND,
                            item.serial_no,
                            f"流水号 {item.serial_no} 从未入账，不能撤销",
                        )
                    plan.append(("insert", item, None))
                    continue

                if item.revision < ex.revision:
                    skipped.append(
                        {
                            "serial_no": item.serial_no,
                            "revision": item.revision,
                            "reason": SKIP_LOWER_REVISION,
                        }
                    )
                    continue

                if item.revision == ex.revision:
                    if item.action == "revoke":
                        if ex.status == "revoked":
                            skipped.append(
                                {
                                    "serial_no": item.serial_no,
                                    "revision": item.revision,
                                    "reason": SKIP_ALREADY_REVOKED,
                                }
                            )
                        else:
                            plan.append(("revoke", item, ex))
                        continue
                    # upsert 且同修订号：内容一致=原样重送，不一致=同版异文
                    same_content = (
                        ex.month == item.month
                        and ex.line_id == item.line_id
                        and ex.station_id == item.station_id
                    )
                    if same_content:
                        skipped.append(
                            {
                                "serial_no": item.serial_no,
                                "revision": item.revision,
                                "reason": SKIP_IDENTICAL_RESUBMISSION,
                            }
                        )
                    else:
                        raise LedgerConflict(
                            CONFLICT_SAME_REVISION,
                            item.serial_no,
                            (
                                f"流水号 {item.serial_no} 现行修订号 {ex.revision} "
                                "已存在且内容不同，同版异文，整批退回"
                            ),
                        )
                    continue

                # item.revision > ex.revision
                if item.action == "revoke":
                    if ex.status == "active":
                        plan.append(("revoke", item, ex))
                    else:
                        plan.append(("revoke_bump", item, ex))
                elif ex.status == "active":
                    plan.append(("replace", item, ex))
                else:
                    plan.append(("reactivate", item, ex))

            if not plan:
                return {
                    "ledger_version": self.get_version(),
                    "applied": [],
                    "skipped": skipped,
                    "affected_months": [],
                }

            # 第二阶段：单事务落库
            old_version = self.get_version()
            applied: list[dict[str, Any]] = []
            affected: set[str] = set()
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                now = datetime.now(timezone.utc).isoformat()
                for kind, item, ex in plan:
                    applied.append(
                        self._apply_one(kind, item, ex, now, affected)
                    )
                new_version = old_version + 1
                self._conn.execute(
                    "UPDATE meta SET value = ? WHERE key = 'ledger_version'",
                    (str(new_version),),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

            return {
                "ledger_version": new_version,
                "applied": applied,
                "skipped": skipped,
                "affected_months": sorted(affected),
            }

    # -------------------------------------------------------------- 事务内细节

    def _apply_one(
        self,
        kind: str,
        item: BatchItem,
        ex: SerialState | None,
        now: str,
        affected: set[str],
    ) -> dict[str, Any]:
        """在已开启的事务内执行一条计划，返回生效明细。"""
        detail: dict[str, Any]
        if kind == "insert":
            assert item.month and item.line_id and item.station_id
            self._conn.execute(
                "INSERT INTO serial_records"
                "(serial_no, revision, status, month, line_id, station_id, updated_at)"
                " VALUES (?, ?, 'active', ?, ?, ?, ?)",
                (
                    item.serial_no,
                    item.revision,
                    item.month,
                    item.line_id,
                    item.station_id,
                    now,
                ),
            )
            self._adjust(item.month, item.line_id, item.station_id, 1)
            self._book(item.month)
            affected.add(item.month)
            detail = {
                "effect": "insert",
                "month": item.month,
                "line_id": item.line_id,
                "station_id": item.station_id,
            }
        elif kind == "replace":
            assert ex is not None and item.month and item.line_id and item.station_id
            # 跨月/跨线站更正：旧位置扣一笔，新位置加一笔
            assert ex.month and ex.line_id and ex.station_id
            self._adjust(ex.month, ex.line_id, ex.station_id, -1)
            affected.add(ex.month)
            self._adjust(item.month, item.line_id, item.station_id, 1)
            self._book(item.month)
            affected.add(item.month)
            self._conn.execute(
                "UPDATE serial_records SET revision = ?, status = 'active', "
                "month = ?, line_id = ?, station_id = ?, updated_at = ? "
                "WHERE serial_no = ?",
                (
                    item.revision,
                    item.month,
                    item.line_id,
                    item.station_id,
                    now,
                    item.serial_no,
                ),
            )
            detail = {
                "effect": "replace",
                "old": {
                    "month": ex.month,
                    "line_id": ex.line_id,
                    "station_id": ex.station_id,
                },
                "new": {
                    "month": item.month,
                    "line_id": item.line_id,
                    "station_id": item.station_id,
                },
            }
        elif kind == "reactivate":
            # 撤销后用更高修订号重新入账：原记录不占账，只加新笔
            assert item.month and item.line_id and item.station_id
            self._adjust(item.month, item.line_id, item.station_id, 1)
            self._book(item.month)
            affected.add(item.month)
            self._conn.execute(
                "UPDATE serial_records SET revision = ?, status = 'active', "
                "month = ?, line_id = ?, station_id = ?, updated_at = ? "
                "WHERE serial_no = ?",
                (
                    item.revision,
                    item.month,
                    item.line_id,
                    item.station_id,
                    now,
                    item.serial_no,
                ),
            )
            detail = {
                "effect": "reactivate",
                "month": item.month,
                "line_id": item.line_id,
                "station_id": item.station_id,
            }
        elif kind == "revoke":
            assert ex is not None and ex.status == "active"
            assert ex.month and ex.line_id and ex.station_id
            self._adjust(ex.month, ex.line_id, ex.station_id, -1)
            affected.add(ex.month)
            self._conn.execute(
                "UPDATE serial_records SET revision = ?, status = 'revoked', "
                "updated_at = ? WHERE serial_no = ?",
                (item.revision, now, item.serial_no),
            )
            detail = {
                "effect": "revoke",
                "month": ex.month,
                "line_id": ex.line_id,
                "station_id": ex.station_id,
            }
        elif kind == "revoke_bump":
            # 已撤销流水号的更高修订号撤销：只保留新修订号，不动汇总
            assert ex is not None and ex.status == "revoked"
            self._conn.execute(
                "UPDATE serial_records SET revision = ?, updated_at = ? "
                "WHERE serial_no = ?",
                (item.revision, now, item.serial_no),
            )
            detail = {"effect": "revoke_revision_bump"}
        else:  # pragma: no cover - 防御性分支
            raise RuntimeError(f"未知计划类型: {kind}")

        return {
            "serial_no": item.serial_no,
            "revision": item.revision,
            "action": item.action,
            **detail,
        }

    def _adjust(
        self, month: str, line_id: str, station_id: str, delta: int
    ) -> None:
        """月汇总加减一笔；count 归零时保留行（已入账清空按零次处理）。"""
        row = self._conn.execute(
            "SELECT count FROM monthly_summary "
            "WHERE month = ? AND line_id = ? AND station_id = ?",
            (month, line_id, station_id),
        ).fetchone()
        current = row["count"] if row is not None else 0
        new_count = current + delta
        if new_count < 0:  # pragma: no cover - 规则保证不会发生
            raise RuntimeError(
                f"月汇总出现负数: {month}/{line_id}/{station_id}: {current}{delta}"
            )
        if row is not None:
            self._conn.execute(
                "UPDATE monthly_summary SET count = ? "
                "WHERE month = ? AND line_id = ? AND station_id = ?",
                (new_count, month, line_id, station_id),
            )
        else:
            self._conn.execute(
                "INSERT INTO monthly_summary(month, line_id, station_id, count) "
                "VALUES (?, ?, ?, ?)",
                (month, line_id, station_id, new_count),
            )

    def _book(self, month: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO booked_months(month) VALUES (?)", (month,)
        )
