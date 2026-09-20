"""目标过敏原最小改配方案（临时预览，不改写审查记录）。

从历史审查记录中选择一条**时效正常**的记录与一个目标过敏原，复用其
**当前配方树**（两版记录取标签核对对象，即新版配方；单版取现版），
列出目标过敏原在该树中的全部来源路径：路径上每个节点都是候选替换点，
用户按**完整节点路径**逐条填写可替换成本，未填写成本的节点视为不可替换。

后端在由来源路径前缀构成的候选树上做**树形动态规划**，求覆盖全部来源
路径的最低总成本节点集合：

- 选择某个节点（替换该配料）即同时阻断其整棵子树中的全部来源，方案中
  不得再选择它的任何后代（所选节点互为反链）；
- 一个携带过敏原的节点若未被选择，其自身来源无法被后代「向上」覆盖，
  只能靠选择它或它的某个祖先阻断；
- 每个子问题在成本最优的前提下，保留所选节点**完整路径序列**（升序）
  字典序最小者，并统计达到该最优值的不同方案数（用于并列提示）；
- 若某条来源路径上没有任何节点填写成本，则该来源不可能被覆盖，直接
  返回无解并逐条列出仍不可覆盖的来源。

成本经 ``Fraction(str(...))`` 精确化，比较与并列判定不受浮点误差影响。

记录缺失（不存在 / 已删除）或处于**需复核**状态（旧记录无指纹、相关
原料映射已变更）时禁止使用。整个模块只读：不写入字典，也不改写任何
审查记录。
"""
from __future__ import annotations

import math
from fractions import Fraction
from itertools import chain
from typing import Dict, List, Optional, Tuple

from . import db
from .analysis import missing_allergen_sources
from .changeover import blocked_message, product_of

# 子问题的统一表示：(成本, 所选路径升序元组, 并列方案数)；不可行时为 None
State = Optional[Tuple[Fraction, tuple, int]]


class ReformulationError(ValueError):
    """改配方案请求无法完成（映射为 HTTP 400）。

    problems 可选地逐条列出禁止使用的记录及原因，供页面定位。
    """

    def __init__(self, message: str, problems: Optional[List[dict]] = None):
        super().__init__(message)
        self.problems = problems or []


class _Node:
    """候选树（来源路径前缀树）节点。

    path      —— 从配方顶层到本节点的完整路径；
    is_source  —— 本节点自身携带目标过敏原（来源路径的末端点之一）；
    children  —— 子节点，按路径字典序排列。
    """

    __slots__ = ("name", "path", "is_source", "children")

    def __init__(self, name: Optional[str], path: tuple, is_source: bool = False):
        self.name = name
        self.path = path
        self.is_source = is_source
        self.children: List["_Node"] = []

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "path": list(self.path),
            "is_source": self.is_source,
            "children": [c.to_dict() for c in self.children],
        }


def list_reformulation_records() -> List[dict]:
    """候选记录清单：每条历史记录的当前配方最终过敏原集合与参与资格。"""
    records = []
    for summary in db.list_reviews():
        review = db.get_review(summary["id"])
        needs = bool(review.get("needs_recheck"))
        reason = review.get("recheck_reason")
        records.append({
            **product_of(review),
            "created_at": review["created_at"],
            "has_two_versions": bool(review.get("text_b")),
            "eligible": not needs,
            "blocked_reason": blocked_message(reason) if needs else None,
        })
    records.sort(key=lambda r: r["id"])
    return records


def _current_version(review: dict) -> dict:
    """记录当前配方（标签核对对象：有新版取新版，否则取现版）的版本结果。"""
    result = review.get("result") or {}
    return result.get("version_b") or result.get("version_a") or {}


def _load_eligible_review(review_id: int):
    """取出记录并校验参与资格，返回 (记录, 当前版本结果)。"""
    try:
        rid = int(review_id)
    except (TypeError, ValueError):
        raise ReformulationError(
            "记录 ID 无效",
            problems=[{"id": review_id, "name": None, "reason": "invalid",
                       "message": "记录 ID 无效，禁止选择"}])
    review = db.get_review(rid)
    if review is None:
        raise ReformulationError(
            "审查记录不存在或已删除",
            problems=[{"id": rid, "name": None, "reason": "missing",
                       "message": "审查记录不存在或已删除，禁止选择"}])
    if review.get("needs_recheck"):
        reason = review.get("recheck_reason")
        raise ReformulationError(
            "记录处于需复核状态，禁止选择",
            problems=[{"id": review["id"], "name": review["name"],
                       "reason": reason or "unknown",
                       "message": f"记录{blocked_message(reason)}，禁止选择"}])
    return review, _current_version(review)


