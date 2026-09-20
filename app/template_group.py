"""共用标签模板分组（临时预览，不改写审查记录）。

把每条**历史审查记录**视为一个产品，复用其当前配方（两版记录取标签核对
对象，即新版配方；单版取现版）的**最终过敏原集合**：

- 用户勾选 2～12 个时效正常的产品，并为每个产品设置**最多允许的附加
  声明数量**（未设置的按 0 个计）；
- 一组产品共用一张标签模板：模板内容 = 组内产品过敏原**并集**；每个产品
  的**附加声明**为「模板含有而自身没有」的过敏原，仅当组内每个产品的
  附加声明数都不超过其上限时，该组才允许成立（单个产品总是成组）；
- 用**子集动态规划**（2^n 个子集状态）求**模板数量最少**的完整分组：
  每个产品恰好归入一组；
- 模板数并列时，组内成员按产品 ID 升序、各组按组内 ID 序列的字典序
  排列，取整体序列最小者，保证结果唯一确定；同时统计达到最少模板数的
  不同分组方案总数，供页面提示。

记录缺失（不存在 / 已删除）或处于**需复核**状态（旧记录无指纹、相关原料
映射已变更）时禁止参与分组，并逐条指出原因。整个模块只读：不写入字典，
也不改写任何审查记录。
"""
from __future__ import annotations

import math
import re
from typing import Dict, List, Optional

from . import db
from .changeover import blocked_message, product_of

MIN_PRODUCTS = 2
MAX_PRODUCTS = 12

_INT_RE = re.compile(r"\d+")


class TemplateGroupError(ValueError):
    """模板分组请求无法完成（映射为 HTTP 400）。

    problems 逐条列出禁止参与的记录及原因，供页面定位。
    """

    def __init__(self, message: str, problems: Optional[List[dict]] = None):
        super().__init__(message)
        self.problems = problems or []


def list_template_products() -> List[dict]:
    """候选产品清单：每条历史记录的当前配方最终过敏原集合与参与资格。"""
    products = []
    for summary in db.list_reviews():
        review = db.get_review(summary["id"])
        needs = bool(review.get("needs_recheck"))
        reason = review.get("recheck_reason")
        products.append({
            **product_of(review),
            "created_at": review["created_at"],
            "has_two_versions": bool(review.get("text_b")),
            "eligible": not needs,
            "blocked_reason": blocked_message(reason) if needs else None,
        })
    products.sort(key=lambda p: p["id"])
    return products


def _bits(mask: int, n: int):
    for i in range(n):
        if mask >> i & 1:
            yield i


def build_grouping(products: List[dict],
                   max_extra: Optional[Dict[int, int]] = None) -> dict:
    """对给定产品集合求模板数量最少的完整分组（子集动态规划）。

    products 为 ``[{id, name, allergens, ...}]``；max_extra 为
    ``{产品ID: 最多允许的附加声明数}``，未配置的产品按 0 个计。
    一组的模板 = 组内过敏原并集；产品的附加声明 = 模板 − 自身过敏原，
    附加声明数不超过其上限时该组可行。模板数并列时按组内 / 组间产品
    ID 序列取最小者，结果唯一确定。
    """
    products = sorted(products, key=lambda p: p["id"])
    n = len(products)
    ids = [p["id"] for p in products]
    names = [p["name"] for p in products]
    sets = [set(p.get("allergens", [])) for p in products]
    limits = [int((max_extra or {}).get(pid, 0)) for pid in ids]
    union_all = sorted(set().union(*sets)) if sets else []

    size = 1 << n
    # 逐子集预处理：过敏原并集、组内产品 ID 序列（升序）与成组可行性
    union_of: List[frozenset] = [frozenset()] * size
    key_of: List[tuple] = [()] * size
    feasible = [False] * size
    feasible[0] = True
    for mask in range(1, size):
        lb = mask & (-mask)
        i = lb.bit_length() - 1
        prev = mask ^ lb
        union = union_of[prev] | sets[i]
        union_of[mask] = union
        key_of[mask] = (ids[i],) + key_of[prev]
        feasible[mask] = all(
            len(union - sets[j]) <= limits[j] for j in _bits(mask, n))

    # 子集 DP：dp[mask] = 恰好覆盖 mask 的最优 (模板数, 组间 ID 序列)。
    # 转移只枚举包含 mask 最低位产品的可行子集，每种分组方案只被统计
    # 一次；ways 记录达到最少模板数的不同分组方案数（用于并列提示）。
    INF = n + 1
    best_count = [INF] * size
    best_key: List[Optional[tuple]] = [None] * size
    best_groups: List[Optional[list]] = [None] * size
    ways = [0] * size
    best_count[0] = 0
    best_key[0] = ()
    best_groups[0] = []
    ways[0] = 1
    for mask in range(1, size):
        lb = mask & (-mask)
        sub = mask
        while sub:
            if sub & lb and feasible[sub]:
                rest = mask ^ sub
                base = best_count[rest]
                if base < INF:
                    cand_count = base + 1
                    cand_key = tuple(sorted(best_key[rest] + (key_of[sub],)))
                    if cand_count < best_count[mask]:
                        best_count[mask] = cand_count
                        best_key[mask] = cand_key
                        best_groups[mask] = best_groups[rest] + [sub]
                        ways[mask] = ways[rest]
                    elif cand_count == best_count[mask]:
                        ways[mask] += ways[rest]
                        if cand_key < best_key[mask]:
                            best_key[mask] = cand_key
                            best_groups[mask] = best_groups[rest] + [sub]
            sub = (sub - 1) & mask

    full = size - 1
    groups = []
    for sub in sorted(best_groups[full], key=lambda m: key_of[m]):
        members = []
        for j in _bits(sub, n):
            extra = sorted(union_of[sub] - sets[j])
            members.append({
                "id": ids[j],
                "name": names[j],
                "allergens": sorted(sets[j]),
                "extra": extra,
                "extra_count": len(extra),
                "max_extra": limits[j],
            })
        groups.append({
            "template": sorted(union_of[sub]),
            "members": members,
        })
    return {
        "products": [{
            "id": ids[i],
            "name": names[i],
            "source_version": products[i].get("source_version"),
            "allergens": sorted(sets[i]),
            "max_extra": limits[i],
        } for i in range(n)],
        "allergen_union": union_all,
        "template_count": best_count[full],
        "optimal_count": ways[full],
        "groups": groups,
    }


