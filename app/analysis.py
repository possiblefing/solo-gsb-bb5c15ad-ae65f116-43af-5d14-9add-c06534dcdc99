"""过敏原传播、标签核对与版本比对。"""
from __future__ import annotations

from typing import Callable, Dict, Iterable, List, Optional, Set

from .parser import IngredientNode


def normalize_name(name: str) -> str:
    """原料名归一化：去空白、转小写，便于字典匹配。"""
    return "".join(name.split()).lower()


def attach_allergens(nodes: List[IngredientNode],
                     dictionary: Dict[str, Set[str]],
                     resolve: Optional[Callable[[str], str]] = None
                     ) -> List[IngredientNode]:
    """按字典标注每种原料的过敏原，并把子配料的过敏原向上传播。

    dictionary 的 key 必须是 normalize_name 之后的形式。
    传入 resolve 时（原料别名链解析），先把原料归一化名解析到**最终
    标准名**再查字典；节点 ``name`` 始终保留配方中的原始名称，
    因此配料树与遗漏来源路径展示的仍是用户录入的原料名。
    """
    for node in nodes:
        key = normalize_name(node.name)
        standard = resolve(key) if resolve is not None else key
        node.own_allergens = set(dictionary.get(standard, set()))
        attach_allergens(node.children, dictionary, resolve)
        total = set(node.own_allergens)
        for child in node.children:
            total |= child.total_allergens
        node.total_allergens = total
        # 只有叶子原料才提示“未识别”；复合原料的过敏原来自子配料
        node.unknown = standard not in dictionary and not node.children
    return nodes


def collect_allergens(nodes: List[IngredientNode]) -> Set[str]:
    """整份配方的过敏原全集（各顶层配料传播结果的并集）。"""
    result: Set[str] = set()
    for node in nodes:
        result |= node.total_allergens
    return result


def flatten(nodes: List[IngredientNode]) -> List[IngredientNode]:
    """把树拍平成列表（先序遍历）。"""
    out: List[IngredientNode] = []
    for node in nodes:
        out.append(node)
        out.extend(flatten(node.children))
    return out


def compare_label(computed: Iterable[str], declared: Iterable[str],
                  checked_tree: Optional[List[dict]] = None) -> dict:
    """标签核对：计算值 vs 标签声明值。

    missing       —— 配方含有但标签未声明（遗漏，必须补充）；
    over_reported —— 标签声明了但配方中不存在（多报）；
    matched       —— 两边一致；
    missing_sources —— 每个遗漏过敏原在配料树中的全部来源路径
                      （仅当传入 checked_tree 时附带）。
    """
    computed, declared = set(computed), set(declared)
    missing = sorted(computed - declared)
    result = {
        "missing": missing,
        "over_reported": sorted(declared - computed),
        "matched": sorted(computed & declared),
    }
    if checked_tree is not None:
        result["missing_sources"] = missing_allergen_sources(checked_tree, missing)
    return result


def missing_allergen_sources(tree: List[dict], allergens: Iterable[str]) -> dict:
    """为每个过敏原收集配料树中携带该过敏原的原料（自身标注）的全部来源路径。

    路径为从顶层配料到具体原料的名称序列，例如
    ``["巧克力酱", "奶粉", "乳清粉"]`` 表示「巧克力酱 > 奶粉 > 乳清粉」。
    同名原料出现在不同分支时视为不同节点，路径分别保留、不做合并；
    每条路径在树中只会产生一次（结构去重），最终按路径整体稳定排序。

    tree 为节点 dict（IngredientNode.to_dict() 或历史记录反序列化的结果），
    判定依据是每个节点自身的 own_allergens，子配料传播不算独立来源，
    因此同一过敏原既被父级自身标注、又由子配料携带时，两条路径都会保留。
    """
    wanted = set(allergens)
    hit: Dict[str, Set[tuple]] = {a: set() for a in wanted}

    def walk(nodes: List[dict], prefix: tuple) -> None:
        for node in nodes:
            path = prefix + (node.get("name", ""),)
            own = set(node.get("own_allergens", ()))
            for allergen in own & wanted:
                hit[allergen].add(path)
            walk(node.get("children", []), path)

    walk(tree, ())
    return {allergen: [list(path) for path in sorted(hit[allergen])]
            for allergen in sorted(wanted) if hit[allergen]}


def diff_allergens(old: Iterable[str], new: Iterable[str]) -> dict:
    """两版配方的过敏原集合差异。"""
    old, new = set(old), set(new)
    return {
        "added": sorted(new - old),
        "removed": sorted(old - new),
        "unchanged": sorted(old & new),
    }


def diff_ingredients(tree_a: List[IngredientNode],
                     tree_b: List[IngredientNode]) -> List[dict]:
    """按顶层配料名比对两版配方，给出每种配料的过敏原变化。"""
    map_a = {normalize_name(n.name): n for n in tree_a}
    map_b = {normalize_name(n.name): n for n in tree_b}
    changes: List[dict] = []
    for key in sorted(set(map_a) | set(map_b)):
        na, nb = map_a.get(key), map_b.get(key)
        name = (nb or na).name
        if na is None:
            changes.append({"ingredient": name, "change": "added",
                            "allergens": sorted(nb.total_allergens)})
        elif nb is None:
            changes.append({"ingredient": name, "change": "removed",
                            "allergens": sorted(na.total_allergens)})
        else:
            added = nb.total_allergens - na.total_allergens
            removed = na.total_allergens - nb.total_allergens
            if added or removed:
                changes.append({"ingredient": name, "change": "modified",
                                "added": sorted(added),
                                "removed": sorted(removed)})
    return changes
