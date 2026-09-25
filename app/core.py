"""直流配电树压降校核核心算法。

计算流程：
1. 校验输入构成以 ``source`` 为根的连通树；
2. 自叶向根累计每段承载的全部下游电流；
3. 自根向叶计算各段压降与各节点电压；
4. 汇总超额定电流线段与欠压节点。

``capacity`` 在基线校核通过的前提下，评估目标节点可追加的最大恒定负载电流：
目标到 source 路径上的线段电流同步增加，任一节点的新增压降等于它与目标节点
两条根路径的公共线段电阻之和乘新增电流（兄弟支路经公共上游线段同样受影响）。

``unregistered_load`` 假定恰有一个非 source 节点挂接未登记的正恒定负载，将各测点
实测电流与未舍入基线线段电流比较：正差（> 1e-6 A）须彼此一致（极差 ≤ 1e-6 A），
其均值即未登记负载电流；实测线段集合恰为候选节点根路径上测点集合的节点全部相容，
未测支路因此允许多解。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from typing import Optional, Sequence

ROOT_NODE = "source"
EPS = 1e-9
ROUND_DIGITS = 6
MEASURE_TOL = 1e-6  # 实测电流比较容差 (A)


class InputValidationError(ValueError):
    """输入未通过业务校验（无效树、负载引用未知节点等）。"""

    def __init__(self, message: str, details: Optional[list[str]] = None):
        super().__init__(message)
        self.message = message
        self.details = details or []


class BaselineViolationError(ValueError):
    """基线校核已存在超额定电流或欠压，无法评估可增容量。"""

    def __init__(self, message: str, details: Optional[list[str]] = None):
        super().__init__(message)
        self.message = message
        self.details = details or []


@dataclass
class Segment:
    id: str
    parent: str
    child: str
    resistance: float
    rated_current: float


@dataclass
class _Tree:
    """校验后的配电树与基线潮流（电流、电压均为未舍入值）。"""

    segments: list[Segment]
    parent_of: dict[str, str]
    children_map: dict[str, list[Segment]]
    order: list[str]  # 自根向叶 BFS 顺序
    load_map: dict[str, float]
    min_voltage_override: dict[str, float]
    node_total: dict[str, float]
    seg_current: dict[str, float]
    node_voltage: dict[str, float]


def _round(value: float) -> float:
    return round(value, ROUND_DIGITS)


def _truncate(value: float) -> float:
    """向下截断至 6 位小数（先按 1e-9 容差吸附浮点噪声）。"""
    snapped = Decimal(repr(round(value, ROUND_DIGITS + 3)))
    return float(snapped.quantize(Decimal("0.000001"), rounding=ROUND_FLOOR))


def _validate_and_build_tree(segments_in: Sequence) -> tuple[list[Segment], dict[str, str]]:
    """校验树结构，返回线段列表与 child -> parent 映射。

    校验规则：
    - 线段 id 唯一（缺省自动生成 seg-<序号>）；
    - 不存在自环（父节点与子节点相同）；
    - 每个节点至多有一个父节点；
    - 根节点 source 不能作为子节点；
    - 所有父节点必须是 source 或某个子节点（连通性）；
    - 不存在环路。
    """
    segments: list[Segment] = []
    for index, s in enumerate(segments_in):
        seg_id = s.id or f"seg-{index + 1}"
        segments.append(
            Segment(
                id=seg_id,
                parent=s.parent,
                child=s.child,
                resistance=s.resistance,
                rated_current=s.rated_current,
            )
        )

    errors: list[str] = []

    seen_ids: set[str] = set()
    for seg in segments:
        if seg.id in seen_ids:
            errors.append(f"线段 id 重复: '{seg.id}'")
        seen_ids.add(seg.id)

    for seg in segments:
        if seg.parent == seg.child:
            errors.append(f"线段 '{seg.id}' 的父节点与子节点相同（'{seg.parent}'），存在自环")

    parent_of: dict[str, str] = {}
    for seg in segments:
        if seg.child in parent_of:
            errors.append(
                f"节点 '{seg.child}' 存在多个父节点（'{parent_of[seg.child]}' 与 '{seg.parent}'）"
            )
        else:
            parent_of[seg.child] = seg.parent

    if ROOT_NODE in parent_of:
        errors.append(f"根节点 '{ROOT_NODE}' 不能作为子节点")

    child_nodes = set(parent_of)
    for seg in segments:
        if seg.parent != ROOT_NODE and seg.parent not in child_nodes:
            errors.append(f"线段 '{seg.id}' 的父节点 '{seg.parent}' 未在树中定义，树不连通")

    if errors:
        raise InputValidationError(f"输入未构成以 '{ROOT_NODE}' 为根的连通树", errors)

    # 环路检测：沿父指针上溯必须到达根
    for seg in segments:
        visited: set[str] = set()
        node = seg.child
        while node != ROOT_NODE:
            if node in visited:
                raise InputValidationError(
                    f"输入未构成以 '{ROOT_NODE}' 为根的连通树",
                    [f"检测到环路，涉及节点 '{node}'"],
                )
            visited.add(node)
            node = parent_of[node]

    return segments, parent_of


def _aggregate_loads(loads_in: Sequence, known_nodes: set[str]) -> tuple[dict[str, float], dict[str, float]]:
    """汇总各节点负载电流（同节点多条负载累加）及节点级最低允许电压。"""
    errors: list[str] = []
    load_map: dict[str, float] = {}
    min_voltage_map: dict[str, float] = {}

    for load in loads_in:
        if load.node not in known_nodes:
            errors.append(f"负载节点 '{load.node}' 不存在于树中")
            continue
        load_map[load.node] = load_map.get(load.node, 0.0) + load.current
        if load.min_voltage is not None:
            previous = min_voltage_map.get(load.node)
            if previous is not None and abs(previous - load.min_voltage) > EPS:
                errors.append(f"节点 '{load.node}' 的最低允许电压被重复定义且不一致")
            else:
                min_voltage_map[load.node] = load.min_voltage

    if errors:
        raise InputValidationError("负载定义无效", errors)

    return load_map, min_voltage_map


def _prepare_tree(source_voltage: float, segments_in: Sequence, loads_in: Sequence) -> _Tree:
    """校验输入并计算基线潮流（各段电流、各节点电压，未舍入）。"""
    segments, parent_of = _validate_and_build_tree(segments_in)

    children_map: dict[str, list[Segment]] = {}
    for seg in segments:
        children_map.setdefault(seg.parent, []).append(seg)

    nodes = set(parent_of) | {ROOT_NODE}
    load_map, min_voltage_override = _aggregate_loads(loads_in, nodes)

    # 自根向叶 BFS 排序（父节点一定先于子节点出现）
    order = [ROOT_NODE]
    queue: deque[str] = deque([ROOT_NODE])
    while queue:
        node = queue.popleft()
        for seg in children_map.get(node, []):
            order.append(seg.child)
            queue.append(seg.child)

    # 自叶向根累计每段承载的全部下游电流
    node_total = {node: load_map.get(node, 0.0) for node in nodes}
    seg_current: dict[str, float] = {}
    for node in reversed(order):
        for seg in children_map.get(node, []):
            seg_current[seg.id] = node_total[seg.child]
            node_total[node] += node_total[seg.child]

    # 自根向叶计算各节点电压
    node_voltage = {ROOT_NODE: source_voltage}
    for node in order:
        for seg in children_map.get(node, []):
            node_voltage[seg.child] = node_voltage[node] - seg_current[seg.id] * seg.resistance

    return _Tree(
        segments=segments,
        parent_of=parent_of,
        children_map=children_map,
        order=order,
        load_map=load_map,
        min_voltage_override=min_voltage_override,
        node_total=node_total,
        seg_current=seg_current,
        node_voltage=node_voltage,
    )


def _node_threshold(tree: _Tree, node: str, min_voltage: float) -> float:
    """节点生效的最低允许电压（节点级覆盖优先于全局值）。"""
    return tree.min_voltage_override.get(node, min_voltage)


def _find_violations(tree: _Tree, min_voltage: float) -> tuple[list[dict], list[dict]]:
    """按 EPS 容差判定超额定电流线段与欠压节点（恰好触限不算越限）。"""
    over_current_segments = []
    for seg in tree.segments:  # 保持输入顺序
        current = tree.seg_current[seg.id]
        if current > seg.rated_current + EPS:
            over_current_segments.append(
                {
                    "segment_id": seg.id,
                    "parent": seg.parent,
                    "child": seg.child,
                    "current": _round(current),
                    "rated_current": seg.rated_current,
                    "excess": _round(current - seg.rated_current),
                }
            )

    under_voltage_nodes = []
    for node in tree.order:  # BFS 顺序，根在前
        threshold = _node_threshold(tree, node, min_voltage)
        voltage = tree.node_voltage[node]
        if voltage < threshold - EPS:
            under_voltage_nodes.append(
                {
                    "node": node,
                    "voltage": _round(voltage),
                    "min_voltage": threshold,
                    "deficit": _round(threshold - voltage),
                }
            )

    return over_current_segments, under_voltage_nodes


def evaluate(source_voltage: float, min_voltage: float, segments_in: Sequence, loads_in: Sequence) -> dict:
    """执行压降校核，返回可 JSON 序列化的结果字典。"""
    tree = _prepare_tree(source_voltage, segments_in, loads_in)
    over_current_segments, under_voltage_nodes = _find_violations(tree, min_voltage)

    segment_results = []
    for seg in tree.segments:  # 保持输入顺序
        current = tree.seg_current[seg.id]
        segment_results.append(
            {
                "segment_id": seg.id,
                "parent": seg.parent,
                "child": seg.child,
                "current": _round(current),
                "rated_current": seg.rated_current,
                "resistance": seg.resistance,
                "voltage_drop": _round(current * seg.resistance),
                "over_rated": current > seg.rated_current + EPS,
            }
        )

    node_results = []
    for node in tree.order:  # BFS 顺序，根在前
        threshold = _node_threshold(tree, node, min_voltage)
        voltage = tree.node_voltage[node]
        node_results.append(
            {
                "node": node,
                "voltage": _round(voltage),
                "min_voltage": threshold,
                "load_current": _round(tree.load_map.get(node, 0.0)),
                "under_voltage": voltage < threshold - EPS,
            }
        )

    return {
        "ok": not over_current_segments and not under_voltage_nodes,
        "source_voltage": source_voltage,
        "min_voltage": min_voltage,
        "total_source_current": _round(tree.node_total[ROOT_NODE]),
        "segments": segment_results,
        "nodes": node_results,
        "over_current_segments": over_current_segments,
        "under_voltage_nodes": under_voltage_nodes,
    }


def capacity(
    source_voltage: float,
    min_voltage: float,
    segments_in: Sequence,
    loads_in: Sequence,
    target_node: str,
) -> dict:
    """评估目标节点可追加的最大恒定负载电流。

    新增电流 ΔI 仅改变目标到 source 路径上的线段电流；任一节点 n 的新增压降
    = R_common(n, target) × ΔI，R_common 为两条根路径公共线段的电阻之和
    （兄弟支路经公共上游线段同样受压）。最大 ΔI 受限于：
    - 路径上每段的额定电流余量；
    - 每个 R_common > 0 节点的欠压余量（零公共电阻节点不受影响）。
    """
    tree = _prepare_tree(source_voltage, segments_in, loads_in)

    if target_node == ROOT_NODE:
        raise InputValidationError("目标节点无效", [f"目标节点不能为根节点 '{ROOT_NODE}'"])
    if target_node not in tree.parent_of:
        raise InputValidationError("目标节点无效", [f"目标节点 '{target_node}' 不存在于树中"])

    over_current_segments, under_voltage_nodes = _find_violations(tree, min_voltage)
    if over_current_segments or under_voltage_nodes:
        details = [
            f"线段 '{v['segment_id']}' 已超额定电流：{v['current']} A（额定 {v['rated_current']} A）"
            for v in over_current_segments
        ]
        details += [
            f"节点 '{v['node']}' 已欠压：{v['voltage']} V（最低允许 {v['min_voltage']} V）"
            for v in under_voltage_nodes
        ]
        raise BaselineViolationError("基线校核已存在超额定电流或欠压，无法评估可增容量", details)

    # 目标到 source 路径上的线段
    seg_by_child = {seg.child: seg for seg in tree.segments}
    path_seg_ids: set[str] = set()
    node = target_node
    while node != ROOT_NODE:
        seg = seg_by_child[node]
        path_seg_ids.add(seg.id)
        node = seg.parent

    # 各节点与目标节点两条根路径的公共线段电阻之和（自根向叶累计）
    common_resistance = {ROOT_NODE: 0.0}
    for node in tree.order:
        for seg in tree.children_map.get(node, []):
            shared = seg.resistance if seg.id in path_seg_ids else 0.0
            common_resistance[seg.child] = common_resistance[node] + shared

    # 候选上限：(可追加电流, 限制类型, 标识)
    limits: list[tuple[float, str, str]] = []
    for seg in tree.segments:  # 保持输入顺序
        if seg.id not in path_seg_ids:
            continue
        limits.append((seg.rated_current - tree.seg_current[seg.id], "segment", seg.id))
    for node in tree.order:  # BFS 顺序，根在前
        r_common = common_resistance[node]
        if r_common <= 0.0:
            continue  # 零公共电阻：该节点电压不受新增负载影响
        headroom = tree.node_voltage[node] - _node_threshold(tree, node, min_voltage)
        limits.append((headroom / r_common, "node", node))

    raw_max = min(limit for limit, _, _ in limits)
    limiting_factors = [
        {"type": kind, "id": ident}
        for limit, kind, ident in limits
        if limit <= raw_max + EPS
    ]

    return {
        "target_node": target_node,
        "max_additional_current": _truncate(max(0.0, raw_max)),
        "limiting_factors": limiting_factors,
    }


def unregistered_load(
    source_voltage: float,
    min_voltage: float,
    segments_in: Sequence,
    loads_in: Sequence,
    measurements_in: Sequence,
) -> dict:
    """定位唯一未登记的正恒定负载。

    假定恰有一个非 source 节点挂接未登记的正恒定负载 I_u，则任一实测线段的
    读数 = 未舍入基线电流 + I_u（负载位于该段下游）或 = 基线电流（否则）。
    判定流程（容差 MEASURE_TOL = 1e-6 A）：

    1. 任一测点差值 < -容差：正负载不可能使实测低于基线 → inconsistent；
    2. 无正差（所有差值 ≤ 容差）：负载电流无法确定 → undetermined；
    3. 正差彼此极差 > 容差：互相矛盾 → inconsistent；
    4. 否则 I_u 取各正差的均值，候选节点须满足：其根路径上的实测线段集合
       恰好等于正差线段集合（路径上的测点必须观测到增量，路径外的测点不得
       观测到增量）；未测支路不施加约束，因此允许多解；
    5. 无相容节点 → inconsistent，否则 located 并返回六位小数负载电流。
    """
    tree = _prepare_tree(source_voltage, segments_in, loads_in)

    # 实测校验：线段 id 唯一且存在于树中
    known_ids = {seg.id for seg in tree.segments}
    errors: list[str] = []
    seen_ids: set[str] = set()
    for m in measurements_in:
        if m.segment_id in seen_ids:
            errors.append(f"实测线段 id 重复: '{m.segment_id}'")
        seen_ids.add(m.segment_id)
        if m.segment_id not in known_ids:
            errors.append(f"实测线段 '{m.segment_id}' 不存在于树中")
    if errors:
        raise InputValidationError("实测电流定义无效", errors)

    # 与未舍入基线线段电流比较（保持输入顺序）
    comparisons = []
    diffs: dict[str, float] = {}
    for m in measurements_in:
        baseline = tree.seg_current[m.segment_id]
        diff = m.current - baseline
        diffs[m.segment_id] = diff
        comparisons.append(
            {
                "segment_id": m.segment_id,
                "measured": m.current,
                "baseline": _round(baseline),
                "difference": _round(diff),
            }
        )

    def _result(status: str, load_current: Optional[float] = None, candidates: Optional[list[str]] = None) -> dict:
        return {
            "status": status,
            "load_current": load_current,
            "candidate_nodes": candidates or [],
            "measurements": comparisons,
        }

    # 负差（超出容差）：正负载不可能使实测低于基线
    if any(diff < -MEASURE_TOL for diff in diffs.values()):
        return _result("inconsistent")

    positive_ids = {sid for sid, diff in diffs.items() if diff > MEASURE_TOL}
    if not positive_ids:
        return _result("undetermined")

    # 所有正差须在容差内一致（极差 ≤ 容差）
    positive_values = [diffs[sid] for sid in positive_ids]
    if max(positive_values) - min(positive_values) > MEASURE_TOL:
        return _result("inconsistent")

    load_current = sum(positive_values) / len(positive_values)

    # 自根向叶累计各节点根路径上的实测线段集合，筛选与正差集合一致的节点
    candidates: list[str] = []
    path_measured: dict[str, frozenset[str]] = {ROOT_NODE: frozenset()}
    for node in tree.order:  # BFS 顺序，父节点先于子节点
        inherited = path_measured[node]
        for seg in tree.children_map.get(node, []):
            child_set = inherited | ({seg.id} if seg.id in diffs else frozenset())
            path_measured[seg.child] = frozenset(child_set)
            if child_set == positive_ids:
                candidates.append(seg.child)

    if not candidates:
        return _result("inconsistent")

    return _result("located", load_current=_round(load_current), candidates=candidates)