def _validate_limit(value, pid: int) -> int:
    """附加声明上限校验：必须是不小于 0 的整数。"""
    message = f"产品 #{pid} 的附加声明上限必须是不小于 0 的整数"
    if isinstance(value, bool):
        raise TemplateGroupError(message)
    if isinstance(value, int):
        limit = value
    elif isinstance(value, float) and math.isfinite(value) \
            and value.is_integer():
        limit = int(value)
    elif isinstance(value, str) and _INT_RE.fullmatch(value.strip()):
        limit = int(value.strip())
    else:
        raise TemplateGroupError(message)
    if limit < 0:
        raise TemplateGroupError(message)
    return limit


def _resolve_limits(max_extra, ids: List[int]) -> Dict[int, int]:
    """解析各产品附加声明上限：未配置的产品按 0 个计。"""
    raw = max_extra or {}
    limits = {}
    for pid in ids:
        if str(pid) in raw:
            value = raw[str(pid)]
        elif pid in raw:
            value = raw[pid]
        else:
            value = 0
        limits[pid] = _validate_limit(value, pid)
    return limits


def build_template_group_preview(review_ids: List[int],
                                 max_extra: Dict[str, int]) -> dict:
    """模板分组预览的完整编排：校验记录资格与上限后求最少模板分组（只读）。

    记录缺失或处于需复核状态时抛出 :class:`TemplateGroupError`，
    problems 逐条给出禁止参与的原因。
    """
    ids = list(review_ids or [])
    if not MIN_PRODUCTS <= len(ids) <= MAX_PRODUCTS:
        raise TemplateGroupError(
            f"请勾选 {MIN_PRODUCTS}～{MAX_PRODUCTS} 个产品（当前 {len(ids)} 个）")
    if len(set(ids)) != len(ids):
        raise TemplateGroupError("同一产品不能重复选择")

    products, problems = [], []
    for rid in ids:
        try:
            rid = int(rid)
        except (TypeError, ValueError):
            problems.append({"id": rid, "name": None, "reason": "invalid",
                             "message": "记录 ID 无效，禁止参与分组"})
            continue
        review = db.get_review(rid)
        if review is None:
            problems.append({"id": rid, "name": None, "reason": "missing",
                             "message": "审查记录不存在或已删除，禁止参与分组"})
            continue
        if review.get("needs_recheck"):
            reason = review.get("recheck_reason")
            problems.append({
                "id": review["id"], "name": review["name"],
                "reason": reason or "unknown",
                "message": f"记录{blocked_message(reason)}，禁止参与分组",
            })
            continue
        products.append(product_of(review))
    if problems:
        raise TemplateGroupError(
            f"所选产品中有 {len(problems)} 条记录禁止参与分组，请调整后重试",
            problems=problems)
    limits = _resolve_limits(max_extra, [p["id"] for p in products])
    return build_grouping(products, limits)
