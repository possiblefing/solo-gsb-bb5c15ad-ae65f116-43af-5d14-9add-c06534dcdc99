"""审查数据迁移包：全量导出、只读预检与一次性令牌确认导入。

导出
----
一个 JSON 文件打包过敏原字典、原料别名与全部审查记录，附格式版本号与
按**规范化内容**计算的 SHA-256 摘要（canonical JSON：对象键排序、紧凑
分隔符、UTF-8 不转义非 ASCII；过敏原与标签声明等列表字段排序后参与）。
条目按主键排序输出，因此同一数据库重复导出内容稳定（逐字节一致），
导入后再导出的摘要同样不变。

导入（两段式）
------------
1. 预检（只读）：解析并**逐项**校验格式版本、SHA-256 摘要、字段类型、
   别名图无环（含自环与归一化重复）与审查记录结构，全部问题一次返回，
   **不写库**；全部通过时签发一枚**绑定该文件摘要的一次性确认令牌**
   （限时有效）。
2. 确认导入：凭令牌 + 原文件文本确认；令牌不存在、已使用、已过期或与
   文件摘要不匹配均拒绝。通过后在**单个 SQLite 事务**内整体替换字典 /
   别名 / 审查记录三张表，任一步失败整体回滚。
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
from typing import Dict, List, Tuple

from . import db
from .aliases import _find_cycles
from .analysis import normalize_name

EXPORT_FORMAT = "allergen-review-migration"
EXPORT_VERSION = 1
TOKEN_TTL_SECONDS = 600          # 确认令牌有效期（秒）
_MAX_TREE_DEPTH = 64             # 配料树结构校验的递归深度上限

_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")


# ---------- 规范化与摘要 ----------

def canonical_json(obj) -> str:
    """规范化 JSON：对象键排序、紧凑分隔符、UTF-8（不转义非 ASCII）。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def package_digest(package: dict) -> str:
    """按规范化内容计算迁移包签名载荷的 SHA-256。

    签名载荷为 ``format`` / ``version`` / ``data`` 三个字段；``counts``、
    ``sha256`` 等派生 / 自描述字段不参与摘要，因此文件内键顺序、空白
    差异不影响校验，而任何数据改动都会使摘要不匹配。
    """
    signed = {"format": package.get("format"),
              "version": package.get("version"),
              "data": package.get("data")}
    return hashlib.sha256(canonical_json(signed).encode("utf-8")).hexdigest()


def _counts_of(data: dict) -> dict:
    return {"dict": len(data["dict"]), "aliases": len(data["aliases"]),
            "reviews": len(data["reviews"])}


# ---------- 导出 ----------

def build_export() -> dict:
    """全量导出迁移包（只读）。

    字典按原料名、别名按归一化别名、审查记录按 ID 排序；过敏原与标签
    声明列表排序后输出 —— 同一数据库重复导出内容稳定。
    """
    snap = db.export_snapshot()
    data = {
        "dict": [{"ingredient": e["ingredient"],
                  "allergens": sorted(e["allergens"]),
                  "updated_at": e["updated_at"]}
                 for e in snap["dict"]],
        "aliases": [{"alias": e["alias"], "target": e["target"],
                     "updated_at": e["updated_at"]}
                    for e in snap["aliases"]],
        "reviews": [{"id": r["id"], "name": r["name"],
                     "declared": sorted(r["declared"]),
                     "text_a": r["text_a"], "text_b": r["text_b"],
                     "label_a": r["label_a"], "label_b": r["label_b"],
                     "result": r["result"], "created_at": r["created_at"],
                     "dict_fingerprint": r["dict_fingerprint"]}
                    for r in snap["reviews"]],
    }
    payload = {"format": EXPORT_FORMAT, "version": EXPORT_VERSION,
               "data": data}
    return {**payload, "counts": _counts_of(data),
            "sha256": package_digest(payload)}


# ---------- 预检校验（纯函数，不访问数据库） ----------

