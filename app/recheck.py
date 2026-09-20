"""字典时效复核：用当前字典重新解析原始配方，给出前后差异。

差异覆盖：

- 标签遗漏（missing）的增 / 减，以及每个过敏原来源路径的前后变化；
- 标签多报（over_reported）、声明一致（matched）的增 / 减；
- A / B 两版各自过敏原全集与未识别原料的前后变化。

无论标签核对对象是 A 还是 B，两版的变化都呈现给复核人，
但遗漏来源路径差异只针对实际核对版本计算。
"""
from __future__ import annotations

from typing import Dict, List

from .review import analyze


def _added_removed(before: List[str], after: List[str]) -> dict:
    return {"added": sorted(set(after) - set(before)),
            "removed": sorted(set(before) - set(after)),
            "before": sorted(before), "after": sorted(after)}


def _path_set_diff(minuend: List[List[str]],
                   subtrahend: List[List[str]]) -> List[List[str]]:
    """路径集合差：返回 minuend 中存在、subtrahend 中不存在的路径。"""
    sub = {tuple(p) for p in subtrahend}
    return [p for p in minuend if tuple(p) not in sub]


def _path_changes(old_check: dict, new_check: dict) -> List[dict]:
    """保留项遗漏过敏原的来源路径前后差异（新增 / 消失的路径分别列出）。"""
    kept = set(old_check.get("missing", [])) & set(new_check.get("missing", []))
    old_sources = old_check.get("missing_sources") or {}
    new_sources = new_check.get("missing_sources") or {}
    changes = []
    for allergen in sorted(kept):
        old_paths = sorted(old_sources.get(allergen, []))
        new_paths = sorted(new_sources.get(allergen, []))
        paths_added = _path_set_diff(new_paths, old_paths)
        paths_removed = _path_set_diff(old_paths, new_paths)
        if paths_added or paths_removed:
            changes.append({
                "allergen": allergen,
                "before": old_paths,
                "after": new_paths,
                "paths_added": paths_added,
                "paths_removed": paths_removed,
            })
    return changes


def _version_diff(old_version: dict, new_version: dict) -> dict:
    return {
        "allergens": _added_removed(
            old_version.get("allergens", []), new_version.get("allergens", [])),
        "unknown_ingredients": _added_removed(
            old_version.get("unknown_ingredients", []),
            new_version.get("unknown_ingredients", [])),
    }


def _has_value(delta: dict) -> bool:
    return bool(delta.get("added") or delta.get("removed"))


def build_recheck(review: dict) -> dict:
    """用当前字典重新解析审查记录的原始 A/B 配方与标签声明。

    返回 {"old_result": 旧结果, "new_result": 新结果,
          "fingerprint": 新指纹, "diff": 前后差异, "changed": 是否存在差异}。
    旧结果若缺 missing_sources（升级前记录），由 db 读取时即时补齐，
    因此来源路径的前后差异同样可用于旧记录。
    """
    new_result, fingerprint = analyze(
        text_a=review["text_a"], text_b=review["text_b"],
        label_a=review["label_a"], label_b=review["label_b"],
        declared=review["declared"])

    old_result = review.get("result") or {}
    old_check = old_result.get("label_check") or {}
    new_check = new_result["label_check"]

    label = {
        "missing_delta": _added_removed(
            old_check.get("missing", []), new_check["missing"]),
        "over_reported_delta": _added_removed(
            old_check.get("over_reported", []), new_check["over_reported"]),
        "matched_delta": _added_removed(
            old_check.get("matched", []), new_check["matched"]),
        "path_changes": _path_changes(old_check, new_check),
        "label_check_version": {
            "before": old_result.get("label_check_version", review["label_a"]),
            "after": new_result["label_check_version"],
        },
    }
    versions = {}
    for key in ("version_a", "version_b"):
        old_v, new_v = old_result.get(key), new_result.get(key)
        if old_v and new_v:
            versions[key] = _version_diff(old_v, new_v)

    changed = (
        _has_value(label["missing_delta"])
        or _has_value(label["over_reported_delta"])
        or _has_value(label["matched_delta"])
        or bool(label["path_changes"])
        or any(
            _has_value(v["allergens"]) or _has_value(v["unknown_ingredients"])
            for v in versions.values())
    )
    return {
        "old_result": old_result,
        "new_result": new_result,
        "fingerprint": fingerprint,
        "diff": {"label": label, "versions": versions},
        "changed": changed,
    }
