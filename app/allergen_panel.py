"""过敏原检测套餐规划（临时预览，不改写审查记录）。

从历史审查记录中**仅选择一条时效正常**的记录，读取其**当前配方**（两版
记录取标签核对对象，即新版配方；单版取现版）的**最终过敏原集合**，作为
套餐组合必须覆盖的目标过敏原。

用户逐行录入套餐，每行格式为::

    套餐名：价格：过敏原列表

- 冒号中英文均可，必须恰好给出三段；过敏原之间用逗号 / 顿号 / 分号 /
  空白分隔（与字典批量导入口径一致），允许为空（该套餐不覆盖任何过敏原，
  仅原样列回，不参与最优组合）；
- 价格经 ``Fraction(str(...))`` **精确小数**解析，必须是不小于 0 的有限
  数值（``0.1 + 0.2`` 与 ``0.3`` 视为同价，不受浮点误差影响）；
- 套餐名不能为空、同一批内套餐名不得重复；
- ``#`` 开头的行与空行忽略。

后端用**子集动态规划**（0/1 背包式覆盖 DP，状态数 = 2^|目标过敏原|）求
覆盖全部目标过敏原的**总价最低**套餐组合（每个套餐至多选一次；套餐覆盖
的非目标过敏原不影响可行性，仅作展示）：

- 不覆盖任何目标过敏原的套餐不参与组合，仅原样列回；转移时区分「不带来
  任何新覆盖」的冗余选择：正价冗余一律跳过（价格非负，不可能更优，永不
  进入最低价）；0 价冗余只要套餐本身覆盖至少一个必须项，即使加入后不新增
  覆盖，也作为不同的同价组合计入并列（其名称序列字典序可能更小而被展示）；
- 总价并列时，按所选套餐名升序序列的**字典序**取唯一最小方案（被展示的
  方案中允许出现覆盖贡献为空的套餐，累计覆盖保持不变）；
- 同时统计达到最低总价的不同套餐组合数，供页面并列提示；
- 全部套餐的过敏原并集仍缺少某些目标过敏原时无解，逐条列出**覆盖缺口**；
- 结果按套餐名序列展示每个套餐的**覆盖贡献**（本套餐新覆盖的目标过敏原）
  与**累计覆盖**（选到该套餐时已覆盖的目标过敏原并集）。

记录缺失（不存在 / 已删除）或处于**需复核**状态（旧记录无指纹、相关原料
映射已变更）时拒绝规划并说明原因。整个模块只读：不写入任何套餐，也不
改写任何审查记录。
"""
from __future__ import annotations

import math
import re
from fractions import Fraction
from typing import Dict, List, Optional, Tuple

from . import db
from .changeover import blocked_message, product_of

# 单个套餐价格的精确小数形态（整数或有限小数，不含指数 / 千分位）
_PRICE_RE = re.compile(r"\d+(?:\.\d+)?$")
_SPLIT_ALLERGENS_RE = re.compile(r"[,，、;；\s]+")
_SPLIT_COLON_RE = re.compile(r"[:：]")


class PanelError(ValueError):
    """套餐规划请求无法完成（映射为 HTTP 400）。

    problems 逐条列出原因：记录资格问题（缺失 / 需复核 / ID 无效）或套餐
    行解析问题（行号、类型、原文），供页面定位。
    """

    def __init__(self, message: str, problems: Optional[List[dict]] = None):
        super().__init__(message)
        self.problems = problems or []


def list_panel_records() -> List[dict]:
    """候选记录清单：每条历史记录的当前配方最终过敏原集合与选择资格。"""
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


def _record_problem(review_id, reason: str, message: str,
                    name: Optional[str] = None) -> dict:
    return {"id": review_id, "name": name, "reason": reason, "message": message}


def _load_eligible_review(review_id) -> dict:
    """取出记录并校验选择资格，返回记录提取出的产品信息（含过敏原）。"""
    try:
        rid = int(review_id)
    except (TypeError, ValueError):
        raise PanelError(
            "记录 ID 无效，禁止规划",
            problems=[_record_problem(review_id, "invalid",
                                      "记录 ID 无效，禁止选择")])
    review = db.get_review(rid)
    if review is None:
        raise PanelError(
            "审查记录不存在或已删除，禁止规划",
            problems=[_record_problem(rid, "missing",
                                      "审查记录不存在或已删除，禁止选择")])
    if review.get("needs_recheck"):
        reason = review.get("recheck_reason")
        raise PanelError(
            "记录处于需复核状态，禁止规划",
            problems=[_record_problem(
                review["id"], reason or "unknown",
                f"记录{blocked_message(reason)}，禁止选择",
                name=review["name"])])
    return product_of(review)


def _parse_price(raw: str, line_no: int) -> Fraction:
    """价格校验：不小于 0 的有限精确小数（经 Fraction(str) 精确化）。"""
    text = raw.strip()
    if not _PRICE_RE.match(text):
        raise PanelError(
            f"第 {line_no} 行价格不是有效数字：{raw.strip()!r}",
            problems=[{"line": line_no, "type": "bad_price",
                       "message": f"价格必须是不小于 0 的数字（收到 {raw.strip()!r}）"}])
    value = Fraction(text)
    if not math.isfinite(float(value)) or value < 0:
        raise PanelError(
            f"第 {line_no} 行价格必须是不小于 0 的有限数值",
            problems=[{"line": line_no, "type": "bad_price",
                       "message": "价格必须是不小于 0 的有限数值"}])
    return value


