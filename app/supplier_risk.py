"""供应商方案风险区间（临时预览，不写入字典或历史记录）。

用户可为配方中的若干原料输入若干**互斥**的供应商过敏原方案（格式
``原料：方案1；方案2``，方案内逗号分隔，空方案写「无」），未覆盖的原料
沿用现有过敏原字典。

后端**不枚举方案组合**：互斥方案只作用于单个原料节点，在嵌套配料树上
让每个节点同时维护两个过敏原集合并分别向上传播：

- necessary（必然）—— 该节点各互斥方案过敏原的**交集**，无论选哪个方案
  都一定含有；
- possible（可能） —— 各方案过敏原的**并集**，仅在选中某些方案时含有。

传播规则与常规审查同构（子配料向父配料汇总），只是同时传播两个集合：

- total_necessary = own_necessary ∪ ⋃ child.total_necessary
- total_possible  = own_possible  ∪ ⋃ child.total_possible

再据标签声明把每种过敏原归入五类：

- certain_missing    必然含有且标签未声明 —— 确定遗漏；
- potential_missing  可能含有（不必然）且标签未声明 —— 潜在遗漏；
- certain_over       标签声明但整棵树不可能含有 —— 确定多报；
- potential_over     标签声明但仅部分方案可能引入 —— 潜在多报；
- matched            标签声明且必然含有 —— 确定一致。

两类遗漏分别给出配料树中的来源路径：确定遗漏取自身必然携带
（own_necessary）的节点，潜在遗漏取自身仅部分方案携带
（own_possible - own_necessary）的节点，口径与
``analysis.missing_allergen_sources`` 一致（子配料传播不重复计入父级）。

当配方涉及的所有被覆盖原料都只有一个方案时，necessary == possible，
风险区间退化为单值，五类结果与现有审查（以单方案覆盖字典后）完全一致。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set

from . import db
from .analysis import flatten, normalize_name
from .parser import IngredientNode, parse_recipe

PLAN_SEPARATORS = ";；"
ALLERGEN_SEPARATORS = ",，、"
EMPTY_PLAN = "无"


class SupplierPlanError(ValueError):
    """供应商方案文本格式错误（映射为 HTTP 400）。"""


@dataclass
class SupplierOptions:
    """一种原料的互斥方案列表；options 已按集合内容去重并保留输入顺序。"""

    name: str
    options: List[Set[str]]


def parse_supplier_plans(text: str) -> Dict[str, SupplierOptions]:
    """解析方案文本为 ``{归一化原料名: SupplierOptions}``。

    每行一条：``原料：方案1；方案2``；方案之间用分号（中英文均可）分隔，
    方案内用逗号/顿号分隔过敏原，恰好为「无」的方案表示空过敏原集合；
    # 开头为注释，空行忽略。同名原料重复出现、缺少冒号、没有任何方案等
    情况抛出 :class:`SupplierPlanError`。
    """
    plans: Dict[str, SupplierOptions] = {}
    for lineno, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = re.split(r"[:：]", line, maxsplit=1)
        if len(parts) != 2:
            raise SupplierPlanError(
                f"第 {lineno} 行格式应为「原料：方案1；方案2」：{line}")
        name = parts[0].strip()
        if not name:
            raise SupplierPlanError(f"第 {lineno} 行原料名不能为空")
        key = normalize_name(name)
        if key in plans:
            raise SupplierPlanError(
                f"原料「{name}」在方案文本中重复出现，请合并为一行")

        options: List[Set[str]] = []
        seen: Set[frozenset] = set()
        for chunk in re.split(f"[{PLAN_SEPARATORS}]", parts[1]):
            piece = chunk.strip()
            if not piece:
                continue  # 容忍行尾多余分号
            if piece == EMPTY_PLAN:
                allergens: Set[str] = set()
            else:
                tokens = {t.strip() for t in
                          re.split(f"[{ALLERGEN_SEPARATORS}]", piece)
                          if t.strip()}
                if EMPTY_PLAN in tokens:
                    raise SupplierPlanError(
                        f"原料「{name}」的方案中「{EMPTY_PLAN}」"
                        f"只能单独使用，表示空方案")
                allergens = tokens
            signature = frozenset(allergens)
            if signature not in seen:  # 完全相同的方案不重复制造歧义
                seen.add(signature)
                options.append(allergens)
        if not options:
            raise SupplierPlanError(
                f"原料「{name}」至少需要一个方案；"
                f"若该原料不含过敏原请写「{name}：{EMPTY_PLAN}」")
        plans[key] = SupplierOptions(name=name, options=options)
    return plans


@dataclass
class RiskNode:
    """风险区间配料树节点。

    own_necessary / own_possible   —— 原料自身的必然 / 可能过敏原
        （字典原料二者相同；方案原料分别为各方案交集 / 并集）；
    total_necessary / total_possible —— 自身叠加全部子配料传播后的结果；
    unknown    —— 叶子原料既未在字典登记、也无供应商方案；
    overridden —— 该原料被供应商方案覆盖（不再使用字典）；
    plan_count —— 互斥方案数量（去重后）。
    """

    name: str
    children: List["RiskNode"] = field(default_factory=list)
    own_necessary: Set[str] = field(default_factory=set)
    own_possible: Set[str] = field(default_factory=set)
    total_necessary: Set[str] = field(default_factory=set)
    total_possible: Set[str] = field(default_factory=set)
    unknown: bool = False
    overridden: bool = False
    plan_count: int = 0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "children": [c.to_dict() for c in self.children],
            "own_necessary": sorted(self.own_necessary),
            "own_possible": sorted(self.own_possible),
            "total_necessary": sorted(self.total_necessary),
            "total_possible": sorted(self.total_possible),
            "unknown": self.unknown,
            "overridden": self.overridden,
            "plan_count": self.plan_count,
        }


def build_risk_tree(nodes: List[IngredientNode],
                    plans: Dict[str, SupplierOptions],
                    dictionary: Dict[str, Set[str]]) -> List[RiskNode]:
    """在配料树上标注并传播必然 / 可能过敏原（不枚举方案组合）。"""
    out: List[RiskNode] = []
    for node in nodes:
        key = normalize_name(node.name)
        children = build_risk_tree(node.children, plans, dictionary)
        spec = plans.get(key)
        if spec is not None:
            opts = spec.options
            # 交集 / 并集只在同一原料的各方案间计算一次，随后按树传播，
            # 复杂度与方案数量线性相关，与方案组合数（笛卡尔积）无关。
            own_necessary = set.intersection(*opts)
            own_possible = set.union(*opts)
            overridden, plan_count = True, len(opts)
        else:
            own = set(dictionary.get(key, set()))
            own_necessary = own_possible = own
            overridden, plan_count = False, 0
        total_necessary = set(own_necessary)
        total_possible = set(own_possible)
        for child in children:
            total_necessary |= child.total_necessary
            total_possible |= child.total_possible
        # 叶子原料既无字典映射又无供应商方案时才提示未识别；
        # 「原料：无」是供应商的明确空方案承诺，不属于未识别。
        unknown = key not in dictionary and spec is None and not children
        out.append(RiskNode(
            name=node.name, children=children,
            own_necessary=own_necessary, own_possible=own_possible,
            total_necessary=total_necessary, total_possible=total_possible,
            unknown=unknown, overridden=overridden, plan_count=plan_count))
    return out


def _flatten_risk(nodes: List[RiskNode]) -> List[RiskNode]:
    out: List[RiskNode] = []
    for node in nodes:
        out.append(node)
        out.extend(_flatten_risk(node.children))
    return out


def _source_paths(tree: List[RiskNode],
                  own_of: Callable[[RiskNode], Set[str]]) -> Dict[str, list]:
    """按节点自身过敏原判定收集全部来源路径（同名分支分别保留）。"""
    hit: Dict[str, Set[tuple]] = {}

    def walk(nodes: List[RiskNode], prefix: tuple) -> None:
        for node in nodes:
            path = prefix + (node.name,)
            for allergen in own_of(node):
                hit.setdefault(allergen, set()).add(path)
            walk(node.children, path)

    walk(tree, ())
    return {allergen: [list(p) for p in sorted(paths)]
            for allergen, paths in sorted(hit.items()) if paths}


def analyze_supplier_risk(*, text: str, plans_text: str,
                          declared: List[str],
                          dictionary: Optional[Dict[str, Set[str]]] = None) -> dict:
    """供应商方案风险区间预览的完整计算（只读字典，不落任何数据）。"""
    dictionary = db.dict_lookup() if dictionary is None else dictionary
    plans = parse_supplier_plans(plans_text)

    parsed = parse_recipe(text)
    tree = build_risk_tree(parsed, plans, dictionary)

    necessary: Set[str] = set()
    possible: Set[str] = set()
    for node in tree:
        necessary |= node.total_necessary
        possible |= node.total_possible
    declared_set = set(declared)

    certain_missing = necessary - declared_set
    possible_only = possible - necessary
    potential_missing = possible_only - declared_set
    certain_over = declared_set - possible
    potential_over = declared_set & possible_only

    certain_paths = _source_paths(tree, lambda n: set(n.own_necessary))
    potential_paths = _source_paths(
        tree, lambda n: n.own_possible - n.own_necessary)

    present_keys = {normalize_name(n.name) for n in flatten(parsed) if n.name}
    overrides = []
    for key in sorted(plans):
        spec = plans[key]
        overrides.append({
            "ingredient": spec.name,
            "plans": [sorted(option) for option in spec.options],
            "used": key in present_keys,
        })

    return {
        "tree": [node.to_dict() for node in tree],
        "allergens_necessary": sorted(necessary),
        "allergens_possible": sorted(possible),
        "unknown_ingredients": sorted(
            {n.name for n in _flatten_risk(tree) if n.unknown}),
        "overrides": overrides,
        "unused_plan_ingredients":
            [o["ingredient"] for o in overrides if not o["used"]],
        # 配方涉及的每个被覆盖原料都只有单一方案时，区间退化为单值，
        # 结果与现有审查口径一致（无方案时同样为 True）。
        "single_plan": all(len(plans[key].options) == 1
                           for key in present_keys if key in plans),
        "label_check": {
            "certain_missing": sorted(certain_missing),
            "potential_missing": sorted(potential_missing),
            "certain_over_reported": sorted(certain_over),
            "potential_over_reported": sorted(potential_over),
            "matched": sorted(declared_set & necessary),
            "certain_missing_sources":
                {a: certain_paths.get(a, []) for a in sorted(certain_missing)
                 if certain_paths.get(a)},
            "potential_missing_sources":
                {a: potential_paths.get(a, []) for a in sorted(potential_missing)
                 if potential_paths.get(a)},
        },
    }
