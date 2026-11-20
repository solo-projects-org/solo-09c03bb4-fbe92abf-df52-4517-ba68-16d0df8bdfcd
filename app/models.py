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
