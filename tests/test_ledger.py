"""到站台账与台账版预检的测试（存储层 + TestClient）。"""

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.ledger import LedgerStore
from app.main import app


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """每个测试使用独立 SQLite 文件，并替换 API 的全局存储。"""
    db_path = str(tmp_path / "ledger.db")
    monkeypatch.setenv("LEDGER_DB_PATH", db_path)
    s = LedgerStore(db_path)
    app.state.ledger_store = s
    yield s
    s.close()
    app.state.ledger_store = None


@pytest.fixture()
def client(store):
    return TestClient(app)


def _upsert(serial, revision, month, line, station, action="upsert"):
    item = {
        "serial_no": serial,
        "revision": revision,
        "action": action,
    }
    if action == "upsert":
        item.update({"month": month, "line_id": line, "station_id": station})
    return item


# ------------------------------------------------------------ 存储层规则测试


def test_insert_counts_and_version(store):
    out = store.submit_batch(
        [
            main_module.BatchItem("A1", 1, "upsert", "2026-09", "L1", "S1"),
            main_module.BatchItem("A2", 1, "upsert", "2026-09", "L1", "S1"),
            main_module.BatchItem("A3", 1, "upsert", "2026-09", "L1", "S2"),
        ]
    )
    assert out["ledger_version"] == 1
    assert sorted(out["affected_months"]) == ["2026-09"]
    assert len(out["applied"]) == 3
    summary = {
        (r["line_id"], r["station_id"]): r["count"]
        for r in store.get_month_summary("2026-09")
    }
    assert summary == {("L1", "S1"): 2, ("L1", "S2"): 1}


def test_lower_revision_is_skipped(store):
    store.submit_batch(
        [main_module.BatchItem("A1", 3, "upsert", "2026-09", "L1", "S1")]
    )
    out = store.submit_batch(
        [main_module.BatchItem("A1", 2, "upsert", "2026-10", "L1", "S2")]
    )
    assert out["applied"] == []
    assert out["skipped"][0]["reason"] == "lower_revision"
    assert out["ledger_version"] == 1  # 版号不增
    state = store.get_serial("A1")
    assert state.revision == 3 and state.month == "2026-09"


def test_identical_resubmission_is_skipped(store):
    item = main_module.BatchItem("A1", 1, "upsert", "2026-09", "L1", "S1")
    store.submit_batch([item])
    out = store.submit_batch([item])
    assert out["applied"] == []
    assert out["skipped"][0]["reason"] == "identical_resubmission"
    assert out["ledger_version"] == 1
    assert store.get_month_summary("2026-09")[0]["count"] == 1


def test_same_revision_different_content_rejects_whole_batch(store):
    store.submit_batch(
        [main_module.BatchItem("A1", 1, "upsert", "2026-09", "L1", "S1")]
    )
    # 同批里还带一条正常的新流水号，必须一并退回
    with pytest.raises(main_module.LedgerConflict) as exc:
        store.submit_batch(
            [
                main_module.BatchItem("A1", 1, "upsert", "2026-10", "L1", "S1"),
                main_module.BatchItem("A2", 1, "upsert", "2026-09", "L1", "S2"),
            ]
        )
    assert exc.value.code == "same_revision_conflict"
    # 原账保留：版号、汇总、第二条流水均未变
    assert store.get_version() == 1
    assert store.get_serial("A2") is None
    assert store.get_month_summary("2026-09") == [
        {"line_id": "L1", "station_id": "S1", "count": 1}
    ]