def _validate_allergen(version: dict, allergen: str) -> str:
    name = (allergen or "").strip()
    if not name:
        raise ReformulationError("目标过敏原不能为空")
    allergens = set(version.get("allergens", []))
    if name not in allergens:
        raise ReformulationError(
            f"目标过敏原「{name}」不在该记录当前配方的最终过敏原集合中")
    return name


def _source_paths(tree: List[dict], allergen: str) -> List[List[str]]:
    """目标过敏原在配料树中的全部来源路径（路径整体字典序升序、结构去重）。"""
    return missing_allergen_sources(tree, [allergen]).get(allergen, [])


def _build_candidate_tree(sources: List[List[str]]) -> _Node:
    """由全部来源路径构造前缀候选树：每个前缀节点都是一个候选替换点。"""
    root = _Node(None, (), is_source=False)
    endpoints = {tuple(p) for p in sources}
    index = {(): root}
    for path in sources:
        prefix: Tuple[str, ...] = ()
        for name in path:
            parent = index[prefix]
            prefix = prefix + (name,)
            node = index.get(prefix)
            if node is None:
                node = _Node(name, prefix, is_source=prefix in endpoints)
                index[prefix] = node
                parent.children.append(node)
            elif prefix in endpoints:
                node.is_source = True
    for node in index.values():
        node.children.sort(key=lambda c: c.path)
    return root


def _review_meta(review: dict) -> dict:
    result = review.get("result") or {}
    return {
        "id": review["id"],
        "name": review["name"],
        "source_version": result.get("label_check_version")
        or review.get("label_a") or "现版配方",
        "has_two_versions": bool(review.get("text_b")),
    }


def get_reformulation_sources(review_id: int, allergen: str) -> dict:
    """返回目标过敏原的全部来源路径与候选替换点（只读）。"""
    review, version = _load_eligible_review(review_id)
    name = _validate_allergen(version, allergen)
    sources = _source_paths(version.get("tree", []), name)
    root = _build_candidate_tree(sources)
    nodes: List[dict] = []

    def collect(node: _Node) -> None:
        if node.path:
            nodes.append({"path": list(node.path), "name": node.name,
                          "is_source": node.is_source})
        for child in node.children:
            collect(child)

    collect(root)
    return {
        "review": _review_meta(review),
        "allergen": name,
        "allergens": sorted(version.get("allergens", [])),
        "sources": sources,
        "candidate_tree": root.to_dict(),
        "candidate_nodes": nodes,
    }


def _json_number(value: Fraction):
    """Fraction → JSON 可序列化数值（整数保留 int，其余转 float）。"""
    return int(value) if value.denominator == 1 else float(value)


