"""配料占比区间顺序校验（临时预览，不写入审查历史）。

在现有嵌套配方文本的每个原料名后追加占比区间标注：

- 区间：``奶粉[8-12%]``；单值：``食盐[2%]``（等价于 [2-2%]）；
- 父配料先标注再跟子配料括号：``巧克力酱[25-60%]（可可脂[10-18%]）``；
- 复用 parser.parse_recipe 的嵌套解析，``配料表：`` 等前缀同样自动去除。

对每组同级配料（顶层为一组，每个父配料的子配料各为一组），按标签书写
顺序建立占比非递增约束 x1 >= x2 >= ... >= xn，用区间传播判断整组是否
存在可行赋值，**不做组合枚举**：

- 前向收紧上界：H_i = min(hi_1..hi_i) —— x_i 不超过任一前序项的上界；
- 后向收紧下界：L_i = max(lo_i..lo_n) —— x_i 不低于任一后序项的下界。

整组可行当且仅当所有 i 满足 L_i <= H_i。可行时 [L_i, H_i] 即第 i 项
占比的精确可行区间（链式非递增约束下两次传播即完备：区间内任意取值都
能扩展为整组可行赋值）；L_i == H_i 的项被锁定为单一边界值。

不可行时，冲突必然由一对下标 (j, k)（j < k 且 lo_k > hi_j）见证：取跨
度最小（并列取最左）的一对，其间的连续片段即为该组最短冲突片段，并附
该同级组的完整嵌套路径。

缺少区间、格式错误、负值、下界大于上界等标注问题在校验前统一收集，
每条都定位到具体嵌套路径。
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from .parser import IngredientNode, parse_recipe

# 原料名末尾的 [ ... ] 占比标注（标注必须位于名称最末）
_ANNOTATION_RE = re.compile(r"^([^\[\]]*)\[([^\[\]]*)\]\s*$")
# 标注内容：单个数字或 下界-上界（允许负号进入匹配，随后单独报负值错误）
_INTERVAL_RE = re.compile(
    r"^\s*(-?\d+(?:\.\d+)?)\s*(?:-\s*(-?\d+(?:\.\d+)?)\s*)?$")

_REASONS = {
    "empty": "未解析到任何配料，请检查配方文本",
    "missing": "缺少占比区间（应为「名称[最小-最大%]」或「名称[值%]」）",
    "bad_format": "占比区间格式错误（应为「[最小-最大%]」或「[值%]」）",
    "negative": "占比不能为负值",
    "inverted": "占比下界大于上界",
}

# 同级组：组路径（父链展示名，顶层为 []）+ [(展示名, lo, hi)]
_Group = Tuple[List[str], List[Tuple[str, Optional[float], Optional[float]]]]

# 先序拍平项：(完整路径, 展示名, lo, hi)
_FlatItem = Tuple[List[str], str, Optional[float], Optional[float]]


def _fmt(value: float) -> str:
    """百分比数值展示：整数去掉小数点，其余保留原样。"""
    return str(int(value)) if float(value).is_integer() else str(value)


def _error(path: List[str], ingredient: str, reason: str,
           detail: str = "") -> dict:
    message = _REASONS[reason] + (f"：{detail}" if detail else "")
    return {"path": list(path), "ingredient": ingredient,
            "reason": reason, "message": message}


def _split_annotation(raw: str) -> Tuple[str, Optional[str], Optional[str]]:
    """从原料原文本拆出末尾占比标注，返回 (展示名, 标注内容, 错误种类)。

    展示名用于嵌套路径：标注缺失时取原文本，标注存在时取「[」前的名称部分。
    """
    text = raw.strip()
    if "[" not in text:
        # 孤立的 ] 视为格式错误；连 [ 都没有则是缺少区间
        return (text, None, "bad_format") if "]" in text \
            else (text, None, "missing")
    m = _ANNOTATION_RE.match(text)
    if not m or not m.group(1).strip():
        display = text[:text.find("[")].strip() or text
        return display, None, "bad_format"
    return m.group(1).strip(), m.group(2), None


def _parse_interval(content: str):
    """把标注内容解析为 (lo, hi)；失败返回 (None, 错误种类)。"""
    body = content.strip()
    if not body.endswith("%"):
        return None, "bad_format"
    m = _INTERVAL_RE.match(body[:-1])
    if not m:
        return None, "bad_format"
    lo = float(m.group(1))
    hi = float(m.group(2)) if m.group(2) is not None else lo
    if lo < 0 or hi < 0:
        return None, "negative"
    if lo > hi:
        return None, "inverted"
    return (lo, hi), None


def _collect(nodes: List[IngredientNode], ancestors: List[str],
             flat: List[_FlatItem], errors: List[dict]) -> None:
    """先序遍历配料树：拍平全部节点并按文档顺序收集标注错误。

    有标注错误的节点以 None 区间占位（保证路径完整），只要存在错误，
    外层即丢弃分组、不做约束校验。
    """
    for node in nodes:
        display, content, err = _split_annotation(node.name)
        path = ancestors + [display]
        lo = hi = None
        if err:
            errors.append(_error(path, display, err))
        else:
            interval, err = _parse_interval(content)
            if err:
                errors.append(_error(path, display, err,
                                     detail=f"[{content}]"))
            else:
                lo, hi = interval
        flat.append((path, display, lo, hi))
        if node.children:
            _collect(node.children, path, flat, errors)


def _group_by_parent(flat: List[_FlatItem]) -> List[_Group]:
    """把先序拍平项按父路径分回同级组（组顺序保持先序，父组在子组前）。"""
    groups: List[_Group] = []
    index: dict = {}
    for path, display, lo, hi in flat:
        key = tuple(path[:-1])
        if key not in index:
            index[key] = (list(path[:-1]), [])
            groups.append(index[key])
        index[key][1].append((display, lo, hi))
    return groups


def _check_group(items: List[Tuple[str, float, float]]):
    """对一组同级配料做区间传播校验（不做组合枚举）。

    可行时返回 (收紧后的 [(L_i, H_i)], None)；不可行时返回
    (None, (j, k)) —— 最短冲突片段的首尾下标（含端点）。
    """
    n = len(items)
    upper = [0.0] * n  # 前向：H_i = min(hi_1..hi_i)
    run = float("inf")
    for i, (_, _, hi) in enumerate(items):
        run = min(run, hi)
        upper[i] = run
    lower = [0.0] * n  # 后向：L_i = max(lo_i..lo_n)
    run = float("-inf")
    for i in range(n - 1, -1, -1):
        run = max(run, items[i][1])
        lower[i] = run
    if all(lower[i] <= upper[i] for i in range(n)):
        return list(zip(lower, upper)), None
    # 不可行 ⟺ 存在 j < k 使 lo_k > hi_j（j == k 已被下界<=上界校验排除）；
    # 取跨度最小（并列取最左）的一对，片段即 items[j..k]
    best = None
    for k in range(n):
        for j in range(k):
            if items[k][1] > items[j][2] and (
                    best is None or k - j < best[1] - best[0]):
                best = (j, k)
    return None, best


def analyze_proportions(text: str) -> dict:
    """占比区间顺序校验的完整计算（纯文本分析，不读写任何持久数据）。

    返回结构：
    - status="error"      —— 标注问题，errors 逐条定位到嵌套路径；
    - status="infeasible" —— conflicts 给出每组最短冲突片段与完整路径；
    - status="feasible"   —— groups 给出每项收紧后的可行区间与单值锁定标记。
    """
    parsed = parse_recipe(text)
    flat: List[_FlatItem] = []
    errors: List[dict] = []
    _collect(parsed, [], flat, errors)
    groups = _group_by_parent(flat)
    item_count = len(flat)
    result = {
        "status": "feasible",
        "item_count": item_count,
        "group_count": len(groups),
        "errors": [],
        "conflicts": [],
        "groups": [],
    }
    if not parsed:
        result["status"] = "error"
        result["errors"] = [_error([], "", "empty")]
        return result
    if errors:
        result["status"] = "error"
        result["errors"] = errors
        return result

    conflicts, feasible_groups = [], []
    for path, items in groups:
        tightened, conflict = _check_group(items)
        if conflict is None:
            feasible_groups.append({
                "path": path,
                "items": [{
                    "name": name,
                    "original": [lo, hi],
                    "tightened": [t_lo, t_hi],
                    # 收紧后区间退化为单点：只能取该单一边界值
                    "pinned": t_lo == t_hi,
                } for (name, lo, hi), (t_lo, t_hi) in zip(items, tightened)],
            })
        else:
            j, k = conflict
            conflicts.append({
                "path": path,
                "fragment": [{"name": n, "lo": lo, "hi": hi}
                             for n, lo, hi in items[j:k + 1]],
                "message": (
                    f"「{items[k][0]}」的占比下界 {_fmt(items[k][1])}% 高于"
                    f"前序「{items[j][0]}」的占比上界 {_fmt(items[j][2])}%，"
                    f"同级占比须按标签顺序非递增，该组不存在可行赋值"),
            })
    if conflicts:
        result["status"] = "infeasible"
        result["conflicts"] = conflicts
    else:
        result["groups"] = feasible_groups
    return result
