"""铁路晚点月报对账 API。

车站补交晚点月报更正后，数据员提交一个月的：
- 各线路核定晚点次数；
- 各线路在各站的实际到站次数（未列出的组合视为 0）；
- 每站已发版与至多一个待发版的版号与晚点次数。

预检接口为每站选择一个版本，使采用待发版的站数最多，并返回所选版号
与一张可行的晚点次数交叉表供复核；若任何版本组合都不可行，则报告
本次无法联发，不生成对账表。
"""

from __future__ import annotations

from fastapi import FastAPI

from .models import (
    CrossTable,
    PrecheckRequest,
    PrecheckResponse,
    SelectedVersion,
)
from .solver import LineInput, StationInput, solve

app = FastAPI(
    title="铁路晚点月报对账 API",
    description=(
        "对车站补交的晚点月报更正做联发预检：在每站已发版/待发版中各选一版，"
        "使采用待发版的站数最多，并给出同时满足线路核定数、车站版本晚点数、"
        "实际到站上限制约的可行交叉表。"
    ),
    version="1.0.0",
)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/api/v1/reconciliation/precheck",
    response_model=PrecheckResponse,
    tags=["reconciliation"],
    summary="晚点月报联发预检",
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

    result = solve(
        [LineInput(line.line_id, line.approved_delays) for line in req.lines],
        [
            StationInput(
                st.station_id,
                st.published.delay_count,
                st.pending.delay_count if st.pending is not None else None,
            )
            for st in req.stations
        ],
        arrivals,
    )

    if result is None:
        return PrecheckResponse(
            month=req.month,
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
    for st, use_pending in zip(req.stations, result.use_pending):
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
        month=req.month,
        feasible=True,
        pending_station_count=sum(result.use_pending),
        selected_versions=selected,
        table=table,
        message="预检通过：已给出采用待发版站数最多的可行对账表，供复核。",
    )