def _problem(section: str, index, ptype: str, message: str) -> dict:
    return {"section": section, "index": index,
            "type": ptype, "message": message}


def _is_str(v) -> bool:
    return isinstance(v, str)


def _is_str_or_none(v) -> bool:
    return v is None or isinstance(v, str)


def _is_str_list(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _is_review_id(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 1


# 各段条目字段规格：字段名 -> (中文名, 类型校验)
_DICT_FIELDS = {
    "ingredient": ("原料名", _is_str),
    "allergens": ("过敏原列表", _is_str_list),
    "updated_at": ("更新时间", _is_str),
}
_ALIAS_FIELDS = {
    "alias": ("别名", _is_str),
    "target": ("标准名", _is_str),
    "updated_at": ("更新时间", _is_str),
}
_REVIEW_FIELDS = {
    "id": ("记录 ID", _is_review_id),
    "name": ("名称", _is_str),
    "declared": ("标签声明", _is_str_list),
    "text_a": ("配方文本 A", _is_str),
    "text_b": ("配方文本 B", _is_str_or_none),
    "label_a": ("版本 A 标签", _is_str),
    "label_b": ("版本 B 标签", _is_str),
    "result": ("审查结果", lambda v: isinstance(v, dict)),
    "created_at": ("创建时间", _is_str),
    "dict_fingerprint": ("字典指纹", _is_str_or_none),
}


def _check_entry(entry, spec: dict, section: str, index: int,
                 problems: List[dict], non_empty: Tuple[str, ...] = ()) -> bool:
    """按字段规格校验一条记录；通过后才参与后续图校验 / 导入。"""
    if not isinstance(entry, dict):
        problems.append(_problem(section, index, "bad_type",
                                 f"第 {index + 1} 条应为 JSON 对象"))
        return False
    ok = True
    extra = sorted(set(entry) - set(spec))
    if extra:
        problems.append(_problem(
            section, index, "unknown_fields",
            f"第 {index + 1} 条包含无法识别的字段（导入时会丢失）："
            f"{'、'.join(extra)}"))
        ok = False
    for field, (label, checker) in spec.items():
        if field not in entry:
            problems.append(_problem(section, index, "missing_field",
                                     f"第 {index + 1} 条缺少字段「{field}」"
                                     f"（{label}）"))
            ok = False
        elif not checker(entry[field]):
            problems.append(_problem(section, index, "bad_field_type",
                                     f"第 {index + 1} 条字段「{field}」"
                                     f"（{label}）类型不正确"))
            ok = False
    for field in non_empty:
        if (field in entry and isinstance(entry[field], str)
                and not entry[field].strip()):
            problems.append(_problem(section, index, "empty_value",
                                     f"第 {index + 1} 条字段「{field}」不能为空"))
            ok = False
    return ok


def _check_tree(nodes, where: str, index: int, problems: List[dict],
                depth: int) -> None:
    if depth > _MAX_TREE_DEPTH:
        problems.append(_problem("reviews", index, "bad_result",
                                 f"{where}配料树嵌套超过 {_MAX_TREE_DEPTH} 层"))
        return
    for pos, node in enumerate(nodes):
        nwhere = f"{where}第 {pos + 1} 个配料"
        if not isinstance(node, dict):
            problems.append(_problem("reviews", index, "bad_result",
                                     f"{nwhere}应为 JSON 对象"))
            continue
        if not _is_str(node.get("name")):
            problems.append(_problem("reviews", index, "bad_result",
                                     f"{nwhere}的 name 应为字符串"))
        for field in ("own_allergens", "total_allergens"):
            if not _is_str_list(node.get(field)):
                problems.append(_problem("reviews", index, "bad_result",
                                         f"{nwhere}的 {field} 应为字符串数组"))
        if not isinstance(node.get("unknown"), bool):
            problems.append(_problem("reviews", index, "bad_result",
                                     f"{nwhere}的 unknown 应为布尔值"))
        children = node.get("children")
        if not isinstance(children, list):
            problems.append(_problem("reviews", index, "bad_result",
                                     f"{nwhere}的 children 应为数组"))
        else:
            _check_tree(children, nwhere + "的", index, problems, depth + 1)


def _check_version_obj(value, vlabel: str, index: int,
                       problems: List[dict]) -> None:
    where = f"result.{vlabel}"
    if not isinstance(value, dict):
        problems.append(_problem("reviews", index, "bad_result",
                                 f"{where} 应为 JSON 对象"))
        return
    for field in ("allergens", "unknown_ingredients"):
        if not _is_str_list(value.get(field)):
            problems.append(_problem("reviews", index, "bad_result",
                                     f"{where}.{field} 应为字符串数组"))
    tree = value.get("tree")
    if not isinstance(tree, list):
        problems.append(_problem("reviews", index, "bad_result",
                                 f"{where}.tree 应为数组"))
    else:
        _check_tree(tree, f"{where}.tree 下", index, problems, 0)


def _check_review_result(result, index: int, problems: List[dict]) -> None:
    """审查记录结果结构校验（与持久化的审查结果 JSON 口径一致）。"""
    if not isinstance(result, dict):
        problems.append(_problem("reviews", index, "bad_result",
                                 "result 应为 JSON 对象"))
        return
    for key in ("version_a", "version_b", "diff", "ingredient_changes",
                "label_check", "label_check_version"):
        if key not in result:
            problems.append(_problem("reviews", index, "bad_result",
                                     f"result 缺少「{key}」"))
    if "version_a" in result:
        _check_version_obj(result["version_a"], "version_a", index, problems)
    if result.get("version_b") is not None:
        _check_version_obj(result["version_b"], "version_b", index, problems)
    if "label_check_version" in result and not _is_str(result["label_check_version"]):
        problems.append(_problem("reviews", index, "bad_result",
                                 "result.label_check_version 应为字符串"))
    if "label_check" in result:
        check = result["label_check"]
        if not isinstance(check, dict):
            problems.append(_problem("reviews", index, "bad_result",
                                     "result.label_check 应为 JSON 对象"))
        else:
            for field in ("missing", "over_reported", "matched"):
                if not _is_str_list(check.get(field)):
                    problems.append(_problem(
                        "reviews", index, "bad_result",
                        f"result.label_check.{field} 应为字符串数组"))
            sources = check.get("missing_sources")
            if sources is not None and not (
                    isinstance(sources, dict) and all(
                        _is_str(k) and isinstance(v, list) and all(
                            isinstance(p, list) and all(_is_str(x) for x in p)
                            for p in v)
                        for k, v in sources.items())):
                problems.append(_problem(
                    "reviews", index, "bad_result",
                    "result.label_check.missing_sources 应为"
                    "「过敏原 → 来源路径数组」"))
    if "diff" in result and result["diff"] is not None:
        diff = result["diff"]
        if not isinstance(diff, dict):
            problems.append(_problem("reviews", index, "bad_result",
                                     "result.diff 应为 JSON 对象或 null"))
        else:
            for field in ("added", "removed", "unchanged"):
                if not _is_str_list(diff.get(field)):
                    problems.append(_problem(
                        "reviews", index, "bad_result",
                        f"result.diff.{field} 应为字符串数组"))
    if "ingredient_changes" in result and result["ingredient_changes"] is not None:
        changes = result["ingredient_changes"]
        if not isinstance(changes, list):
            problems.append(_problem("reviews", index, "bad_result",
                                     "result.ingredient_changes 应为数组或 null"))
        else:
            for pos, c in enumerate(changes):
                if not (isinstance(c, dict) and _is_str(c.get("ingredient"))
                        and _is_str(c.get("change"))):
                    problems.append(_problem(
                        "reviews", index, "bad_result",
                        f"result.ingredient_changes 第 {pos + 1} 项应为含 "
                        "ingredient / change 的对象"))
                    continue
                kind = c["change"]
                if kind == "modified":
                    for field in ("added", "removed"):
                        if not _is_str_list(c.get(field)):
                            problems.append(_problem(
                                "reviews", index, "bad_result",
                                f"result.ingredient_changes 第 {pos + 1} 项的 "
                                f"{field} 应为字符串数组"))
                elif kind in ("added", "removed"):
                    if not _is_str_list(c.get("allergens")):
                        problems.append(_problem(
                            "reviews", index, "bad_result",
                            f"result.ingredient_changes 第 {pos + 1} 项的 "
                            "allergens 应为字符串数组"))
                else:
                    problems.append(_problem(
                        "reviews", index, "bad_result",
                        f"result.ingredient_changes 第 {pos + 1} 项变更类型"
                        f"「{kind}」无法识别"))


def _check_alias_graph(entries: List[Tuple[int, dict]],
                       problems: List[dict]) -> None:
    """别名图校验：归一化后别名重复、自环与任意长度环逐项报告。"""
    edges: Dict[str, str] = {}
    display: Dict[str, str] = {}
    first_index: Dict[str, int] = {}
    for i, e in entries:
        key = normalize_name(e["alias"])
        if key in edges:
            problems.append(_problem(
                "aliases", i, "duplicate_alias",
                f"第 {i + 1} 条别名「{e['alias']}」与第 {first_index[key] + 1} "
                "条归一化后重复，同一别名只能有一条映射"))
            continue
        edges[key] = normalize_name(e["target"])
        display[key] = e["alias"]
        first_index[key] = i
    for cyc in _find_cycles(edges):
        # 确定性起点：按本包内最早出现的环成员开头
        start = min(range(len(cyc)),
                    key=lambda j: (first_index.get(cyc[j], 0), cyc[j]))
        rotated = cyc[start:] + cyc[:start]
        names = [display.get(k, k) for k in rotated]
        lines = "、".join(str(first_index[k] + 1) for k in rotated)
        if len(cyc) == 1:
            problems.append(_problem(
                "aliases", first_index[rotated[0]], "self_loop",
                f"第 {lines} 条别名「{names[0]}」指向自身（自环）"))
        else:
            loop = names + [names[0]]
            problems.append(_problem(
                "aliases", min(first_index[k] for k in rotated), "cycle",
                f"第 {lines} 条别名构成环路：{' → '.join(loop)}"))


def validate_package(package) -> dict:
    """逐项校验迁移包（纯函数，不访问数据库）。

    返回 ``{"ok", "problems", "warnings", "counts", "sha256"}``：
    problems 逐项给出 section / index / type / message；全部通过时 ok=True
    并给出面向确认的警告（warnings）。
    """
    problems: List[dict] = []
    if not isinstance(package, dict):
        return {"ok": False,
                "problems": [_problem("package", None, "not_object",
                                      "迁移包顶层应为 JSON 对象")],
                "warnings": [], "counts": None, "sha256": None}

    fmt = package.get("format")
    if fmt != EXPORT_FORMAT:
        problems.append(_problem(
            "package", None, "bad_format",
            f"format 应为「{EXPORT_FORMAT}」，实际为 {fmt!r}"))
    version = package.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        problems.append(_problem("package", None, "bad_version",
                                 "version 应为整数格式版本号"))
    elif version != EXPORT_VERSION:
        problems.append(_problem(
            "package", None, "unsupported_version",
            f"不支持的格式版本 {version}（当前支持 {EXPORT_VERSION}）"))
    sha = package.get("sha256")
    sha_valid = isinstance(sha, str) and bool(_SHA256_RE.fullmatch(sha))
    if not sha_valid:
        problems.append(_problem("package", None, "bad_sha256_field",
                                 "缺少有效的 sha256 摘要字段（64 位十六进制）"))

    data = package.get("data")
    if not isinstance(data, dict):
        problems.append(_problem(
            "package", None, "bad_data",
            "data 应为包含 dict / aliases / reviews 三段数据的 JSON 对象"))
        data = {}
    extra_sections = sorted(set(data) - {"dict", "aliases", "reviews"})
    if extra_sections:
        problems.append(_problem(
            "package", None, "unknown_fields",
            f"data 包含无法识别的数据段（导入时会丢失）："
            f"{'、'.join(extra_sections)}"))
    sections: Dict[str, list] = {}
    for key, label in (("dict", "字典"), ("aliases", "别名"),
                       ("reviews", "审查记录")):
        sec = data.get(key)
        if sec is None:
            problems.append(_problem("package", None, "missing_section",
                                     f"data 缺少「{key}」段（{label}）"))
            sec = []
        elif not isinstance(sec, list):
            problems.append(_problem("package", None, "bad_section",
                                     f"data.{key}（{label}）应为数组"))
            sec = []
        sections[key] = sec

    # 摘要校验：按规范化内容重算，与文件声明值比对
    if sha_valid and {"format", "version", "data"} <= set(package):
        actual = package_digest(package)
        if actual != sha.lower():
            problems.append(_problem(
                "package", None, "digest_mismatch",
                f"SHA-256 摘要不匹配（文件声明 {sha[:12]}…，按内容重算 "
                f"{actual[:12]}…）：文件内容已被修改或已损坏"))

    # counts 为派生信息字段：存在时必须与实际数据量一致
    counts = package.get("counts")
    if counts is not None:
        if not isinstance(counts, dict):
            problems.append(_problem("package", None, "bad_counts",
                                     "counts 应为 JSON 对象"))
        else:
            for key in ("dict", "aliases", "reviews"):
                c = counts.get(key)
                if (not isinstance(c, int) or isinstance(c, bool) or c < 0):
                    problems.append(_problem(
                        "package", None, "bad_counts",
                        f"counts.{key} 应为非负整数"))
                elif c != len(sections[key]):
                    problems.append(_problem(
                        "package", None, "counts_mismatch",
                        f"counts.{key} 声明为 {c}，实际数据为 "
                        f"{len(sections[key])} 条"))

    # 字典：字段类型 + 主键重复
    seen_ingredients: Dict[str, int] = {}
    for i, e in enumerate(sections["dict"]):
        if not _check_entry(e, _DICT_FIELDS, "dict", i, problems,
                            non_empty=("ingredient",)):
            continue
        if e["ingredient"] in seen_ingredients:
            problems.append(_problem(
                "dict", i, "duplicate_ingredient",
                f"第 {i + 1} 条原料「{e['ingredient']}」与第 "
                f"{seen_ingredients[e['ingredient']] + 1} 条重复"))
        else:
            seen_ingredients[e["ingredient"]] = i

    # 别名：字段类型 + 有向图（重复 / 自环 / 任意长度环）
    valid_aliases = [(i, e) for i, e in enumerate(sections["aliases"])
                     if _check_entry(e, _ALIAS_FIELDS, "aliases", i, problems,
                                     non_empty=("alias", "target"))]
    _check_alias_graph(valid_aliases, problems)

    # 审查记录：字段类型 + 主键重复 + 结果结构
    seen_ids: Dict[int, int] = {}
    valid_reviews: List[Tuple[int, dict]] = []
    for i, e in enumerate(sections["reviews"]):
        if _check_entry(e, _REVIEW_FIELDS, "reviews", i, problems,
                        non_empty=("text_a",)):
            rid = e["id"]
            if rid in seen_ids:
                problems.append(_problem(
                    "reviews", i, "duplicate_id",
                    f"第 {i + 1} 条记录 ID {rid} 与第 {seen_ids[rid] + 1} 条重复"))
            else:
                seen_ids[rid] = i
                valid_reviews.append((i, e))
        if isinstance(e, dict) and "result" in e:
            _check_review_result(e["result"], i, problems)

    ok = not problems
    warnings: List[str] = []
    if ok:
        legacy = sum(1 for _, e in valid_reviews
                     if e["dict_fingerprint"] is None)
        if legacy:
            warnings.append(
                f"{legacy} 条审查记录没有字典指纹，导入后将标记为「需复核」")
        if not any(sections.values()):
            warnings.append(
                "迁移包不包含任何数据（字典 / 别名 / 审查记录均为空），"
                "导入后现有数据将被清空")
    return {"ok": ok, "problems": problems, "warnings": warnings,
            "counts": _counts_of(sections) if isinstance(package.get("data"), dict)
            else None,
            "sha256": sha.lower() if sha_valid else None}


# ---------- 一次性确认令牌 ----------

_tokens: Dict[str, dict] = {}
_token_lock = threading.Lock()


def _purge_expired(now: float) -> None:
    for t in [t for t, e in _tokens.items() if e["expires"] <= now]:
        del _tokens[t]


def issue_token(sha256: str) -> str:
    """签发绑定指定文件摘要的一次性确认令牌（限时有效）。"""
    token = secrets.token_urlsafe(24)
    with _token_lock:
        now = time.time()
        _purge_expired(now)
        _tokens[token] = {"sha256": sha256,
                          "expires": now + TOKEN_TTL_SECONDS}
    return token


def consume_token(token: str, sha256: str) -> bool:
    """令牌存在、未过期且绑定该摘要时消费之（一次性，消费后即失效）。"""
    if not token:
        return False
    with _token_lock:
        entry = _tokens.get(token)
        if not entry or entry["expires"] <= time.time():
            return False
        if entry["sha256"] != sha256:
            return False  # 摘要不匹配不算消费：用户可能选错了文件
        del _tokens[token]
    return True


# ---------- 预检与确认导入 ----------

class MigrationConfirmError(Exception):
    """确认导入被拒绝（令牌无效 / 摘要不匹配 / 校验未通过）。"""

    def __init__(self, message: str, problems: List[dict] = None):
        super().__init__(message)
        self.problems = problems or []


def precheck_package(text: str) -> dict:
    """只读预检：解析并逐项校验迁移包，**不写库**。

    全部通过时签发绑定该文件摘要的一次性确认令牌，并附上包内数据量、
    当前库数据量与面向确认的警告，供页面展示。
    """
    try:
        package = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        outcome = {"ok": False,
                   "problems": [_problem("package", None, "invalid_json",
                                         f"不是合法的 JSON 文件：{exc}")],
                   "warnings": [], "counts": None, "sha256": None}
    else:
        outcome = validate_package(package)
    outcome["current_counts"] = db.table_counts()
    if outcome["ok"]:
        cur = outcome["current_counts"]
        if any(cur.values()):
            outcome["warnings"].append(
                f"导入将整体替换当前数据（现有：字典 {cur['dict']} 条 / "
                f"别名 {cur['aliases']} 条 / 审查记录 {cur['reviews']} 条），"
                "替换后不可恢复")
        outcome["token"] = issue_token(outcome["sha256"])
        outcome["expires_in"] = TOKEN_TTL_SECONDS
    else:
        outcome["token"] = None
    return outcome


def confirm_import(text: str, token: str) -> dict:
    """凭一次性令牌确认导入：单个 SQLite 事务整体替换，失败回滚。

    令牌必须绑定该文件的摘要（即预检通过的是同一份内容）；确认前会
    再次完整校验，任一步失败都不写库或整体回滚。
    """
    try:
        package = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MigrationConfirmError(f"迁移包不是合法的 JSON：{exc}")
    if not isinstance(package, dict):
        raise MigrationConfirmError("迁移包顶层应为 JSON 对象")
    digest = package_digest(package)
    if not consume_token(token, digest):
        raise MigrationConfirmError(
            "确认令牌无效、已使用、已过期或与迁移包摘要不匹配，请重新预检")
    outcome = validate_package(package)
    if not outcome["ok"]:
        raise MigrationConfirmError("迁移包校验未通过，未执行导入",
                                    outcome["problems"])
    data = package["data"]
    db.replace_all(dict_entries=data["dict"], aliases=data["aliases"],
                   reviews=data["reviews"])
    return {"ok": True, "imported": _counts_of(data), "sha256": digest}
