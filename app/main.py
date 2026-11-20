"""铁路晚点月报对账 API。

车站补交晚点月报更正后，数据员提交一个月的：
- 各线路核定晚点次数；
- 各线路在各站的实际到站次数（未列出的组合视为 0）；
- 每站已发版与至多一个待发版的版号与晚点次数。

预检接口为每站选择一个版本，使采用待发版的站数最多，并返回所选版号
与一张可行的晚点次数交叉表供复核；若任何版本组合都不可行，则报告
本次无法联发，不生成对账表。

此外提供到站台账（``/api/v1/ledger/...``）：批量入账/撤销到站流水，
按最高修订号计数并维护月汇总；台账版联发预检直接读取同一份已提交的
月汇总作为到站上限，返回所用台账版号，无需扫描流水。
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from .ledger import BatchItem, LedgerConflict, LedgerStore
from .models import (
    ArrivalCount,
    CrossTable,
    LedgerBatchRequest,
    LedgerBatchResponse,
    LedgerMonthResult,
    LedgerPrecheckRequest,
    LedgerPrecheckResponse,
    MissingMonthsResponse,
    MonthSummaryResponse,
    PrecheckRequest,
    PrecheckResponse,
    SelectedVersion,
    SerialStateResponse,
    SummaryRow,
)
from .solver import LineInput, StationInput, solve


# ------------------------------------------------------------------ 台账存储


def _default_db_path() -> str:
    return os.environ.get("LEDGER_DB_PATH", "/data/ledger.db")


def get_store() -> LedgerStore:
    """惰性单例；测试可通过 LEDGER_DB_PATH 指向临时文件。"""
    store = getattr(app.state, "ledger_store", None)
    if store is None:
        store = LedgerStore(_default_db_path())
        app.state.ledger_store = store
    return store


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    store = getattr(app.state, "ledger_store", None)
    if store is not None:
        store.close()
        app.state.ledger_store = None


app = FastAPI(
    title="铁路晚点月报对账 API",
    description=(
        "对车站补交的晚点月报更正做联发预检：在每站已发版/待发版中各选一版，"
        "使采用待发版的站数最多，并给出同时满足线路核定数、车站版本晚点数、"
        "实际到站上限制约的可行交叉表。另有到站台账：批量入账/修订/撤销到站"
        "流水并维护月汇总，台账版预检直接读取月汇总并返回所用版号。"
    ),
    version="2.0.0",
    lifespan=lifespan,
)


# ------------------------------------------------------------- 预检共用逻辑


def _build_precheck(
    month: str,
    lines: list[Any],
    stations: list[Any],
    arrivals: list[list[int]],
) -> PrecheckResponse:
    """给定线路、车站版本与到站上限矩阵，构造预检响应。"""
    line_ids = [line.line_id for line in lines]
    station_ids = [st.station_id for st in stations]
    result = solve(
        [LineInput(line.line_id, line.approved_delays) for line in lines],
        [
            StationInput(
                st.station_id,
                st.published.delay_count,
                st.pending.delay_count if st.pending is not None else None,
            )
            for st in stations
        ],
        arrivals,
    )

    if result is None:
        return PrecheckResponse(
            month=month,
            feasible=False,
            pending_station_count=0,
            selected_versions=None,
            table=None,
            message=(
                "本次无法联发：任何已发版/待发版组合下，都不存在同时满足线路"
                "核定数、车站版本晚点数与实际到站上限制约的交叉表，未生成对账表。"
            ),
        )

    selected = []
    for st, use_pending in zip(stations, result.use_pending):
        chosen = st.pending if use_pending else st.published
        assert chosen is not None
        selected.append(
            SelectedVersion(
                station_id=st.station_id,
                source="pending" if use_pending else "published",
                version=chosen.version,
                delay_count=chosen.delay_count,
            )
        )

    cells = [list(row) for row in result.matrix]
    table = CrossTable(
        line_ids=line_ids,
        station_ids=station_ids,
        cells=cells,
        line_totals={lid: sum(row) for lid, row in zip(line_ids, cells)},
        station_totals={
            sid: sum(cells[i][j] for i in range(len(line_ids)))
            for j, sid in enumerate(station_ids)
        },
    )
    return PrecheckResponse(
        month=month,
        feasible=True,
        pending_station_count=sum(result.use_pending),
        selected_versions=selected,
        table=table,
        message="预检通过：已给出采用待发版站数最多的可行对账表，供复核。",
    )


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


# --------------------------------------------------------- 手填上限的原预检


@app.post(
    "/api/v1/reconciliation/precheck",
    response_model=PrecheckResponse,
    tags=["reconciliation"],
    summary="晚点月报联发预检（手填到站上限）",
)
def precheck(req: PrecheckRequest) -> PrecheckResponse:
    line_ids = [line.line_id for line in req.lines]
    station_ids = [st.station_id for st in req.stations]
    line_idx = {lid: i for i, lid in enumerate(line_ids)}
    station_idx = {sid: j for j, sid in enumerate(station_ids)}

    # 未列出的线路×车站组合视为 0 次到站
    arrivals = [[0] * len(station_ids) for _ in line_ids]
    for a in req.arrivals:
        arrivals[line_idx[a.line_id]][station_idx[a.station_id]] = a.count

    return _build_precheck(req.month, req.lines, req.stations, arrivals)


# --------------------------------------------------------------- 到站台账 API


@app.post(
    "/api/v1/ledger/batches",
    response_model=LedgerBatchResponse,
    tags=["ledger"],
    summary="批量提交到站流水（入账/修订/撤销）",
    status_code=200,
)
def submit_ledger_batch(req: LedgerBatchRequest) -> LedgerBatchResponse:
    store = get_store()
    items = [
        BatchItem(
            serial_no=it.serial_no,
            revision=it.revision,
            action=it.action,
            month=it.month,
            line_id=it.line_id,
            station_id=it.station_id,
        )
        for it in req.items
    ]
    try:
        out = store.submit_batch(items)
    except LedgerConflict as exc:
        # 整批退回：未写入任何数据，原账保留
        raise HTTPException(
            status_code=409,
            detail={
                "code": exc.code,
                "message": exc.message,
                "serial_no": exc.serial_no,
                "ledger_version": store.get_version(),
            },
        )
    return LedgerBatchResponse(**out)


@app.get(
    "/api/v1/ledger/months/{month}/summary",
    response_model=MonthSummaryResponse,
    tags=["ledger"],
    summary="读取某归属月的到站月汇总",
)
def get_month_summary(month: str) -> MonthSummaryResponse:
    store = get_store()
    rows = store.get_month_summary(month)
    return MonthSummaryResponse(
        month=month,
        booked=store.is_booked(month),
        ledger_version=store.get_version(),
        rows=[SummaryRow(**row) for row in rows],
    )


@app.get(
    "/api/v1/ledger/serials/{serial_no}",
    response_model=SerialStateResponse,
    tags=["ledger"],
    summary="查询某流水号的现行（最高修订号）记录",
)
def get_serial(serial_no: str) -> SerialStateResponse:
    store = get_store()
    state = store.get_serial(serial_no)
    if state is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "serial_not_found",
                "message": f"流水号 {serial_no} 不存在",
                "serial_no": serial_no,
            },
        )
    return SerialStateResponse(
        serial_no=state.serial_no,
        revision=state.revision,
        status=state.status,
        month=state.month,
        line_id=state.line_id,
        station_id=state.station_id,
    )


@app.get(
    "/api/v1/ledger/version",
    tags=["ledger"],
    summary="查询台账当前全局版号",
)
def get_ledger_version() -> dict[str, int]:
    return {"ledger_version": get_store().get_version()}


# ----------------------------------------------------- 台账版联发预检（多月）


@app.post(
    "/api/v1/ledger/precheck",
    response_model=LedgerPrecheckResponse,
    tags=["reconciliation"],
    summary="台账版联发预检（直接读取月汇总，可多月）",
    responses={404: {"model": MissingMonthsResponse}},
)
def ledger_precheck(req: LedgerPrecheckRequest):
    store = get_store()
    version = store.get_version()

    # 从未入账的月份提示缺数；已入账但清空的月份按零次处理（不报错）
    missing = [m.month for m in req.months if not store.is_booked(m.month)]
    if missing:
        return JSONResponse(
            status_code=404,
            content=MissingMonthsResponse(
                code="months_not_booked",
                message=(
                    "以下月份在台账中从未入账，缺少到站次数，无法预检："
                    + ", ".join(missing)
                ),
                missing_months=missing,
                ledger_version=version,
            ).model_dump(),
        )

    results: list[LedgerMonthResult] = []
    for month_input in req.months:
        line_ids = [line.line_id for line in month_input.lines]
        station_ids = [st.station_id for st in month_input.stations]
        # 只查月汇总，不扫描流水
        arrival_map = store.get_arrival_map(month_input.month)
        arrivals = [
            [arrival_map.get((lid, sid), 0) for sid in station_ids]
            for lid in line_ids
        ]
        pre = _build_precheck(
            month_input.month, month_input.lines, month_input.stations, arrivals
        )
        results.append(
            LedgerMonthResult(
                month=pre.month,
                feasible=pre.feasible,
                pending_station_count=pre.pending_station_count,
                selected_versions=pre.selected_versions,
                table=pre.table,
                arrivals_used=[
                    ArrivalCount(
                        line_id=lid,
                        station_id=sid,
                        count=arrival_map.get((lid, sid), 0),
                    )
                    for lid in line_ids
                    for sid in station_ids
                ],
                message=pre.message,
            )
        )

    return LedgerPrecheckResponse(ledger_version=version, results=results)
