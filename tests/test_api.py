"""API 集成测试（TestClient）。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _payload_two_station_correction():
    """两站更正必须同时采用才能对平的情形。"""
    return {
        "month": "2026-10",
        "lines": [{"line_id": "L1", "approved_delays": 12}],
        "stations": [
            {
                "station_id": "S1",
                "published": {"version": "2026-10.v1", "delay_count": 4},
                "pending": {"version": "2026-10.v2", "delay_count": 6},
            },
            {
                "station_id": "S2",
                "published": {"version": "2026-10.v1", "delay_count": 4},
                "pending": {"version": "2026-10.v2", "delay_count": 6},
            },
        ],
        "arrivals": [
            {"line_id": "L1", "station_id": "S1", "count": 6},
            {"line_id": "L1", "station_id": "S2", "count": 6},
        ],
    }


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_precheck_two_stations_must_adopt_together():
    resp = client.post(
        "/api/v1/reconciliation/precheck", json=_payload_two_station_correction()
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["feasible"] is True
    assert body["pending_station_count"] == 2
    # 两站都必须采用待发版
    for sel in body["selected_versions"]:
        assert sel["source"] == "pending"
        assert sel["version"] == "2026-10.v2"
        assert sel["delay_count"] == 6
    # 交叉表：行合计=核定12，列合计=所选版各6，每格<=到站6
    table = body["table"]
    assert table["line_ids"] == ["L1"]
    assert table["station_ids"] == ["S1", "S2"]
    assert table["cells"] == [[6, 6]]
    assert table["line_totals"] == {"L1": 12}
    assert table["station_totals"] == {"S1": 6, "S2": 6}


def test_precheck_prefers_max_pending():
    payload = {
        "month": "2026-10",
        "lines": [
            {"line_id": "L1", "approved_delays": 5},
            {"line_id": "L2", "approved_delays": 7},
        ],
        "stations": [
            {
                "station_id": "S1",
                "published": {"version": "v1", "delay_count": 5},
                "pending": {"version": "v2", "delay_count": 8},
            },
            {
                "station_id": "S2",
                "published": {"version": "v1", "delay_count": 7},
                "pending": {"version": "v2", "delay_count": 4},
            },
        ],
        "arrivals": [
            {"line_id": "L1", "station_id": "S1", "count": 4},
            {"line_id": "L1", "station_id": "S2", "count": 4},
            {"line_id": "L2", "station_id": "S1", "count": 6},
            {"line_id": "L2", "station_id": "S2", "count": 4},
        ],
    }
    resp = client.post("/api/v1/reconciliation/precheck", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["feasible"] is True
    assert body["pending_station_count"] == 2
    cells = body["table"]["cells"]
    for i, lid in enumerate(body["table"]["line_ids"]):
        assert sum(cells[i]) == body["table"]["line_totals"][lid]
    for j, sid in enumerate(body["table"]["station_ids"]):
        assert sum(row[j] for row in cells) == body["table"]["station_totals"][sid]


def test_precheck_infeasible_reports_no_joint_issue():
    payload = {
        "month": "2026-10",
        "lines": [{"line_id": "L1", "approved_delays": 10}],
        "stations": [
            {
                "station_id": "S1",
                "published": {"version": "v1", "delay_count": 3},
                "pending": {"version": "v2", "delay_count": 5},
            },
            {
                "station_id": "S2",
                "published": {"version": "v1", "delay_count": 4},
            },
        ],
        "arrivals": [
            {"line_id": "L1", "station_id": "S1", "count": 9},
            {"line_id": "L1", "station_id": "S2", "count": 9},
        ],
    }
    resp = client.post("/api/v1/reconciliation/precheck", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["feasible"] is False
    assert body["table"] is None
    assert body["selected_versions"] is None
    assert "无法联发" in body["message"]


def test_validation_rejects_too_many_lines():
    payload = {
        "month": "2026-10",
        "lines": [
            {"line_id": f"L{i}", "approved_delays": 0} for i in range(13)
        ],
        "stations": [
            {
                "station_id": "S1",
                "published": {"version": "v1", "delay_count": 0},
            }
        ],
    }
    assert client.post("/api/v1/reconciliation/precheck", json=payload).status_code == 422


def test_validation_rejects_unknown_station_in_arrivals():
    payload = _payload_two_station_correction()
    payload["arrivals"].append(
        {"line_id": "L1", "station_id": "S-X", "count": 1}
    )
    assert client.post("/api/v1/reconciliation/precheck", json=payload).status_code == 422


def test_validation_rejects_duplicate_arrival_pair():
    payload = _payload_two_station_correction()
    payload["arrivals"].append(
        {"line_id": "L1", "station_id": "S1", "count": 1}
    )
    assert client.post("/api/v1/reconciliation/precheck", json=payload).status_code == 422


def test_validation_rejects_bad_month():
    payload = _payload_two_station_correction()
    payload["month"] = "2026-13"
    assert client.post("/api/v1/reconciliation/precheck", json=payload).status_code == 422
