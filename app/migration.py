"""审查数据迁移包：全量导出、只读预检与一次性令牌确认导入。

迁移包是一个 UTF-8 JSON 文件，包含：

- ``format_version``：格式版本（当前为 :data:`FORMAT_VERSION`）；
- ``dictionary``：过敏原字典（原料 → 过敏原列表）；
- ``aliases``：原料别名（别名 → 标准名，出度 ≤ 1 的有向图）；
- ``reviews``：全部审查记录（含原始 A/B 配方、标签声明、结果结构、
  创建时间与字典指纹，保留原 ID）；
- ``checksum``：对**除 checksum 外的全部内容**按规范化 JSON 计算的
  SHA-256（``{"algorithm": "SHA-256", "value": "<hex>"}``）。

同一份数据库内容重复导出得到的文件**逐字节稳定**：键排序、紧凑 / 缩进
序列化方式固定，且不写入导出时间等易变字段（字典 / 别名的 updated_at
在导入确认时统一重新戳记；审查记录的 created_at 属于业务数据，原样保留）。

导入分两步且预检绝不写库：

1. :func:`validate_package` 只读预检，逐项返回 problems（版本、摘要、
   字段类型、别名图无环、审查记录结构）与非阻断 warnings，通过后由
   :func:`issue_token` 签发**绑定该文件摘要**的一次性令牌；
2. 确认时重新预检、:func:`consume_token` 校验并消费令牌（重放 / 改文件
   均拒绝），再由 ``db.replace_all_data`` 在单个 SQLite 事务内整体替换
   现有数据，任一步失败都由该事务回滚。
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
from typing import Dict, List, Optional, Tuple

from .aliases import AliasResolver, _find_cycles
from .analysis import normalize_name

# 迁移包格式版本：包结构 / 字段口径变化时递增
FORMAT_VERSION = 1
SUPPORTED_VERSIONS = {1}
CHECKSUM_ALGORITHM = "SHA-256"

# 配料树来自用户录入的嵌套配方，正常深度极浅；限制深度仅用于防御畸形包
_MAX_TREE_DEPTH = 1000


# ---------- 序列化与摘要 ----------

def canonical_bytes(package: dict, *, include_checksum: bool = False) -> bytes:
    """规范化序列化迁移包内容。

    键排序、不转义非 ASCII、紧凑分隔符；摘要与文件内容都经此函数产出，
    因此同内容永远得到同字节序列。默认排除 ``checksum``（摘要不能覆盖
    自身）。
    """
    content = package if include_checksum else {
        k: v for k, v in package.items() if k != "checksum"}
    return json.dumps(
        content, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")


def compute_digest(package: dict) -> str:
    """对除 checksum 外的包内容计算 SHA-256 十六进制摘要。"""
    return hashlib.sha256(canonical_bytes(package)).hexdigest()


def build_package(*, dictionary: List[dict], aliases: List[dict],
                  reviews: List[dict]) -> dict:
    """由持久层导出的原始数据构建带摘要的迁移包（内存对象）。

    入参列表按各自的稳定键排序后再入包，调用方顺序不影响结果；
    字典 / 别名的 updated_at 不进包（易变字段，导入时重新戳记）。
    """
    pkg = {
        "format_version": FORMAT_VERSION,
        "dictionary": sorted((
            {"ingredient": e["ingredient"],
             "allergens": sorted(e["allergens"])}
            for e in dictionary), key=lambda e: e["ingredient"]),
        "aliases": sorted((
            {"alias": a["alias"], "target": a["target"]}
            for a in aliases), key=lambda a: a["alias"]),
        "reviews": sorted(({
            "id": r["id"],
            "name": r["name"],
            "declared": list(r["declared"]),
            "text_a": r["text_a"],
            "text_b": r["text_b"],
            "label_a": r["label_a"],
            "label_b": r["label_b"],
            "result": r["result"],
            "created_at": r["created_at"],
            "dict_fingerprint": r["dict_fingerprint"],
        } for r in reviews), key=lambda r: r["id"]),
    }
    pkg["checksum"] = {"algorithm": CHECKSUM_ALGORITHM,
                       "value": compute_digest(pkg)}
    return pkg


def serialize_package(package: dict) -> bytes:
    """迁移包落盘 / 下载字节：缩进、键排序、末尾换行，逐字节稳定。"""
    return (json.dumps(package, ensure_ascii=False, sort_keys=True,
                       indent=2) + "\n").encode("utf-8")


# ---------- 结构校验小工具 ----------

def _problem(problems: List[dict], scope: str, message: str,
             **extra) -> None:
    item = {"scope": scope, "message": message}
    item.update(extra)
    problems.append(item)


def _warn(warnings: List[dict], scope: str, message: str,
          **extra) -> None:
    item = {"scope": scope, "message": message}
    item.update(extra)
    warnings.append(item)


def _is_nonempty_str(v) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _is_str_list(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _is_int(v) -> bool:
    # bool 是 int 的子类，明确排除
    return isinstance(v, int) and not isinstance(v, bool)


# ---------- 只读预检 ----------

def _validate_tree(nodes, problems: List[dict], ctx: str,
                   depth: int = 0) -> None:
    if not isinstance(nodes, list):
        _problem(problems, ctx, "配料树 tree 必须是数组")
        return
    if depth > _MAX_TREE_DEPTH:
        _problem(problems, ctx, f"配料树嵌套深度超过 {_MAX_TREE_DEPTH}，疑似畸形数据")
        return
    for i, node in enumerate(nodes):
        here = f"{ctx}[{i}]"
        if not isinstance(node, dict):
            _problem(problems, here, "配料树节点必须是对象")
            continue
        if not _is_nonempty_str(node.get("name")):
            _problem(problems, f"{here}.name", "配料树节点 name 必须是非空字符串")
        for field in ("own_allergens", "total_allergens"):
            if not _is_str_list(node.get(field)):
                _problem(problems, f"{here}.{field}",
                         f"配料树节点 {field} 必须是字符串数组")
        if not isinstance(node.get("unknown"), bool):
            _problem(problems, f"{here}.unknown", "配料树节点 unknown 必须是布尔值")
        children = node.get("children")
        if isinstance(children, list):
            _validate_tree(children, problems, f"{here}.children", depth + 1)
        else:
            _problem(problems, f"{here}.children", "配料树节点 children 必须是数组")


def _validate_version(version, problems: List[dict], ctx: str) -> None:
    if not isinstance(version, dict):
        _problem(problems, ctx, "必须是对象")
        return
    if not _is_str_list(version.get("allergens")):
        _problem(problems, f"{ctx}.allergens", "allergens 必须是字符串数组")
    if not _is_str_list(version.get("unknown_ingredients")):
        _problem(problems, f"{ctx}.unknown_ingredients",
                 "unknown_ingredients 必须是字符串数组")
    _validate_tree(version.get("tree"), problems, f"{ctx}.tree")


def _validate_label_check(check, problems: List[dict], ctx: str) -> None:
    if not isinstance(check, dict):
        _problem(problems, ctx, "label_check 必须是对象")
        return
    for field in ("missing", "over_reported", "matched"):
        if not _is_str_list(check.get(field)):
            _problem(problems, f"{ctx}.{field}", f"{field} 必须是字符串数组")
    sources = check.get("missing_sources")
    if sources is not None:
        if not isinstance(sources, dict):
            _problem(problems, f"{ctx}.missing_sources",
                     "missing_sources 必须是对象（过敏原 → 来源路径数组）")
        else:
            for allergen, paths in sources.items():
                if not isinstance(allergen, str) or not isinstance(paths, list):
                    _problem(problems, f"{ctx}.missing_sources",
                             "missing_sources 的键必须是过敏原字符串、值必须是路径数组")
                    continue
                for j, path in enumerate(paths):
                    if not _is_str_list(path):
                        _problem(problems,
                                 f"{ctx}.missing_sources[{allergen}][{j}]",
                                 "每条来源路径必须是字符串数组")


def _validate_diff(diff, problems: List[dict], ctx: str) -> None:
    if diff is None:
        return
    if not isinstance(diff, dict):
        _problem(problems, ctx, "diff 必须是对象或 null")
        return
    for field in ("added", "removed", "unchanged"):
        if not _is_str_list(diff.get(field)):
            _problem(problems, f"{ctx}.{field}", f"{field} 必须是字符串数组")


def _validate_ingredient_changes(value, problems: List[dict],
                                 ctx: str) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        _problem(problems, ctx, "ingredient_changes 必须是数组或 null")
        return
    for i, item in enumerate(value):
        here = f"{ctx}[{i}]"
        if not isinstance(item, dict):
            _problem(problems, here, "配料变化项必须是对象")
            continue
        if not _is_nonempty_str(item.get("ingredient")):
            _problem(problems, f"{here}.ingredient", "ingredient 必须是非空字符串")
        if not _is_nonempty_str(item.get("change")):
            _problem(problems, f"{here}.change", "change 必须是非空字符串")


def _validate_result(result, problems: List[dict], ctx: str) -> None:
    """审查记录结果结构校验（仅校验已知键的类型，多余键向前兼容忽略）。"""
    if not isinstance(result, dict):
        _problem(problems, ctx, "result 必须是对象")
        return
    va = result.get("version_a")
    if not isinstance(va, dict):
        _problem(problems, f"{ctx}.version_a", "version_a 必须是对象")
    else:
        _validate_version(va, problems, f"{ctx}.version_a")
    vb = result.get("version_b")
    if vb is not None:
        _validate_version(vb, problems, f"{ctx}.version_b")
    _validate_diff(result.get("diff"), problems, f"{ctx}.diff")
    _validate_ingredient_changes(
        result.get("ingredient_changes"), problems,
        f"{ctx}.ingredient_changes")
    _validate_label_check(
        result.get("label_check"), problems, f"{ctx}.label_check")
    if not isinstance(result.get("label_check_version"), str):
        _problem(problems, f"{ctx}.label_check_version",
                 "label_check_version 必须是字符串")


def _validate_dictionary(dictionary, problems: List[dict],
                         warnings: List[dict]) -> set:
    """校验字典段，返回合法条目的归一化原料名集合。"""
    if not isinstance(dictionary, list):
        _problem(problems, "dictionary", "dictionary 必须是数组")
        return set()
    norm_first: Dict[str, str] = {}
    raw_seen: set = set()
    keys: set = set()
    for i, entry in enumerate(dictionary):
        ctx = f"dictionary[{i}]"
        if not isinstance(entry, dict):
            _problem(problems, ctx, "字典条目必须是对象")
            continue
        name = entry.get("ingredient")
        if not _is_nonempty_str(name):
            _problem(problems, f"{ctx}.ingredient",
                     "ingredient 必须是非空字符串")
            continue
        allergens = entry.get("allergens")
        if not _is_str_list(allergens):
            _problem(problems, f"{ctx}.allergens",
                     "allergens 必须是字符串数组")
            continue
        if any(not a.strip() for a in allergens):
            _warn(warnings, f"{ctx}.allergens",
                  f"原料「{name}」的过敏原列表含空白项，导入后原样保留，建议清理")
        if name in raw_seen:
            _problem(problems, ctx,
                     f"原料「{name}」在字典中重复出现（同名条目无法共存）")
            continue
        raw_seen.add(name)
        nkey = normalize_name(name)
        if nkey in norm_first and norm_first[nkey] != name:
            _warn(warnings, ctx,
                  f"原料「{norm_first[nkey]}」与「{name}」归一化后同名，"
                  "导入后字典查询只会保留其中一条映射，请合并后再导入")
        else:
            norm_first.setdefault(nkey, name)
        keys.add(nkey)
    return keys


def _validate_aliases(aliases, problems: List[dict],
                      warnings: List[dict], dict_keys: set) -> None:
    if not isinstance(aliases, list):
        _problem(problems, "aliases", "aliases 必须是数组")
        return
    # key -> [(数组下标, 目标key, 别名展示名, 目标展示名)]
    grouped: Dict[str, List[tuple]] = {}
    displays: Dict[str, str] = {}
    order: List[str] = []
    for i, entry in enumerate(aliases):
        ctx = f"aliases[{i}]"
        if not isinstance(entry, dict):
            _problem(problems, ctx, "别名条目必须是对象")
            continue
        alias, target = entry.get("alias"), entry.get("target")
        if not _is_nonempty_str(alias):
            _problem(problems, f"{ctx}.alias", "alias 必须是非空字符串")
            continue
        if not _is_nonempty_str(target):
            _problem(problems, f"{ctx}.target", "target 必须是非空字符串")
            continue
        akey, tkey = normalize_name(alias), normalize_name(target)
        displays.setdefault(akey, alias)
        displays.setdefault(tkey, target)
        if akey not in grouped:
            grouped[akey] = []
            order.append(akey)
        grouped[akey].append((i, tkey, alias, target))

    conflict: set = set()
    for akey in order:
        rows = grouped[akey]
        targets = {tkey for _, tkey, _, _ in rows}
        alias_display = rows[0][2]
        if len(targets) > 1:
            conflict.add(akey)
            idxs = sorted(i for i, *_ in rows)
            _problem(problems, f"aliases[{idxs[0]}]",
                     f"别名「{alias_display}」在第 {idxs} 项指向不同标准名，"
                     "同一别名只能指向一个标准名",
                     type="multi_target", indexes=idxs)
        elif len(rows) > 1:
            _warn(warnings, f"aliases[{rows[0][0]}]",
                  f"别名「{alias_display}」重复 {len(rows)} 次且目标相同，导入时折叠为一条")
        for i, tkey, alias, target in rows:
            if tkey == akey:
                _problem(problems, f"aliases[{i}]",
                         f"别名「{alias}」不能指向自身（自环）",
                         type="self_loop", index=i)

    # 合并出边：多目标冲突的别名取其首条目标参与建图（其本身已单独报错），
    # 确保经过冲突条目的环也不会漏检；再对整张图做无环校验
    edges: Dict[str, str] = {}
    edge_index: Dict[str, int] = {}
    for akey in order:
        i, tkey, *_ = grouped[akey][0]
        edges[akey] = tkey
        edge_index[akey] = i
    for cyc in _find_cycles(edges):
        if len(cyc) < 2:
            continue  # 长度 1 的自环已逐条报告
        start = min(cyc, key=lambda k: (edge_index.get(k, 1 << 30), k))
        rotated = cyc[cyc.index(start):] + cyc[:cyc.index(start)]
        names = [displays.get(k, k) for k in rotated]
        loop = names + [names[0]]
        idxs = sorted(edge_index.get(k, 1 << 30) for k in cyc if k in edge_index)
        _problem(problems, f"aliases[{idxs[0]}]",
                 f"别名构成环路：{' → '.join(loop)}，请删除或改写其中一条映射",
                 type="cycle", indexes=idxs, cycle=loop)

    # 最终标准名既不是字典条目也不经别名链落到字典条目：审查时会判定未登记
    resolver = AliasResolver(edges)
    warned: set = set()
    for akey in order:
        terminal = resolver.resolve(akey)
        if terminal not in dict_keys and terminal not in warned:
            warned.add(terminal)
            chain_names = [displays.get(k, k)
                           for k in resolver.chain(akey)]
            _warn(warnings, f"aliases[{grouped[akey][0][0]}]",
                  f"别名链 {' → '.join(chain_names)} 的最终标准名"
                  f"「{displays.get(terminal, terminal)}」未在字典中登记，"
                  "导入后相关原料会被判定为未识别")


def _validate_reviews(reviews, problems: List[dict],
                      warnings: List[dict]) -> None:
    if not isinstance(reviews, list):
        _problem(problems, "reviews", "reviews 必须是数组")
        return
    seen_ids: set = set()
    for i, r in enumerate(reviews):
        ctx = f"reviews[{i}]"
        if not isinstance(r, dict):
            _problem(problems, ctx, "审查记录必须是对象")
            continue
        rid = r.get("id")
        if not _is_int(rid) or rid < 1:
            _problem(problems, f"{ctx}.id", "id 必须是不小于 1 的整数")
        elif rid in seen_ids:
            _problem(problems, f"{ctx}.id", f"审查记录 ID {rid} 重复")
        else:
            seen_ids.add(rid)
        if not _is_nonempty_str(r.get("name")):
            _problem(problems, f"{ctx}.name", "name 必须是非空字符串")
        if not _is_str_list(r.get("declared")):
            _problem(problems, f"{ctx}.declared", "declared 必须是字符串数组")
        if not _is_nonempty_str(r.get("text_a")):
            _problem(problems, f"{ctx}.text_a", "text_a 必须是非空字符串")
        text_b = r.get("text_b")
        if text_b is not None and not isinstance(text_b, str):
            _problem(problems, f"{ctx}.text_b", "text_b 必须是字符串或 null")
        for field in ("label_a", "label_b", "created_at"):
            if not isinstance(r.get(field), str):
                _problem(problems, f"{ctx}.{field}", f"{field} 必须是字符串")
        fp = r.get("dict_fingerprint")
        if fp is None:
            _warn(warnings, f"{ctx}.dict_fingerprint",
                  f"审查记录「{r.get('name') if isinstance(r.get('name'), str) else f'#{rid}'}」"
                  "没有字典指纹，导入后会标记为需复核（旧记录）")
        elif not isinstance(fp, str):
            _problem(problems, f"{ctx}.dict_fingerprint",
                     "dict_fingerprint 必须是字符串或 null")
        _validate_result(r.get("result"), problems, f"{ctx}.result")


def validate_package(package) -> dict:
    """只读预检迁移包，**不写任何数据、不签发令牌**。

    返回::

        {"ok": bool, "digest": str|None, "counts": {...},
         "problems": [...], "warnings": [...]}

    problems 为阻断项（逐项收集，不因第一项出错而中止）；warnings 为
    非阻断提醒。只有 problems 为空时调用方才可签发一次性令牌。
    """
    problems: List[dict] = []
    warnings: List[dict] = []
    counts = {"dictionary": 0, "aliases": 0, "reviews": 0}

    if not isinstance(package, dict):
        _problem(problems, "$", "迁移包必须是 JSON 对象")
        return {"ok": False, "digest": None, "counts": counts,
                "problems": problems, "warnings": warnings}

    # 摘要先算：即便包被篡改导致其他字段校验失败，也能报告摘要不一致
    digest: Optional[str] = None
    try:
        digest = compute_digest(package)
    except (TypeError, ValueError):
        _problem(problems, "checksum", "包内容无法规范化序列化，不能计算摘要")

    version = package.get("format_version")
    if "format_version" not in package:
        _problem(problems, "format_version", "缺少 format_version 字段")
    elif not _is_int(version):
        _problem(problems, "format_version", "format_version 必须是整数")
    elif version not in SUPPORTED_VERSIONS:
        _problem(problems, "format_version",
                 f"不支持的格式版本 {version}，本系统支持版本："
                 f"{sorted(SUPPORTED_VERSIONS)}")

    checksum = package.get("checksum")
    if not isinstance(checksum, dict):
        _problem(problems, "checksum", "checksum 必须是对象")
    else:
        algo = checksum.get("algorithm")
        value = checksum.get("value")
        if algo != CHECKSUM_ALGORITHM:
            _problem(problems, "checksum.algorithm",
                     f"摘要算法必须为 {CHECKSUM_ALGORITHM}，实际为 {algo!r}")
        if not isinstance(value, str) or len(value) != 64:
            _problem(problems, "checksum.value",
                     "摘要值必须是 64 位十六进制字符串")
        elif digest is not None and value.lower() != digest.lower():
            _problem(problems, "checksum.value",
                     "摘要校验失败：文件内容与 checksum 不一致（文件可能已损坏或被篡改）",
                     type="checksum_mismatch", expected=digest, actual=value)

    dictionary = package.get("dictionary")
    aliases = package.get("aliases")
    reviews = package.get("reviews")
    if "dictionary" not in package:
        _problem(problems, "dictionary", "缺少 dictionary 字段")
    if "aliases" not in package:
        _problem(problems, "aliases", "缺少 aliases 字段")
    if "reviews" not in package:
        _problem(problems, "reviews", "缺少 reviews 字段")

    dict_keys = _validate_dictionary(dictionary, problems, warnings) \
        if "dictionary" in package else set()
    if "aliases" in package:
        _validate_aliases(aliases, problems, warnings, dict_keys)
    if "reviews" in package:
        _validate_reviews(reviews, problems, warnings)

    if isinstance(dictionary, list):
        counts["dictionary"] = len(dictionary)
    if isinstance(aliases, list):
        counts["aliases"] = len(aliases)
    if isinstance(reviews, list):
        counts["reviews"] = len(reviews)
    if counts == {"dictionary": 0, "aliases": 0, "reviews": 0}:
        _warn(warnings, "$",
              "迁移包不包含任何字典、别名与审查记录，确认导入将清空当前库的全部数据")

    return {"ok": not problems, "digest": digest, "counts": counts,
            "problems": problems, "warnings": warnings}


# ---------- 一次性导入令牌（绑定文件摘要，进程内有效） ----------

class TokenError(Exception):
    """令牌缺失 / 已消费 / 绑定的摘要不符。"""


class _TokenStore:
    """token -> digest 的进程内一次性票据存储。

    预检与确认在同一服务进程内完成；票据消费（pop）是原子的，
    因此同一令牌不可能被使用两次，文件被改动后摘要不符同样拒绝。
    """

    def __init__(self) -> None:
        self._tokens: Dict[str, str] = {}
        self._lock = threading.Lock()

    def issue(self, digest: str) -> str:
        token = secrets.token_hex(32)
        with self._lock:
            self._tokens[token] = digest
        return token

    def consume(self, token: str, digest: str) -> None:
        if not isinstance(token, str) or not token:
            raise TokenError("缺少确认令牌，请先通过只读预检")
        with self._lock:
            bound = self._tokens.pop(token, None)
        if bound is None:
            raise TokenError("确认令牌无效或已使用（令牌一次性有效），请重新预检")
        if bound != digest:
            raise TokenError(
                "确认令牌与当前文件摘要不绑定：文件在预检后发生过变化，请重新预检")

    def clear(self) -> None:
        with self._lock:
            self._tokens.clear()


tokens = _TokenStore()


def precheck(package, *, current_counts: Optional[dict] = None) -> dict:
    """只读预检并在通过时签发绑定摘要的一次性令牌。

    current_counts 为当前库数据量，用于追加「整体替换」风险提醒；
    本函数不访问持久层，预检阶段绝不写库。
    """
    outcome = validate_package(package)
    counts = outcome["counts"]
    if current_counts:
        outcome["warnings"].insert(0, {
            "scope": "$",
            "message": "确认导入将在单个事务内**整体替换**当前库的全部数据："
                       f"现有字典 {current_counts.get('dictionary', 0)} 条、"
                       f"别名 {current_counts.get('aliases', 0)} 条、"
                       f"审查记录 {current_counts.get('reviews', 0)} 条将被覆盖为 "
                       f"字典 {counts['dictionary']} 条、别名 {counts['aliases']} 条、"
                       f"审查记录 {counts['reviews']} 条，替换不可逐行撤销",
        })
    if outcome["ok"]:
        outcome["token"] = tokens.issue(outcome["digest"])
    return outcome


def normalized_rows(package: dict) -> Tuple[List[dict], List[dict], List[dict]]:
    """从已通过预检的包整理出可直接落库的行（折叠重复别名）。"""
    dictionary = [{"ingredient": e["ingredient"],
                   "allergens": list(e["allergens"])}
                  for e in package["dictionary"]]
    seen_alias: set = set()
    aliases: List[dict] = []
    for a in package["aliases"]:
        key = normalize_name(a["alias"])
        if key in seen_alias:
            continue
        seen_alias.add(key)
        aliases.append({"alias": a["alias"], "target": a["target"]})
    reviews = [dict(r) for r in package["reviews"]]
    return dictionary, aliases, reviews
