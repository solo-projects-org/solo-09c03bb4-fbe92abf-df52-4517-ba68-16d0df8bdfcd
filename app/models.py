"""对账 API 的请求/响应模型（含数据校验）。"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_LINES = 12
MAX_STATIONS = 12
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class VersionReport(BaseModel):
    """某一版的版号与晚点次数。"""

    version: str = Field(min_length=1, max_length=64, examples=["2026-10.v1"])
    delay_count: int = Field(ge=0, examples=[5])


class LineApproval(BaseModel):
    """一条线路的核定晚点次数。"""

    line_id: str = Field(min_length=1, max_length=32, examples=["L-01"])
    approved_delays: int = Field(ge=0, examples=[8])


class StationReports(BaseModel):
    """一个车站的已发版与（至多一个）待发版。"""

    station_id: str = Field(min_length=1, max_length=32, examples=["ST-A"])
    published: VersionReport
    pending: VersionReport | None = None


class ArrivalCount(BaseModel):
    """某线路在某站的实际到站次数（未列出的组合视为 0）。"""

    line_id: str
    station_id: str
    count: int = Field(ge=0)


class PrecheckRequest(BaseModel):
    """一个月的联预检请求。"""

    month: str = Field(examples=["2026-10"])
    lines: list[LineApproval] = Field(min_length=1, max_length=MAX_LINES)
    stations: list[StationReports] = Field(min_length=1, max_length=MAX_STATIONS)
    arrivals: list[ArrivalCount] = Field(default_factory=list)

    @field_validator("month")
    @classmethod
    def _check_month(cls, v: str) -> str:
        if not _MONTH_RE.match(v):
            raise ValueError("month 必须为 YYYY-MM 格式（月份 01-12）")
        return v

    @model_validator(mode="after")
    def _check_refs(self) -> "PrecheckRequest":
        line_ids = [line.line_id for line in self.lines]
        if len(set(line_ids)) != len(line_ids):
            raise ValueError("lines 中存在重复的 line_id")
        station_ids = [st.station_id for st in self.stations]
        if len(set(station_ids)) != len(station_ids):
            raise ValueError("stations 中存在重复的 station_id")
        line_set, station_set = set(line_ids), set(station_ids)
        seen: set[tuple[str, str]] = set()
        for a in self.arrivals:
            if a.line_id not in line_set:
                raise ValueError(f"arrivals 引用了未声明的线路: {a.line_id}")
            if a.station_id not in station_set:
                raise ValueError(f"arrivals 引用了未声明的车站: {a.station_id}")
            key = (a.line_id, a.station_id)
            if key in seen:
                raise ValueError(
                    f"arrivals 中线路 {a.line_id} 与车站 {a.station_id} 的组合重复"
                )
            seen.add(key)
        return self


class SelectedVersion(BaseModel):
    """某站最终选定的版本。"""

    station_id: str
    source: Literal["published", "pending"]
    version: str
    delay_count: int


class CrossTable(BaseModel):
    """晚点次数交叉表：cells 行=线路、列=车站，与 *_ids 顺序对应。"""

    line_ids: list[str]
    station_ids: list[str]
    cells: list[list[int]]
    line_totals: dict[str, int]
    station_totals: dict[str, int]


class PrecheckResponse(BaseModel):
    month: str
    feasible: bool
    pending_station_count: int = Field(description="本次采用待发版的车站数")
    selected_versions: list[SelectedVersion] | None = None
    table: CrossTable | None = None
    message: str


# --------------------------------------------------------------------- 到站台账


class LedgerItem(BaseModel):
    """一条到站报送：归档流水号 + 修订号 + 归属（撤销时不带归属）。"""

    serial_no: str = Field(min_length=1, max_length=64, examples=["ARC-0001"])
    revision: int = Field(ge=0, examples=[1])
    action: Literal["upsert", "revoke"] = Field(default="upsert")
    month: str | None = Field(default=None, examples=["2026-09"])
    line_id: str | None = Field(default=None, examples=["L1"])
    station_id: str | None = Field(default=None, examples=["S1"])

    @field_validator("month")
    @classmethod
    def _check_month(cls, v: str | None) -> str | None:
        if v is not None and not _MONTH_RE.match(v):
            raise ValueError("month 必须为 YYYY-MM 格式（月份 01-12）")
        return v

    @model_validator(mode="after")
    def _check_action_shape(self) -> "LedgerItem":
        if self.action == "upsert":
            missing = [
                name
                for name in ("month", "line_id", "station_id")
                if getattr(self, name) in (None, "")
            ]
            if missing:
                raise ValueError(f"upsert 必须包含字段: {', '.join(missing)}")
        else:
            extras = [
                name
                for name in ("month", "line_id", "station_id")
                if getattr(self, name) is not None
            ]
            if extras:
                raise ValueError(f"revoke 不允许携带归属字段: {', '.join(extras)}")
        return self


class LedgerBatchRequest(BaseModel):
    """一批到站报送；同一流水号每批限一条。"""

    items: list[LedgerItem] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def _check_unique_serial(self) -> "LedgerBatchRequest":
        seen: set[str] = set()
        for item in self.items:
            if item.serial_no in seen:
                raise ValueError(
                    f"同一流水号每批限一条: {item.serial_no} 在本批出现多次"
                )
            seen.add(item.serial_no)
        return self


class BatchAppliedEntry(BaseModel):
    serial_no: str
    revision: int
    action: str
    effect: str
    month: str | None = None
    line_id: str | None = None
    station_id: str | None = None
    old: dict[str, str] | None = None
    new: dict[str, str] | None = None


class BatchSkippedEntry(BaseModel):
    serial_no: str
    revision: int
    reason: str


class LedgerBatchResponse(BaseModel):
    ledger_version: int = Field(description="本批提交后台账的全局版号")
    applied: list[BatchAppliedEntry]
    skipped: list[BatchSkippedEntry]
    affected_months: list[str]


class SummaryRow(BaseModel):
    line_id: str
    station_id: str
    count: int


class MonthSummaryResponse(BaseModel):
    month: str
    booked: bool = Field(description="该月份是否曾入账（清空后仍为 true）")
    ledger_version: int
    rows: list[SummaryRow]


class SerialStateResponse(BaseModel):
    serial_no: str
    revision: int
    status: Literal["active", "revoked"]
    month: str | None
    line_id: str | None
    station_id: str | None


class LedgerMonthInput(BaseModel):
    """台账预检中一个月份的对账输入（到站上限由台账汇总提供，无需手填）。"""

    month: str
    lines: list[LineApproval] = Field(min_length=1, max_length=MAX_LINES)
    stations: list[StationReports] = Field(min_length=1, max_length=MAX_STATIONS)

    @field_validator("month")
    @classmethod
    def _check_month(cls, v: str) -> str:
        if not _MONTH_RE.match(v):
            raise ValueError("month 必须为 YYYY-MM 格式（月份 01-12）")
        return v

    @model_validator(mode="after")
    def _check_unique_ids(self) -> "LedgerMonthInput":
        line_ids = [line.line_id for line in self.lines]
        if len(set(line_ids)) != len(line_ids):
            raise ValueError("lines 中存在重复的 line_id")
        station_ids = [st.station_id for st in self.stations]
        if len(set(station_ids)) != len(station_ids):
            raise ValueError("stations 中存在重复的 station_id")
        return self


class LedgerPrecheckRequest(BaseModel):
    """基于台账月汇总的联发预检（可一次提交多个月份）。"""

    months: list[LedgerMonthInput] = Field(min_length=1, max_length=12)


class LedgerMonthResult(BaseModel):
    month: str
    feasible: bool
    pending_station_count: int
    selected_versions: list[SelectedVersion] | None = None
    table: CrossTable | None = None
    arrivals_used: list[ArrivalCount] = Field(
        description="预检实际采用的到站上限，直接读自台账月汇总（含 0 次行）"
    )
    message: str


class LedgerPrecheckResponse(BaseModel):
    ledger_version: int = Field(description="预检读取的台账版号")
    results: list[LedgerMonthResult]


class MissingMonthsResponse(BaseModel):
    code: Literal["months_not_booked"]
    message: str
    missing_months: list[str]
    ledger_version: int