def parse_packages(text: str) -> List[dict]:
    """解析「套餐名：价格：过敏原列表」逐行文本。

    任一行不合法即抛出 :class:`PanelError`，problems 给出**全部**问题行
    （与别名批量导入的整批拒绝口径一致：本次预览不产生任何方案）。
    返回 ``[{name, price(Fraction), allergens(排序去重列表)}]``，保持录入
    顺序；不覆盖任何目标过敏原的套餐也保留（页面提示其未参与组合）。
    """
    packages: List[dict] = []
    problems: List[dict] = []
    seen: Dict[str, int] = {}
    for idx, raw_line in enumerate((text or "").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = _SPLIT_COLON_RE.split(line)
        if len(parts) != 3:
            problems.append({
                "line": idx, "type": "format",
                "message": "格式应为「套餐名：价格：过敏原列表」（需恰好 2 个冒号分隔 3 段）",
            })
            continue
        name = parts[0].strip()
        if not name:
            problems.append({"line": idx, "type": "empty_name",
                             "message": "套餐名不能为空"})
            continue
        allergens = sorted({
            a.strip() for a in _SPLIT_ALLERGENS_RE.split(parts[2]) if a.strip()})
        price = None
        try:
            price = _parse_price(parts[1], idx)
        except PanelError as exc:
            problems.extend(exc.problems)
        if name in seen:
            problems.append({
                "line": idx, "type": "duplicate_name",
                "message": f"套餐名「{name}」与第 {seen[name]} 行重复，套餐名必须唯一",
            })
            continue
        seen[name] = idx
        if price is not None:
            packages.append({"name": name, "price": price,
                             "allergens": allergens})
    if problems:
        raise PanelError(
            f"套餐录入有 {len(problems)} 行未通过校验，请修正后重试",
            problems=problems)
    return packages


def _json_number(value: Fraction):
    """Fraction → JSON 可序列化数值（整数保留 int，其余转 float）。"""
    return int(value) if value.denominator == 1 else float(value)


def solve_panel(target: List[str], packages: List[dict]) -> dict:
    """子集动态规划求覆盖目标过敏原的最低总价套餐组合。

    target 为必须覆盖的过敏原（去重排序）；packages 为已解析套餐。价格经
    Fraction 精确比较：正向 0/1 背包统计各覆盖状态的最低总价与并列组合数
    （正价冗余跳过、0 元冗余允许自转移计入并列），再以**后缀 DP + 按套餐名
    升序贪心**重建字典序最小的唯一方案（不在状态里存代表键：局部最小键追加
    未来套餐后未必仍最小）。无法覆盖全部目标时返回 feasible=False 与覆盖缺口。
    """
    allergens = sorted(set(target))
    index = {a: i for i, a in enumerate(allergens)}
    m = len(allergens)

    # 只保留能覆盖至少一个目标过敏原的套餐参与 DP；其余原样列出
    useful, unused = [], []
    for pkg in packages:
        mask = 0
        for a in pkg["allergens"]:
            if a in index:
                mask |= 1 << index[a]
        if mask:
            useful.append({**pkg, "mask": mask})
        else:
            unused.append(pkg)
    # 固定处理顺序：按套餐名排序，保证并列键与方案唯一、可复现
    useful.sort(key=lambda p: p["name"])

    full = (1 << m) - 1
    # 0/1 背包：dp[mask] = 恰好覆盖 mask 的 (总价 Fraction, 并列组合数)，
    # 不可达为 None。每个状态只存成本与并列数；字典序最小方案不用局部代表
    # 键（局部最小键追加未来套餐后未必仍最小），稍后用后缀 DP 贪心重建。
    dp: List[Optional[Tuple[Fraction, int]]] = [None] * (1 << m)
    dp[0] = (Fraction(0), 1)
    for pkg in useful:
        snapshot = list(dp)  # 每个套餐至多选一次：只由「处理它之前」的状态转移
        pmask, price = pkg["mask"], pkg["price"]
        for mask, state in enumerate(snapshot):
            if state is None:
                continue
            redundant = (mask & pmask) == pmask  # 加入该套餐不带来任何新覆盖
            if redundant and price > 0:
                continue  # 正价冗余不可能更优，永不进入最低价
            nmask = mask | pmask
            cand_cost = state[0] + price
            cur = dp[nmask]
            if cur is None or cand_cost < cur[0]:
                dp[nmask] = (cand_cost, state[1])
            elif cand_cost == cur[0]:
                # 不同前驱子集与「不选本套餐」的方案互不相交，并列数直接相加
                dp[nmask] = (cur[0], cur[1] + state[1])

    by_name = {p["name"]: p for p in useful}
    for p in unused:
        by_name[p["name"]] = p

    def describe(pkg: dict, covered_before: int) -> dict:
        contribution = sorted(
            a for i, a in enumerate(allergens)
            if (pkg["mask"] if "mask" in pkg else 0) >> i & 1
            and not covered_before >> i & 1)
        extra = sorted(set(pkg["allergens"]) - set(allergens))
        return {
            "name": pkg["name"],
            "price": _json_number(pkg["price"]),
            "allergens": list(pkg["allergens"]),
            "contribution": contribution,
            "contribution_count": len(contribution),
            "extra_allergens": extra,
        }

    if m == 0:
        # 当前配方不含任何过敏原：空组合即以 0 元完成覆盖
        best: Optional[Tuple[Fraction, int]] = (Fraction(0), 1)
    else:
        best = dp[full]

    if best is None:
        reachable = 0
        for mask, state in enumerate(dp):
            if state is not None:
                reachable |= mask
        gap = [a for i, a in enumerate(allergens) if not reachable >> i & 1]
        return {
            "feasible": False,
            "required_allergens": allergens,
            "total_price": None,
            "optimal_count": 0,
            "selections": [],
            "uncovered": gap,
            "packages": [_package_overview(by_name[n], allergens)
                         for n in sorted(by_name)],
        }

    total, ways = best
    # 后缀 DP：suf[k][mask] = 仅用 useful[k:] 把已覆盖 mask 补到 full 的最低
    # 追加成本（不可达为 None）。随后按套餐名升序贪心重建字典序最小方案：
    # 已能以预算补全即停止（空续接是前缀，字典序最小）；否则取「加入后仍能
    # 以最低总价补全」的最小名套餐——0 元冗余因不耗预算会被纳入（其覆盖贡献
    # 为空、累计覆盖不变），正价冗余无法满足预算恒等式而被自动排除。
    n = len(useful)
    suf: List[List[Optional[Fraction]]] = [
        [None] * (1 << m) for _ in range(n + 1)]
    suf[n][full] = Fraction(0)
    for k in range(n - 1, -1, -1):
        pmask = useful[k]["mask"]
        price = useful[k]["price"]
        for mask in range(1 << m):
            skip = suf[k + 1][mask]
            tail = suf[k + 1][mask | pmask]
            take = tail + price if tail is not None else None
            if skip is None:
                chosen = take
            elif take is None:
                chosen = skip
            else:
                chosen = skip if skip < take else take
            suf[k][mask] = chosen

    key: List[str] = []
    covered = 0
    spent = Fraction(0)
    k = 0
    while not (covered == full and spent == total):
        for j in range(k, n):
            pkg = useful[j]
            tail = suf[j + 1][covered | pkg["mask"]]
            if tail is not None and spent + pkg["price"] + tail == total:
                key.append(pkg["name"])
                covered |= pkg["mask"]
                spent += pkg["price"]
                k = j + 1
                break
        else:  # pragma: no cover - 可行性已由正向 DP 保证，理论上不可达
            raise RuntimeError("最优方案重建失败")

    selections = []
    covered = 0
    for name in key:
        pkg = by_name[name]
        item = describe(pkg, covered)
        covered |= pkg.get("mask", 0)
        item["cumulative_covered"] = [
            a for i, a in enumerate(allergens) if covered >> i & 1]
        selections.append(item)
    return {
        "feasible": True,
        "required_allergens": allergens,
        "total_price": _json_number(total),
        "optimal_count": ways,
        "selections": selections,
        "uncovered": [],
        "unused_packages": [
            {"name": p["name"], "price": _json_number(p["price"]),
             "allergens": list(p["allergens"])}
            for p in sorted(unused, key=lambda q: q["name"])],
        "packages": [_package_overview(by_name[n], allergens)
                     for n in sorted(by_name)],
    }


def _package_overview(pkg: dict, required: List[str]) -> dict:
    """录入套餐的原样回显：覆盖哪些必查项、哪些是额外过敏原。"""
    required_set = set(required)
    return {
        "name": pkg["name"],
        "price": _json_number(pkg["price"]),
        "allergens": list(pkg["allergens"]),
        "covers_required": sorted(a for a in pkg["allergens"]
                                  if a in required_set),
        "extra_allergens": sorted(a for a in pkg["allergens"]
                                  if a not in required_set),
    }


def build_panel_preview(review_id, text: str) -> dict:
    """套餐规划预览的完整编排：校验记录资格后解析套餐并求最优组合（只读）。

    记录缺失或处于需复核状态时抛出 :class:`PanelError`，problems 给出
    禁止选择的原因；套餐行解析问题同样整批拒绝并逐行指出。
    """
    review = _load_eligible_review(review_id)
    packages = parse_packages(text)
    if not packages:
        raise PanelError(
            "至少录入一个套餐（每行「套餐名：价格：过敏原列表」）",
            problems=[{"line": None, "type": "empty",
                       "message": "套餐内容为空（# 注释 / 空行不计入），请至少录入一行"}])
    result = solve_panel(review["allergens"], packages)
    result["review"] = {
        "id": review["id"],
        "name": review["name"],
        "source_version": review.get("source_version"),
    }
    return result
