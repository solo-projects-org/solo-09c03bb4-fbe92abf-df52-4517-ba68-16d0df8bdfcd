"""求解器单元测试。"""

from app.solver import LineInput, StationInput, solve


def _check_matrix(result, lines, stations, arrivals):
    """校验矩阵满足全部约束。"""
    for i, row in enumerate(result.matrix):
        assert sum(row) == lines[i].approved, "按线路合计须等于核定数"
        for j, cell in enumerate(row):
            assert 0 <= cell <= arrivals[i][j], "每格须为不超过实际到站次数的非负整数"
    for j, st in enumerate(stations):
        col = sum(result.matrix[i][j] for i in range(len(lines)))
        expect = (
            st.pending_count if result.use_pending[j] else st.published_count
        )
        assert col == expect, "按站合计须等于所选版晚点数"


def test_basic_feasible_prefers_pending():
    # 两线两站：全已发(5,7)与全待发(8,4)总量都为12，应优先全待发
    lines = [LineInput("L1", 5), LineInput("L2", 7)]
    stations = [
        StationInput("S1", published_count=5, pending_count=8),
        StationInput("S2", published_count=7, pending_count=4),
    ]
    arrivals = [[4, 4], [6, 4]]
    result = solve(lines, stations, arrivals)
    assert result is not None
    assert result.use_pending == (True, True)
    _check_matrix(result, lines, stations, arrivals)


def test_both_pending_required_to_balance():
    # 两站更正必须同时采用才能对平：
    # 核定12；已发各4（合计8），一站改一站不改合计10，两站都改合计12
    lines = [LineInput("L1", 12)]
    stations = [
        StationInput("S1", published_count=4, pending_count=6),
        StationInput("S2", published_count=4, pending_count=6),
    ]
    arrivals = [[6, 6]]
    result = solve(lines, stations, arrivals)
    assert result is not None
    assert result.use_pending == (True, True)
    assert result.matrix == ((6, 6),)
    _check_matrix(result, lines, stations, arrivals)


def test_infeasible_when_no_combination_matches_total():
    lines = [LineInput("L1", 10)]
    stations = [
        StationInput("S1", published_count=3, pending_count=5),
        StationInput("S2", published_count=4, pending_count=None),
    ]
    # 可选总量只有 7 或 8，永远对不上 10
    assert solve(lines, stations, [[9, 9]]) is None


def test_infeasible_when_arrival_cap_too_tight():
    lines = [LineInput("L1", 10)]
    stations = [StationInput("S1", published_count=3, pending_count=10)]
    # 总量能对上 10，但实际到站只有 2 次，格子放不下
    assert solve(lines, stations, [[2]]) is None


def test_zero_arrivals_default_for_unlisted_pairs():
    # 未列出的组合按 0 处理：L1 在 S2 无到站，S2 的晚点只能由 L2 承担
    lines = [LineInput("L1", 3), LineInput("L2", 4)]
    stations = [
        StationInput("S1", published_count=3, pending_count=None),
        StationInput("S2", published_count=4, pending_count=None),
    ]
    arrivals = [[3, 0], [3, 4]]
    result = solve(lines, stations, arrivals)
    assert result is not None
    assert result.matrix[0][1] == 0
    _check_matrix(result, lines, stations, arrivals)


def test_all_zero_is_feasible():
    lines = [LineInput("L1", 0)]
    stations = [StationInput("S1", published_count=0, pending_count=None)]
    result = solve(lines, stations, [[0]])
    assert result is not None
    assert result.matrix == ((0,),)
