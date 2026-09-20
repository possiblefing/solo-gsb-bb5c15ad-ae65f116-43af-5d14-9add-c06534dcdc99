"""共线生产换线排程（临时预览，不改写审查记录）。

把每条**历史审查记录**视为共线生产线上的一个产品，复用其当前配方
（两版记录取标签核对对象，即新版配方；单版取现版）的**最终过敏原集合**：

- 用户勾选 2～10 个产品，并为所选产品涉及的全部过敏原配置清洁分钟数；
- 换线成本是**有向**的：前一产品 i → 后一产品 j 需要清洁的过敏原为
  「i 含有而 j 不含」的差集，成本为差集各过敏原清洁分钟数之和
  （未配置的过敏原按 0 分钟计；i → j 与 j → i 一般不同）；
- 用**子集动态规划**（Held-Karp，2^n 个子集状态）求总清洁时间最短的
  完整生产顺序：每个产品恰好生产一次，首产产品无换线成本；
- 总耗时并列时按**产品 ID 序列**的字典序取最小者，保证结果唯一确定；
  同时统计并列最优顺序的总数，供页面提示；
- 分钟数经 ``Fraction(str(...))`` 精确化，成本比较与并列判定不受
  浮点误差影响（如 0.1 + 0.2 与 0.3 应判为并列）。

记录缺失（不存在 / 已删除）或处于**需复核**状态（旧记录无指纹、相关原料
映射已变更）时禁止参与排程，并逐条指出原因。整个模块只读：不写入字典，
也不改写任何审查记录。
"""
from __future__ import annotations

import math
from fractions import Fraction
from typing import Dict, List, Optional

from . import db

MIN_PRODUCTS = 2
MAX_PRODUCTS = 10

# 需复核原因 → 展示文案（与历史记录列表的口径一致）
_RECHECK_REASONS = {
    "legacy": "旧记录无字典指纹",
    "stale": "相关原料的过敏原映射已变更",
}


class ChangeoverError(ValueError):
    """换线排程请求无法完成（映射为 HTTP 400）。

    problems 逐条列出禁止参与的记录及原因，供页面定位。
    """

    def __init__(self, message: str, problems: Optional[List[dict]] = None):
        super().__init__(message)
        self.problems = problems or []


def blocked_message(reason: Optional[str]) -> str:
    base = _RECHECK_REASONS.get(reason or "", "字典时效状态未知")
    return f"需复核（{base}），请先完成字典时效复核"


def _current_version(review: dict) -> dict:
    """记录当前配方（标签核对对象：有新版取新版，否则取现版）的版本结果。"""
    result = review.get("result") or {}
    return result.get("version_b") or result.get("version_a") or {}


def product_of(review: dict) -> dict:
    """从审查记录提取排程产品：ID、名称、取值版本与最终过敏原集合。"""
    result = review.get("result") or {}
    return {
        "id": review["id"],
        "name": review["name"],
        "source_version": result.get("label_check_version")
        or review.get("label_a") or "现版配方",
        "allergens": sorted(_current_version(review).get("allergens", [])),
    }


def list_changeover_products() -> List[dict]:
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


def _json_number(value: Fraction):
    """Fraction → JSON 可序列化数值（整数保留 int，其余转 float）。"""
    return int(value) if value.denominator == 1 else float(value)


