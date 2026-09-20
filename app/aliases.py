"""原料别名管理：「别名：标准名」有向图批量校验、链式解析与解析预览。

别名映射是一张**出度 ≤ 1 的有向图**（每个别名只能指向一个目标），合法时
全图无环，因此任一原料名都可以沿出边唯一解析到**最终标准名**：

- 批量导入整批原子校验：格式错误、同一别名多目标、自环、任意长度环都会
  导致整批拒绝（不写入任何一条）。每条错误返回冲突行；环错误额外返回
  从最早冲突行开始的环路（功能图中环上节点的唯一简单环，即最短环路）。
- 解析**仅接入正式审查与字典时效复核**：过敏原字典查询、未识别判定与
  指纹均针对最终标准名进行；配料树与遗漏来源路径始终显示原料原始名称。
- 别名规则变化是否要求复核历史记录，由指纹中记录的「最终标准名 +
  该标准名的过敏原映射」共同决定（见 :func:`fingerprint.scoped_fingerprint`）：
  最终标准名不变、其过敏原映射也不变时（例如只是缩短了等价链路），
  历史记录不标记需复核。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .analysis import normalize_name

# 已存在别名行：alias_key -> (别名展示名, 标准名展示名)
ExistingAliases = Dict[str, Tuple[str, str]]


# ---------- 解析 ----------

def parse_alias_lines(text: str) -> Tuple[List[Tuple[int, str, str]], List[dict]]:
    """解析别名文本。

    返回 (rows, errors)：rows 为 ``(行号, 别名原文, 标准名原文)``；
    无法成行（缺少冒号、别名或标准名为空）的行收集为格式错误，
    空行与 ``#`` 注释行忽略。冒号、别名/标准名两侧空白中英文写法均可。
    """
    rows: List[Tuple[int, str, str]] = []
    errors: List[dict] = []
    for lineno, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        for sep in ("：", ":"):
            if sep in line:
                alias, target = line.split(sep, 1)
                break
        else:
            alias = target = ""
        alias, target = alias.strip(), target.strip()
        if not alias or not target:
            errors.append({
                "type": "format", "line": lineno,
                "message": f"第 {lineno} 行格式应为「别名：标准名」：{line}",
            })
            continue
        rows.append((lineno, alias, target))
    return rows, errors


# ---------- 有向图校验 ----------

def _find_cycles(edges: Dict[str, str]) -> List[List[str]]:
    """在出度 ≤ 1 的功能图中找出全部互不相交的环。

    每个节点至多属于一个环；返回的每个环为节点 key 列表（不含重复的
    闭合节点）。用 0/1/2 三态遍历，已完结路径不重复行走。
    """
    state: Dict[str, int] = {}  # 1=当前路径中，2=已完结
    cycles: List[List[str]] = []
    for start in edges:
        if state.get(start, 0) == 2:
            continue
        order: List[str] = []
        index: Dict[str, int] = {}
        node: Optional[str] = start
        while node is not None and state.get(node, 0) != 2:
            if state.get(node) == 1:  # 再次踩到当前路径上的节点 -> 成环
                cycles.append(order[index[node]:])
                break
            state[node] = 1
            index[node] = len(order)
            order.append(node)
            node = edges.get(node)
        for n in order:
            state[n] = 2
    return cycles


def validate_alias_batch(text: str, existing: ExistingAliases) -> dict:
    """整批原子校验别名文本（与库中既有别名合并后判定）。

    返回 ``{"errors": [...], "entries": [(别名, 标准名), ...]}``：
    errors 非空时调用方必须拒绝整批、不写入任何条目；entries 为去重后
    待 upsert 的别名（归一化后同名同目标的重复行折叠，展示名取首行）。

    错误类型：
    - format       格式错误（缺冒号 / 别名或标准名为空），含行号；
    - multi_target 同一别名在本批指向多个不同标准名，含全部冲突行；
    - self_loop    自环（别名指向自身），含冲突行；
    - cycle        任意长度环，含本批冲突行与从最早冲突行展开的最短环路。
    """
    rows, errors = parse_alias_lines(text)

    # key -> {"rows": [(行号, 目标key, 别名展示名, 标准名展示名)], ...}
    grouped: Dict[str, dict] = {}
    order: List[str] = []
    for lineno, alias, target in rows:
        key, tkey = normalize_name(alias), normalize_name(target)
        if key not in grouped:
            grouped[key] = {"alias_display": alias, "rows": []}
            order.append(key)
        grouped[key]["rows"].append((lineno, tkey, alias, target))

    conflict_keys: Set[str] = set()
    self_loop_lines: Dict[str, List[int]] = {}
    entries: List[Tuple[str, str]] = []

    for key in order:
        g = grouped[key]
        target_displays: Dict[str, str] = {}
        for lineno, tkey, _, target_display in g["rows"]:
            target_displays.setdefault(tkey, target_display)
            if tkey == key:
                self_loop_lines.setdefault(key, []).append(lineno)

        # 同一别名多目标：归一化后目标不同才算冲突；同目标重复行幂等折叠
        if len(target_displays) > 1:
            conflict_keys.add(key)
            lines = [r[0] for r in g["rows"]]
            targets = "、".join(
                f"「{target_displays[t]}」（第 {ln} 行）"
                for ln, t in sorted((r[0], r[1]) for r in g["rows"]))
            errors.append({
                "type": "multi_target", "lines": lines,
                "alias": g["alias_display"],
                "message": f"别名「{g['alias_display']}」在第 "
                           f"{'、'.join(str(n) for n in lines)} 行指向不同标准名"
                           f"（{targets}），同一别名只能指向一个标准名",
            })

        first_lineno, first_tkey, first_alias, first_target = g["rows"][0]
        entries.append((first_alias, first_target))

    # 自环：别名指向自身
    for key in order:
        lines = self_loop_lines.get(key)
        if lines:
            display = grouped[key]["alias_display"]
            errors.append({
                "type": "self_loop", "lines": lines, "alias": display,
                "message": f"第 {'、'.join(str(n) for n in lines)} 行存在自环："
                           f"别名「{display}」不能指向自身",
            })

    # 与既有映射合并（本批条目覆盖同 key 旧映射；多目标冲突的本批条目搁置）
    candidate: Dict[str, str] = {
        k: normalize_name(target_display) for k, (_, target_display) in existing.items()}
    batch_line: Dict[str, int] = {}
    for key in order:
        if key in conflict_keys:
            continue
        tkey = grouped[key]["rows"][0][1]
        candidate[key] = tkey
        batch_line[key] = grouped[key]["rows"][0][0]

    # 任意长度环：库中既有无环，新环必然包含本批条目
    for cyc in _find_cycles(candidate):
        batch_members = [k for k in cyc if k in batch_line]
        if not batch_members or len(cyc) < 2:
            continue  # 长度 1 的自环已单独报告；既有环理论上不存在

        # 以最早冲突行（并列按 key）为环起点，给出确定且最短的环路表示
        start = min(batch_members, key=lambda k: (batch_line[k], k))
        i = cyc.index(start)
        rotated = cyc[i:] + cyc[:i]

        def display(k: str) -> str:
            return grouped[k]["alias_display"] if k in grouped else existing[k][0]

        names = [display(k) for k in rotated]
        loop = names + [names[0]]
        lines = sorted(batch_line[k] for k in batch_members)
        errors.append({
            "type": "cycle", "lines": lines, "cycle": loop,
            "message": f"别名在第 {'、'.join(str(n) for n in lines)} 行构成环路："
                       f"{' → '.join(loop)}，请删除或改写其中一条映射",
        })

    errors.sort(key=lambda e: (min(e.get("lines") or [e.get("line") or 0]),
                               e["type"]))
    return {"errors": errors,
            "entries": [] if errors else entries}


# ---------- 链式解析 ----------

@dataclass
class AliasResolver:
    """把原料名沿别名出边解析到最终标准名（合法数据无环）。"""

    edges: Dict[str, str] = field(default_factory=dict)
    _cache: Dict[str, str] = field(default_factory=dict)

    def resolve(self, key: str) -> str:
        """返回最终标准名 key；不是别名时原样返回。"""
        if key in self._cache:
            return self._cache[key]
        path: List[str] = []
        seen: Set[str] = set()
        cur = key
        while cur in self.edges and cur not in self._cache:
            if cur in seen:  # 防御：即便库中出现脏数据成环也不死循环
                terminal = cur
                break
            seen.add(cur)
            path.append(cur)
            cur = self.edges[cur]
        else:
            terminal = self._cache.get(cur, cur)
        for n in path:
            self._cache[n] = terminal
        self._cache[key] = terminal
        return terminal

    def chain(self, key: str) -> List[str]:
        """解析链：``[原始名, 中间别名, ..., 最终标准名]``（含首尾）。"""
        out = [key]
        cur = key
        seen = {key}
        while cur in self.edges:
            cur = self.edges[cur]
            if cur in seen:  # 防御性断环
                out.append(cur)
                break
            seen.add(cur)
            out.append(cur)
        return out


# ---------- 解析预览 ----------

def resolve_preview(names: List[str], edges: Dict[str, str],
                    dictionary: Dict[str, Set[str]],
                    displays: Optional[Dict[str, str]] = None,
                    leaf_keys: Optional[Set[str]] = None) -> List[dict]:
    """对一组原料名（可直接传配料树拍平后的名称）给出别名解析预览。

    按首次出现顺序对归一化名去重，每项返回原始名、解析链、最终标准名、
    是否经别名改写以及该标准名在字典中的过敏原（未登记时 in_dict=False）。
    displays 为归一化名 -> 展示名（别名原文 / 标准名原文），缺省时退回
    归一化名本身；leaf_keys 为配方中作为叶子（无子配料）出现的归一化
    原料名集合，复合父配料 is_leaf=False（其过敏原来自子配料，本就无需
    字典登记）。纯只读计算，不访问持久层。
    """
    resolver = AliasResolver(edges)
    displays = displays or {}
    leaf_keys = set() if leaf_keys is None else leaf_keys
    items: List[dict] = []
    seen: Set[str] = set()
    for name in names:
        key = normalize_name(name)
        if not key or key in seen:
            continue
        seen.add(key)
        chain_keys = resolver.chain(key)
        chain = [displays.get(k, k) for k in chain_keys]
        standard = chain_keys[-1]
        items.append({
            "name": name,
            "chain": chain,
            "standard": displays.get(standard, standard),
            "resolved": standard != key,
            "is_leaf": key in leaf_keys,
            "in_dict": standard in dictionary,
            "allergens": sorted(dictionary.get(standard, set())),
        })
    return items