def test_cross_month_correction_moves_one_count(store):
    store.submit_batch(
        [main_module.BatchItem("A1", 1, "upsert", "2026-09", "L1", "S1")]
    )
    out = store.submit_batch(
        [main_module.BatchItem("A1", 2, "upsert", "2026-10", "L1", "S1")]
    )
    assert out["ledger_version"] == 2
    assert out["applied"][0]["effect"] == "replace"
    assert sorted(out["affected_months"]) == ["2026-09", "2026-10"]
    # 旧月扣一笔：保留 0 行（已入账清空按零次）
    old = store.get_month_summary("2026-09")
    assert old == [{"line_id": "L1", "station_id": "S1", "count": 0}]
    assert store.is_booked("2026-09") is True
    # 新月加一笔
    new = store.get_month_summary("2026-10")
    assert new == [{"line_id": "L1", "station_id": "S1", "count": 1}]
    state = store.get_serial("A1")
    assert state.revision == 2 and state.month == "2026-10" and state.status == "active"


def test_revoke_keeps_revision_and_deducts(store):
    store.submit_batch(
        [main_module.BatchItem("A1", 1, "upsert", "2026-09", "L1", "S1")]
    )
    out = store.submit_batch(
        [main_module.BatchItem("A1", 2, "revoke", None, None, None)]
    )
    assert out["applied"][0]["effect"] == "revoke"
    state = store.get_serial("A1")
    assert state.status == "revoked" and state.revision == 2  # 修订号保留并推进
    assert store.get_month_summary("2026-09")[0]["count"] == 0
    # 已撤销后再发同修订号撤销：跳过
    again = store.submit_batch(
        [main_module.BatchItem("A1", 2, "revoke", None, None, None)]
    )
    assert again["skipped"][0]["reason"] == "already_revoked"
    # 更高修订号的撤销只推进修订号，不动汇总
    bump = store.submit_batch(
        [main_module.BatchItem("A1", 5, "revoke", None, None, None)]
    )
    assert bump["applied"][0]["effect"] == "revoke_revision_bump"
    assert store.get_serial("A1").revision == 5
    assert store.get_month_summary("2026-09")[0]["count"] == 0


def test_reactivate_after_revoke_adds_new_count(store):
    store.submit_batch(
        [main_module.BatchItem("A1", 1, "upsert", "2026-09", "L1", "S1")]
    )
    store.submit_batch(
        [main_module.BatchItem("A1", 2, "revoke", None, None, None)]
    )
    out = store.submit_batch(
        [main_module.BatchItem("A1", 3, "upsert", "2026-10", "L1", "S2")]
    )
    assert out["applied"][0]["effect"] == "reactivate"
    state = store.get_serial("A1")
    assert state.status == "active" and state.revision == 3
    assert store.get_month_summary("2026-09")[0]["count"] == 0
    assert store.get_month_summary("2026-10") == [
        {"line_id": "L1", "station_id": "S2", "count": 1}
    ]


def test_revoke_unknown_serial_conflicts(store):
    with pytest.raises(main_module.LedgerConflict) as exc:
        store.submit_batch(
            [main_module.BatchItem("X9", 1, "revoke", None, None, None)]
        )
    assert exc.value.code == "serial_not_found"
    assert store.get_version() == 0


def test_persistence_survives_reopen(store, tmp_path):
    db_path = str(tmp_path / "ledger.db")
    s1 = LedgerStore(db_path)
    s1.submit_batch(
        [main_module.BatchItem("A1", 1, "upsert", "2026-09", "L1", "S1")]
    )
    s1.close()
    s2 = LedgerStore(db_path)  # 模拟重启
    try:
        assert s2.get_version() == 1
        assert s2.get_serial("A1").status == "active"
        assert s2.get_month_summary("2026-09")[0]["count"] == 1
        assert s2.is_booked("2026-09") is True
    finally:
        s2.close()


# ----------------------------------------------------------------- API 测试