def build_schedule(products: List[dict],
                   cleaning_minutes: Dict[str, float]) -> dict:
    """对给定产品集合求总清洁时间最短的生产顺序（子集动态规划）。

    products 为 ``[{id, name, allergens, ...}]``；cleaning_minutes 为
    ``{过敏原: 分钟}``，未配置的过敏原按 0 分钟计。分钟数经
    ``Fraction(str())`` 精确化，成本比较与并列判定不受浮点误差影响。
    """
    products = sorted(products, key=lambda p: p["id"])
    n = len(products)
    ids = [p["id"] for p in products]
    names = [p["name"] for p in products]
    sets = [set(p.get("allergens", [])) for p in products]
    union = sorted(set().union(*sets))
    minutes = {a: Fraction(str(cleaning_minutes.get(a, 0))) for a in union}

    # 有向换线成本：i → j 需清洁「i 含有而 j 不含」的过敏原
    cost = [[Fraction(0)] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i != j:
                cost[i][j] = sum((minutes[a] for a in sets[i] - sets[j]),
                                 Fraction(0))

    # Held-Karp：best[mask][j] = 恰好生产 mask 且最后生产 j 的最优
    # (成本, 产品ID序列)；成本并列时保留 ID 序列字典序最小者，
    # count 记录达到最小成本的不同顺序数（用于并列提示）。
    size = 1 << n
    best_cost: List[List[Optional[Fraction]]] = \
        [[None] * n for _ in range(size)]
    best_seq: List[List[Optional[tuple]]] = [[None] * n for _ in range(size)]
    count = [[0] * n for _ in range(size)]
    for j in range(n):
        best_cost[1 << j][j] = Fraction(0)
        best_seq[1 << j][j] = (ids[j],)
        count[1 << j][j] = 1
    for mask in range(size):
        for j in range(n):
            base = best_cost[mask][j]
            if base is None:
                continue
            seq = best_seq[mask][j]
            for k in range(n):
                if mask & (1 << k):
                    continue
                nmask = mask | (1 << k)
                cand = base + cost[j][k]
                cand_seq = seq + (ids[k],)
                cur = best_cost[nmask][k]
                if cur is None or cand < cur:
                    best_cost[nmask][k] = cand
                    best_seq[nmask][k] = cand_seq
                    count[nmask][k] = count[mask][j]
                elif cand == cur:
                    count[nmask][k] += count[mask][j]
                    if cand_seq < best_seq[nmask][k]:
                        best_seq[nmask][k] = cand_seq
    full = size - 1
    total = min(best_cost[full][j] for j in range(n))
    order_ids = min(best_seq[full][j] for j in range(n)
                    if best_cost[full][j] == total)
    optimal_count = sum(count[full][j] for j in range(n)
                        if best_cost[full][j] == total)

    index = {pid: i for i, pid in enumerate(ids)}
    order = [index[pid] for pid in order_ids]

    segments = []
    cumulative = Fraction(0)
    for k in range(1, n):
        i, j = order[k - 1], order[k]
        clean = sorted(sets[i] - sets[j])
        details = [{"allergen": a, "minutes": _json_number(minutes[a])}
                   for a in clean]
        seg = sum((minutes[a] for a in clean), Fraction(0))
        cumulative += seg
        segments.append({
            "from": {"id": ids[i], "name": names[i]},
            "to": {"id": ids[j], "name": names[j]},
            "allergens": clean,
            "details": details,
            "minutes": _json_number(seg),
            "cumulative_minutes": _json_number(cumulative),
        })
    return {
        "products": [{
            "id": ids[i],
            "name": names[i],
            "source_version": products[i].get("source_version"),
            "allergens": sorted(sets[i]),
        } for i in range(n)],
        "allergen_union": union,
        "cleaning_minutes": {a: _json_number(minutes[a]) for a in union},
        "order": list(order_ids),
        "total_minutes": _json_number(total),
        "optimal_count": optimal_count,
        "segments": segments,
    }


def _validate_minutes(cleaning_minutes) -> Dict[str, float]:
    """清洁分钟数校验：必须是不小于 0 的有限数值；空过敏原名忽略。"""
    minutes: Dict[str, float] = {}
    for allergen, value in (cleaning_minutes or {}).items():
        name = str(allergen).strip()
        if not name:
            continue
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise ChangeoverError(
                f"过敏原「{name}」的清洁分钟数不是有效数字：{value!r}")
        if not math.isfinite(v) or v < 0:
            raise ChangeoverError(
                f"过敏原「{name}」的清洁分钟数必须是不小于 0 的有限数值")
        minutes[name] = v
    return minutes


def build_changeover_preview(review_ids: List[int],
                             cleaning_minutes: Dict[str, float]) -> dict:
    """换线排程预览的完整编排：校验记录资格后求最优顺序（只读）。

    记录缺失或处于需复核状态时抛出 :class:`ChangeoverError`，
    problems 逐条给出禁止参与的原因。
    """
    ids = list(review_ids or [])
    if not MIN_PRODUCTS <= len(ids) <= MAX_PRODUCTS:
        raise ChangeoverError(
            f"请勾选 {MIN_PRODUCTS}～{MAX_PRODUCTS} 个产品（当前 {len(ids)} 个）")
    if len(set(ids)) != len(ids):
        raise ChangeoverError("同一产品不能重复选择")
    minutes = _validate_minutes(cleaning_minutes)

    products, problems = [], []
    for rid in ids:
        try:
            rid = int(rid)
        except (TypeError, ValueError):
            problems.append({"id": rid, "name": None, "reason": "invalid",
                             "message": "记录 ID 无效，禁止参与排程"})
            continue
        review = db.get_review(rid)
        if review is None:
            problems.append({"id": rid, "name": None, "reason": "missing",
                             "message": "审查记录不存在或已删除，禁止参与排程"})
            continue
        if review.get("needs_recheck"):
            reason = review.get("recheck_reason")
            problems.append({
                "id": review["id"], "name": review["name"],
                "reason": reason or "unknown",
                "message": f"记录{blocked_message(reason)}，禁止参与排程",
            })
            continue
        products.append(product_of(review))
    if problems:
        raise ChangeoverError(
            f"所选产品中有 {len(problems)} 条记录禁止参与排程，请调整后重试",
            problems=problems)
    return build_schedule(products, minutes)
