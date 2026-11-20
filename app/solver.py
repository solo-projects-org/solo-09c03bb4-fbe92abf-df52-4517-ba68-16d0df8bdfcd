"""晚点月报对账求解器。

输入：各线路核定晚点次数、各线路在各站的实际到站次数（作为交叉表每格上限）、
每站已发版与（至多一个）待发版的晚点次数。

目标：为每站选择一个版本，使采用待发版的站数最多；在此前提下构造一张
可行的晚点次数交叉表——每格为不超过实际到站次数的非负整数，按线路合计
等于核定数，按站合计等于所选版本的晚点次数。

方法：版本选择按待发站子集从大到小枚举，第一个可行子集即最优；
可行性判定与矩阵构造用最大流（Dinic）。由于供需与容量均为整数，
最大流整数解天然存在，直接取流量作为交叉表格子值。
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations


@dataclass(frozen=True)
class LineInput:
    line_id: str
    approved: int  # 线路核定晚点次数


@dataclass(frozen=True)
class StationInput:
    station_id: str
    published_count: int  # 已发版晚点次数
    pending_count: int | None  # 待发版晚点次数，None 表示无待发版


@dataclass(frozen=True)
class SolveResult:
    use_pending: tuple[bool, ...]  # 每站是否采用待发版，与 stations 顺序一致
    matrix: tuple[tuple[int, ...], ...]  # 交叉表，行=线路，列=车站


class _Dinic:
    """Dinic 最大流。边用 [to, cap, rev_index] 列表存储以便原地改容量。"""

    def __init__(self, n: int) -> None:
        self._g: list[list[list[int]]] = [[] for _ in range(n)]

    def add_edge(self, u: int, v: int, cap: int) -> tuple[int, int]:
        """加边，返回 (u, 边下标)，便于求解后读取该边流量。"""
        self._g[u].append([v, cap, len(self._g[v])])
        self._g[v].append([u, 0, len(self._g[u]) - 1])
        return u, len(self._g[u]) - 1

    def max_flow(self, s: int, t: int) -> int:
        flow = 0
        n = len(self._g)
        while True:
            level = [-1] * n
            level[s] = 0
            queue = [s]
            for u in queue:
                for v, cap, _ in self._g[u]:
                    if cap > 0 and level[v] < 0:
                        level[v] = level[u] + 1
                        queue.append(v)
            if level[t] < 0:
                return flow
            it = [0] * n

            def dfs(u: int, f: int) -> int:
                if u == t:
                    return f
                while it[u] < len(self._g[u]):
                    i = it[u]
                    v, cap, rev = self._g[u][i]
                    if cap > 0 and level[v] == level[u] + 1:
                        d = dfs(v, min(f, cap))
                        if d > 0:
                            self._g[u][i][1] -= d
                            self._g[v][rev][1] += d
                            return d
                    it[u] += 1
                return 0

            while True:
                pushed = dfs(s, 1 << 60)
                if pushed == 0:
                    break
                flow += pushed

    def flow_on(self, u: int, idx: int, original_cap: int) -> int:
        return original_cap - self._g[u][idx][1]


def _feasible_matrix(
    supply: list[int], demand: list[int], arrivals: list[list[int]]
) -> tuple[tuple[int, ...], ...] | None:
    """判定运输问题可行性；可行则返回矩阵，否则返回 None。

    supply[i] = 线路 i 核定数；demand[j] = 车站 j 所选版晚点数；
    arrivals[i][j] = 线路上限（实际到站次数）。
    """
    n_l, n_s = len(supply), len(demand)
    src, sink = 0, n_l + n_s + 1
    dinic = _Dinic(n_l + n_s + 2)
    for i, s in enumerate(supply):
        dinic.add_edge(src, 1 + i, s)
    edge_refs: list[list[tuple[tuple[int, int], int]]] = []
    for i in range(n_l):
        row = []
        for j in range(n_s):
            cap = arrivals[i][j]
            row.append((dinic.add_edge(1 + i, 1 + n_l + j, cap), cap))
        edge_refs.append(row)
    for j, d in enumerate(demand):
        dinic.add_edge(1 + n_l + j, sink, d)

    if dinic.max_flow(src, sink) != sum(supply):
        return None

    matrix = []
    for i in range(n_l):
        row = []
        for j in range(n_s):
            (u, idx), cap = edge_refs[i][j]
            row.append(dinic.flow_on(u, idx, cap))
        matrix.append(tuple(row))
    return tuple(matrix)


def solve(
    lines: list[LineInput],
    stations: list[StationInput],
    arrivals: list[list[int]],
) -> SolveResult | None:
    """枚举版本组合，返回采用待发版站数最多的可行解；无可行解返回 None。"""
    n_s = len(stations)
    total_approved = sum(line.approved for line in lines)
    supply = [line.approved for line in lines]
    flexible = [j for j, st in enumerate(stations) if st.pending_count is not None]

    # 按待发站数量从多到少枚举，第一个可行子集即“采用待发版站数最多”
    for k in range(len(flexible), -1, -1):
        for subset in combinations(flexible, k):
            chosen = frozenset(subset)
            demand = [
                stations[j].pending_count if j in chosen else stations[j].published_count  # type: ignore[index]
                for j in range(n_s)
            ]
            if sum(demand) != total_approved:
                continue  # 总量对不上，必不可行，跳过
            matrix = _feasible_matrix(supply, demand, arrivals)
            if matrix is not None:
                use_pending = tuple(j in chosen for j in range(n_s))
                return SolveResult(use_pending=use_pending, matrix=matrix)
    return None