def _parse_cost(value, path: tuple) -> Fraction:
    """成本校验：必须是不小于 0 的有限数值（经 Fraction(str) 精确化）。"""
    label = " › ".join(path)
    if isinstance(value, bool):
        raise ReformulationError(f"节点「{label}」的可替换成本必须是数值")
    try:
        cost = Fraction(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        raise ReformulationError(
            f"节点「{label}」的可替换成本不是有效数字：{value!r}")
    if not math.isfinite(float(cost)) or cost < 0:
        raise ReformulationError(
            f"节点「{label}」的可替换成本必须是不小于 0 的有限数值")
    return cost


def _validate_costs(raw_costs, root: _Node) -> Dict[tuple, Fraction]:
    """解析用户填写的节点成本：路径必须命中候选节点、不可重复、成本非负。

    未出现在列表中的节点一律视为不可替换（不填即无成本）。
    """
    valid_paths = set()

    def collect(node: _Node) -> None:
        if node.path:
            valid_paths.add(node.path)
        for child in node.children:
            collect(child)

    collect(root)
    prices: Dict[tuple, Fraction] = {}
    for item in raw_costs or []:
        if not isinstance(item, dict):
            raise ReformulationError("成本条目格式不正确：需要 {path, cost}")
        raw_path = item.get("path")
        if not isinstance(raw_path, (list, tuple)) or not raw_path \
                or not all(isinstance(p, str) and p.strip() for p in raw_path):
            raise ReformulationError(
                "成本条目缺少有效的完整节点路径（非空字符串数组）")
        path = tuple(p.strip() for p in raw_path)
        if path not in valid_paths:
            raise ReformulationError(
                f"节点路径「{' › '.join(path)}」不在目标过敏原的来源路径上，"
                "不能填写可替换成本")
        if path in prices:
            raise ReformulationError(
                f"节点「{' › '.join(path)}」的可替换成本重复填写")
        prices[path] = _parse_cost(item.get("cost"), path)
    return prices


def _solve(node: _Node, prices: Dict[tuple, Fraction]) -> State:
    """树形动态规划。

    返回子树内覆盖全部来源的最优 ``(成本, 所选路径升序元组, 并列数)``；
    不存在任何可行选择时返回 None。

    - 选择本节点（需已填写成本）：成本即节点成本，阻断整个子树；
    - 不选择本节点：本节点自身是来源时不可行（后代无法向上覆盖），
      否则要求每个含来源的子树都可行，成本与并列数相乘、路径序列归并。
    """
    options: List[Tuple[Fraction, tuple, int]] = []
    if node.path in prices:
        options.append((prices[node.path], (node.path,), 1))

    child_states = [_solve(child, prices) for child in node.children]
    # 跳过本节点要求：自身不是来源，且每个含来源子树都有可行方案
    # （候选树中每个子节点都是某条来源路径的前缀，故子树均含来源）
    if not node.is_source and all(st is not None for st in child_states):
        cost = sum((st[0] for st in child_states), Fraction(0))
        key = tuple(sorted(chain.from_iterable(st[1] for st in child_states)))
        ways = 1
        for st in child_states:
            ways *= st[2]
        options.append((cost, key, ways))

    if not options:
        return None
    best_cost = min(opt[0] for opt in options)
    tied = [opt for opt in options if opt[0] == best_cost]
    best_key = min(opt[1] for opt in tied)
    best_ways = sum(opt[2] for opt in tied)
    return best_cost, best_key, best_ways


def _uncoverable_sources(sources: List[List[str]],
                         prices: Dict[tuple, Fraction]) -> List[List[str]]:
    """整条路径上没有任何已填写成本节点的来源，不可能被任何方案覆盖。"""
    priced = set(prices)
    uncoverable = []
    for path in sources:
        prefix: Tuple[str, ...] = ()
        hit = False
        for name in path:
            prefix += (name,)
            if prefix in priced:
                hit = True
                break
        if not hit:
            uncoverable.append(list(path))
    return uncoverable


def build_reformulation_plan(review_id: int, allergen: str,
                             raw_costs: Optional[List[dict]] = None) -> dict:
    """改配方案预览的完整编排：校验资格与成本后求树形 DP 最优（只读）。"""
    review, version = _load_eligible_review(review_id)
    name = _validate_allergen(version, allergen)
    sources = _source_paths(version.get("tree", []), name)
    if not sources:
        # 过敏原在最终集合中却没有自身标注来源，属于数据不一致，防御性报错
        raise ReformulationError(
            f"目标过敏原「{name}」在当前配方树中没有来源路径，无法计算改配方案")
    root = _build_candidate_tree(sources)
    prices = _validate_costs(raw_costs, root)

    meta = _review_meta(review)
    uncoverable = _uncoverable_sources(sources, prices)
    if uncoverable:
        return {
            "feasible": False,
            "review": meta,
            "allergen": name,
            "sources": sources,
            "total_cost": None,
            "optimal_count": 0,
            "selections": [],
            "uncoverable_sources": uncoverable,
        }

    cost, key, ways = _solve(root, prices)
    selections = []
    for path in key:
        blocked = [list(p) for p in sources
                   if tuple(p)[:len(path)] == path]
        selections.append({
            "path": list(path),
            "name": path[-1],
            "cost": _json_number(prices[path]),
            "blocked_sources": blocked,
            "blocked_count": len(blocked),
        })
    return {
        "feasible": True,
        "review": meta,
        "allergen": name,
        "sources": sources,
        "total_cost": _json_number(cost),
        "optimal_count": ways,
        "selections": selections,
        "uncoverable_sources": [],
    }
