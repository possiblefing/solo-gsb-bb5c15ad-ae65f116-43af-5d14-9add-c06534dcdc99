"""审查核心流程：解析两版配方、传播过敏原、标签核对，并产出字典指纹。

新建审查（main.create_review）与历史复核（recheck / main.recheck_review）
共用本模块，保证“确认复核”后覆盖的结果与当时新建审查的计算口径完全一致。

原料别名仅在本流程接入：解析时先沿别名链把原料名解析到最终标准名再查
过敏原字典（未识别判定同样针对标准名），但配料树节点名称始终保留配方
原文，故报告与遗漏来源路径展示的仍是用户录入的名称。
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Set

from . import db
from .aliases import AliasResolver
from .analysis import (attach_allergens, collect_allergens, compare_label,
                       diff_allergens, diff_ingredients, flatten)
from .fingerprint import recipe_keys, scoped_fingerprint
from .parser import IngredientNode, parse_recipe


def build_version(tree: List[IngredientNode]) -> dict:
    return {
        "tree": [node.to_dict() for node in tree],
        "allergens": sorted(collect_allergens(tree)),
        "unknown_ingredients": sorted({n.name for n in flatten(tree) if n.unknown}),
    }


def _parse_version(text: str, dictionary: Dict[str, Set[str]],
                   resolve: Optional[Callable[[str], str]] = None):
    tree = parse_recipe(text)
    attach_allergens(tree, dictionary, resolve)
    return tree, build_version(tree)


def analyze(*, text_a: str, text_b: Optional[str], label_a: str, label_b: str,
            declared: List[str], dictionary: Optional[Dict[str, Set[str]]] = None,
            resolver: Optional[AliasResolver] = None):
    """用给定（默认当前）字典与别名规则解析配方并构建审查结果。

    返回 (result, fingerprint)：
    - result 结构与历史上持久化的结果 JSON 完全一致（树中保留原始名称）；
    - fingerprint 为 A/B 配方实际涉及原料按最终标准名取映射的稳定指纹
      （A、B 两版涉及原料取并集）。

    显式传入 dictionary 时默认不应用别名（与直接单测的旧口径一致）；
    走正式审查/复核全链路（dictionary 取默认当前字典）时自动加载当前别名。
    """
    use_current = dictionary is None
    if use_current:
        dictionary = db.dict_lookup()
        resolver = db.alias_resolver()
    resolve = resolver.resolve if resolver is not None else None

    tree_a, version_a = _parse_version(text_a, dictionary, resolve)

    result = {
        "version_a": version_a,
        "version_b": None,
        "diff": None,
        "ingredient_changes": None,
        # 标签默认对照现版配方；若提供了新版配方，则对照新版
        "label_check_version": label_a,
    }
    keys = recipe_keys(text_a)
    check_allergens = version_a["allergens"]
    checked_tree = version_a["tree"]

    tree_b = None
    if text_b and text_b.strip():
        tree_b, version_b = _parse_version(text_b, dictionary, resolve)
        result["version_b"] = version_b
        result["diff"] = diff_allergens(
            version_a["allergens"], version_b["allergens"])
        result["ingredient_changes"] = diff_ingredients(tree_a, tree_b)
        result["label_check_version"] = label_b
        check_allergens = version_b["allergens"]
        checked_tree = version_b["tree"]
        keys |= recipe_keys(text_b)

    result["label_check"] = compare_label(
        check_allergens, declared, checked_tree)
    return result, scoped_fingerprint(keys, dictionary, resolve)
