"""配方文本解析：把带嵌套括号的配料表解析为树结构。

语法约定：
- 顶层配料之间用逗号（中英文）、顿号、分号或换行分隔；
- 圆括号（中英文均可）表示子配料，可任意嵌套，
  例如：巧克力酱（可可脂，奶粉（乳清粉，乳糖），白砂糖）；
- 行首的“配料：”“配料表：”“成分：”“Ingredients:”等前缀会被自动去除。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Set

OPEN_PARENS = "(（"
CLOSE_PARENS = ")）"
SEPARATORS = ",，、;；\n"

PREFIXES = {
    "配料", "配料表", "成分", "成分表", "原料", "原料表",
    "ingredients", "ingredient",
}


@dataclass
class IngredientNode:
    """配料树节点：name 为原料名，children 为子配料。

    own_allergens   —— 字典中该原料自身标注的过敏原；
    total_allergens —— 自身 + 全部后代传播上来的过敏原集合；
    unknown         —— 叶子原料且未在过敏原字典中登记。
    """

    name: str
    children: List["IngredientNode"] = field(default_factory=list)
    own_allergens: Set[str] = field(default_factory=set)
    total_allergens: Set[str] = field(default_factory=set)
    unknown: bool = False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "own_allergens": sorted(self.own_allergens),
            "total_allergens": sorted(self.total_allergens),
            "unknown": self.unknown,
            "children": [c.to_dict() for c in self.children],
        }


def strip_prefix(text: str) -> str:
    """去掉“配料表：”之类的表头前缀。"""
    for sep in ("：", ":"):
        head, found, tail = text.partition(sep)
        if found and head.strip().lower() in PREFIXES:
            return tail.strip()
    return text.strip()


def parse_recipe(text: str) -> List[IngredientNode]:
    """把配方文本解析为顶层配料节点列表。括号不配对时尽量容错解析。"""
    text = strip_prefix(text or "")
    nodes, _ = _parse_level(text, 0)
    return nodes


def _flush(buf: List[str], nodes: List[IngredientNode]) -> None:
    name = "".join(buf).strip()
    if name:
        nodes.append(IngredientNode(name=name))
    buf.clear()


def _parse_level(text: str, pos: int):
    """解析一层括号内的内容，返回 (节点列表, 结束位置)。"""
    nodes: List[IngredientNode] = []
    buf: List[str] = []
    i, n = pos, len(text)
    while i < n:
        ch = text[i]
        if ch in OPEN_PARENS:
            name = "".join(buf).strip()
            buf.clear()
            children, i = _parse_level(text, i + 1)
            if name:
                nodes.append(IngredientNode(name=name, children=children))
            else:
                # 括号前没有名字，视为匿名分组，子项直接并入当前层
                nodes.extend(children)
        elif ch in CLOSE_PARENS:
            _flush(buf, nodes)
            return nodes, i + 1
        elif ch in SEPARATORS:
            _flush(buf, nodes)
            i += 1
        else:
            buf.append(ch)
            i += 1
    _flush(buf, nodes)
    return nodes, i