def test_api_batch_then_summary_and_version(client):
    resp = client.post(
        "/api/v1/ledger/batches",
        json={
            "items": [
                _upsert("A1", 1, "2026-09", "L1", "S1"),
                _upsert("A2", 1, "2026-09", "L1", "S1"),
            ]
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ledger_version"] == 1
    assert len(body["applied"]) == 2

    assert client.get("/api/v1/ledger/version").json() == {"ledger_version": 1}

    summ = client.get("/api/v1/ledger/months/2026-09/summary").json()
    assert summ["booked"] is True
    assert summ["rows"] == [{"line_id": "L1", "station_id": "S1", "count": 2}]

    serial = client.get("/api/v1/ledger/serials/A1").json()
    assert serial["status"] == "active" and serial["revision"] == 1
    assert client.get("/api/v1/ledger/serials/NOPE").status_code == 404


def test_api_duplicate_serial_in_batch_422(client):
    resp = client.post(
        "/api/v1/ledger/batches",
        json={
            "items": [
                _upsert("A1", 1, "2026-09", "L1", "S1"),
                _upsert("A1", 2, "2026-09", "L1", "S2"),
            ]
        },
    )
    assert resp.status_code == 422


def test_api_upsert_requires_attribution_fields(client):
    resp = client.post(
        "/api/v1/ledger/batches",
        json={"items": [{"serial_no": "A1", "revision": 1, "action": "upsert"}]},
    )
    assert resp.status_code == 422
    resp2 = client.post(
        "/api/v1/ledger/batches",
        json={
            "items": [
                {
                    "serial_no": "A1",
                    "revision": 1,
                    "action": "revoke",
                    "month": "2026-09",
                }
            ]
        },
    )
    assert resp2.status_code == 422


def test_api_same_revision_conflict_409_keeps_ledger(client):
    client.post(
        "/api/v1/ledger/batches",
        json={"items": [_upsert("A1", 1, "2026-09", "L1", "S1")]},
    )
    resp = client.post(
        "/api/v1/ledger/batches",
        json={
            "items": [
                _upsert("A1", 1, "2026-10", "L1", "S1"),  # 同版异文
                _upsert("A2", 1, "2026-09", "L1", "S2"),  # 连带退回
            ]
        },
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "same_revision_conflict"
    assert detail["serial_no"] == "A1"
    assert detail["ledger_version"] == 1
    assert client.get("/api/v1/ledger/serials/A2").status_code == 404
    summ = client.get("/api/v1/ledger/months/2026-09/summary").json()
    assert summ["rows"] == [{"line_id": "L1", "station_id": "S1", "count": 1}]


def test_api_revoke_unknown_409(client):
    resp = client.post(
        "/api/v1/ledger/batches",
        json={"items": [{"serial_no": "X9", "revision": 1, "action": "revoke"}]},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "serial_not_found"


def test_api_ledger_precheck_two_months_uses_summary(client):
    # 2026-09：L1@S1 到站 6 次；2026-10：L1@S1 到站 10 次
    client.post(
        "/api/v1/ledger/batches",
        json={
            "items": [
                _upsert(f"A{i}", 1, "2026-09", "L1", "S1") for i in range(6)
            ]
            + [
                _upsert(f"B{i}", 1, "2026-10", "L1", "S1") for i in range(10)
            ]
        },
    )
    payload = {
        "months": [
            {
                "month": "2026-09",
                "lines": [{"line_id": "L1", "approved_delays": 6}],
                "stations": [
                    {
                        "station_id": "S1",
                        "published": {"version": "sep.v1", "delay_count": 4},
                        "pending": {"version": "sep.v2", "delay_count": 6},
                    }
                ],
            },
            {
                "month": "2026-10",
                "lines": [{"line_id": "L1", "approved_delays": 10}],
                "stations": [
                    {
                        "station_id": "S1",
                        "published": {"version": "oct.v1", "delay_count": 10},
                    }
                ],
            },
        ]
    }
    resp = client.post("/api/v1/ledger/precheck", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ledger_version"] == 1  # 返回所用版号
    sep, oct_ = body["results"]
    assert sep["feasible"] is True
    assert sep["table"]["cells"] == [[6]]  # 上限直接取自台账汇总
    assert sep["arrivals_used"] == [
        {"line_id": "L1", "station_id": "S1", "count": 6}
    ]
    assert sep["selected_versions"][0]["version"] == "sep.v2"
    assert oct_["feasible"] is True
    assert oct_["table"]["cells"] == [[10]]
    assert oct_["arrivals_used"] == [
        {"line_id": "L1", "station_id": "S1", "count": 10}
    ]


def test_api_precheck_missing_month_404_lists_gaps(client):
    client.post(
        "/api/v1/ledger/batches",
        json={"items": [_upsert("A1", 1, "2026-09", "L1", "S1")]},
    )
    payload = {
        "months": [
            {
                "month": "2026-09",
                "lines": [{"line_id": "L1", "approved_delays": 1}],
                "stations": [
                    {
                        "station_id": "S1",
                        "published": {"version": "v1", "delay_count": 1},
                    }
                ],
            },
            {
                "month": "2026-11",
                "lines": [{"line_id": "L1", "approved_delays": 0}],
                "stations": [
                    {
                        "station_id": "S1",
                        "published": {"version": "v1", "delay_count": 0},
                    }
                ],
            },
        ]
    }
    resp = client.post("/api/v1/ledger/precheck", json=payload)
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "months_not_booked"
    assert body["missing_months"] == ["2026-11"]
    assert body["ledger_version"] == 1


def test_api_booked_but_emptied_month_treated_as_zero(client):
    client.post(
        "/api/v1/ledger/batches",
        json={"items": [_upsert("A1", 1, "2026-09", "L1", "S1")]},
    )
    client.post(
        "/api/v1/ledger/batches",
        json={"items": [{"serial_no": "A1", "revision": 2, "action": "revoke"}]},
    )
    # 已入账但清空：不报错，按零次到站处理 → 核定 0、晚点 0 可行
    payload = {
        "months": [
            {
                "month": "2026-09",
                "lines": [{"line_id": "L1", "approved_delays": 0}],
                "stations": [
                    {
                        "station_id": "S1",
                        "published": {"version": "v1", "delay_count": 0},
                    }
                ],
            }
        ]
    }
    resp = client.post("/api/v1/ledger/precheck", json=payload)
    assert resp.status_code == 200
    result = resp.json()["results"][0]
    assert result["feasible"] is True
    assert result["table"]["cells"] == [[0]]
    assert result["arrivals_used"] == [
        {"line_id": "L1", "station_id": "S1", "count": 0}
    ]

    # 同样零上限下核定 1 则无法联发
    payload["months"][0]["lines"][0]["approved_delays"] = 1
    resp2 = client.post("/api/v1/ledger/precheck", json=payload)
    assert resp2.status_code == 200
    assert resp2.json()["results"][0]["feasible"] is False


def test_api_cross_month_correction_end_to_end(client):
    """跨月更正 + 两月预检的完整链路（对应 Swagger 试跑场景）。"""
    client.post(
        "/api/v1/ledger/batches",
        json={"items": [_upsert("A1", 1, "2026-09", "L1", "S1")]},
    )
    corr = client.post(
        "/api/v1/ledger/batches",
        json={"items": [_upsert("A1", 2, "2026-10", "L1", "S1")]},
    )
    assert corr.status_code == 200
    assert corr.json()["affected_months"] == ["2026-09", "2026-10"]
    # 两月都已入账（旧月为清空状态），预检不应报缺数
    payload = {
        "months": [
            {
                "month": "2026-09",
                "lines": [{"line_id": "L1", "approved_delays": 0}],
                "stations": [
                    {
                        "station_id": "S1",
                        "published": {"version": "v1", "delay_count": 0},
                    }
                ],
            },
            {
                "month": "2026-10",
                "lines": [{"line_id": "L1", "approved_delays": 1}],
                "stations": [
                    {
                        "station_id": "S1",
                        "published": {"version": "v1", "delay_count": 1},
                    }
                ],
            },
        ]
    }
    resp = client.post("/api/v1/ledger/precheck", json=payload)
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert [r["month"] for r in results] == ["2026-09", "2026-10"]
    assert results[0]["arrivals_used"][0]["count"] == 0
    assert results[1]["arrivals_used"][0]["count"] == 1
