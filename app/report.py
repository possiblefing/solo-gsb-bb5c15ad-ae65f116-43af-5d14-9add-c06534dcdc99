"""标签审查报告导出：HTML（可直接打印/存档）与 Markdown 两种格式。"""
from __future__ import annotations

import html
from typing import List


def _esc(s) -> str:
    return html.escape(str(s))


def _badges(allergens: List[str], own: List[str]) -> str:
    out = []
    for a in allergens:
        cls = "badge own" if a in own else "badge"
        out.append(f'<span class="{cls}">{_esc(a)}</span>')
    return " ".join(out)


def _tree_html(nodes: List[dict]) -> str:
    if not nodes:
        return ""
    items = []
    for n in nodes:
        unknown = ' <span class="unknown">[未识别原料]</span>' if n.get("unknown") else ""
        children = _tree_html(n.get("children", []))
        items.append(
            f"<li><b>{_esc(n['name'])}</b>{unknown} "
            f"{_badges(n['total_allergens'], n['own_allergens'])}{children}</li>")
    return "<ul>" + "".join(items) + "</ul>"


def _path_html(path: List[str]) -> str:
    spans = []
    for i, part in enumerate(path):
        cls = " class='src-leaf'" if i == len(path) - 1 else ""
        spans.append(f"<span{cls}>{_esc(part)}</span>")
    return "<li>" + " <span class='arrow'>&rsaquo;</span> ".join(spans) + "</li>"


def _missing_cell(check: dict) -> str:
    """遗漏过敏原单元格：逐项列出来源路径，同名原料的不同分支路径不合并。"""
    items = check.get("missing", [])
    if not items:
        return "<span class='none'>无</span>"
    sources = check.get("missing_sources") or {}
    blocks = []
    for a in items:
        paths = sources.get(a, [])
        if paths:
            path_items = "".join(_path_html(p) for p in paths)
            detail = (f"<details class='src' open><summary>来源路径（{len(paths)}）</summary>"
                      f"<ul class='src-paths'>{path_items}</ul></details>")
        else:
            detail = ""
        blocks.append(
            f"<div class='miss-item'><span class=\"badge miss\">{_esc(a)}</span>{detail}</div>")
    return "".join(blocks)


def _check_section(check: dict) -> str:
    def row(title, cls, items, hint):
        if items:
            badges = " ".join(f'<span class="badge {cls}">{_esc(a)}</span>' for a in items)
        else:
            badges = "<span class='none'>无</span>"
        return f"<tr><th>{title}</th><td>{badges}</td><td class='hint'>{hint}</td></tr>"

    missing_row = (
        "<tr><th>标签遗漏（必须补充）</th>"
        f"<td>{_missing_cell(check)}</td>"
        "<td class='hint'>配方中实际含有，但标签未声明的过敏原；"
        "展开可查看嵌套配料树中的全部来源路径</td></tr>")

    return f"""
    <table class="check">
      {missing_row}
      {row("标签多报", "over", check.get("over_reported", []),
            "标签声明了，但配方中未检出的过敏原")}
      {row("声明一致", "ok", check.get("matched", []),
            "标签声明与配方计算结果一致")}
    </table>"""


def _version_section(title: str, version: dict) -> str:
    unknown = version.get("unknown_ingredients", [])
    unknown_html = (
        f"<p class='warn'>未识别原料（字典中未登记，请补充）:"
        f"{'、'.join(_esc(u) for u in unknown)}</p>" if unknown else "")
    allergens = "、".join(_esc(a) for a in version["allergens"]) or "无"
    return f"""
    <h3>{_esc(title)}</h3>
    <p>过敏原全集：<b>{allergens}</b></p>
    {unknown_html}
    {_tree_html(version['tree'])}"""


def _diff_section(result: dict, label_a: str, label_b: str) -> str:
    diff = result.get("diff")
    if not diff:
        return ""
    def fmt(items):
        return "、".join(_esc(a) for a in items) or "无"
    rows = "".join(
        f"<tr><td>{_esc(c['ingredient'])}</td><td>{_esc(change_text(c))}</td></tr>"
        for c in (result.get("ingredient_changes") or []))
    changes_table = (f"<table class='check'><tr><th>配料</th><th>变化</th></tr>{rows}</table>"
                     if rows else "<p>各配料过敏原无变化。</p>")
    return f"""
    <h2>配方版本对比（{_esc(label_a)} → {_esc(label_b)}）</h2>
    <p>新增过敏原：<b class="miss">{fmt(diff['added'])}</b><br>
       减少过敏原：<b class="ok">{fmt(diff['removed'])}</b><br>
       保持不变：{fmt(diff['unchanged'])}</p>
    {changes_table}"""


def change_text(change: dict) -> str:
    if change["change"] == "added":
        return f"新增配料，引入过敏原：{'、'.join(change['allergens']) or '无'}"
    if change["change"] == "removed":
        return f"移除配料，原含过敏原：{'、'.join(change['allergens']) or '无'}"
    parts = []
    if change.get("added"):
        parts.append("新增 " + "、".join(change["added"]))
    if change.get("removed"):
        parts.append("减少 " + "、".join(change["removed"]))
    return "；".join(parts)


def render_html_report(review: dict) -> str:
    result = review["result"]
    checked = result.get("label_check_version", review["label_a"])
    diff_html = _diff_section(result, review["label_a"], review["label_b"])
    version_b = result.get("version_b")
    version_b_html = _version_section(review["label_b"], version_b) if version_b else ""
    declared = "、".join(_esc(a) for a in review["declared"]) or "（未填写）"
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>标签审查报告 - {_esc(review['name'])}</title>
<style>
 body {{ font-family: "PingFang SC", "Microsoft YaHei", sans-serif;
        max-width: 860px; margin: 2rem auto; padding: 0 1rem; color: #222; }}
 h1 {{ border-bottom: 3px solid #2c7; padding-bottom: .4rem; }}
 h2 {{ margin-top: 2rem; color: #17457a; }}
 table.check {{ border-collapse: collapse; width: 100%; }}
 table.check th, table.check td {{ border: 1px solid #ccc; padding: .5rem .7rem;
        text-align: left; vertical-align: top; }}
 table.check th {{ background: #f4f6f8; width: 12em; }}
 .badge {{ display: inline-block; background: #e8eef7; border-radius: 10px;
        padding: 1px 10px; margin: 2px; font-size: .9em; }}
 .badge.own {{ background: #cfe3cf; font-weight: 600; }}
 .badge.miss, .miss {{ background: #ffd9d9; color: #a00; }}
 .badge.over {{ background: #ffe9c2; color: #965a00; }}
 .badge.ok, .ok {{ background: #d8f0d8; color: #14641a; }}
 .unknown {{ color: #b26a00; font-size: .85em; }}
 .warn {{ color: #b26a00; }}
 .hint {{ color: #777; font-size: .85em; }}
 .none {{ color: #999; }}
 ul {{ line-height: 1.9; }}
 .meta {{ color: #666; }}
 .miss-item {{ margin: 4px 0; }}
 details.src {{ margin: 2px 0 4px; }}
 details.src summary {{ cursor: pointer; color: #777; font-size: .85em; }}
 ul.src-paths {{ margin: 4px 0; padding-left: 1.4em; line-height: 1.7; }}
 ul.src-paths li {{ list-style: disc; }}
 .arrow {{ color: #999; margin: 0 2px; }}
 .src-leaf {{ font-weight: 600; color: #a00; }}
</style></head><body>
<h1>食品过敏原标签审查报告</h1>
<p class="meta">审查名称：<b>{_esc(review['name'])}</b>
   生成时间：{_esc(review['created_at'])}　报告编号：#{review['id']}</p>

<h2>标签核对结论（核对对象：{_esc(checked)}）</h2>
<p>标签声明的过敏原：{declared}</p>
{_check_section(result['label_check'])}

<h2>配方解析与过敏原传播</h2>
<p class="hint">深绿色徽章为该原料自身标注的过敏原，浅色徽章为子配料传播上来的过敏原。</p>
{_version_section(review['label_a'], result['version_a'])}
{version_b_html}
{diff_html}
<hr><p class="hint">本报告由食品过敏原标签审查台自动生成，供标签合规审查参考。</p>
</body></html>"""


def _tree_md(nodes: List[dict], indent: int = 0) -> str:
    lines = []
    for n in nodes:
        allergens = "、".join(n["total_allergens"]) or "无"
        unknown = "（未识别原料）" if n.get("unknown") else ""
        lines.append(f"{'  ' * indent}- **{n['name']}**{unknown} — 过敏原：{allergens}")
        lines.append(_tree_md(n.get("children", []), indent + 1))
    return "\n".join(l for l in lines if l)


def _missing_md(check: dict) -> List[str]:
    """遗漏过敏原及其在嵌套配料树中的全部来源路径（Markdown 列表）。"""
    missing = check.get("missing", [])
    lines = [f"- **标签遗漏（必须补充）**：{'、'.join(missing) if missing else '无'}"]
    sources = check.get("missing_sources") or {}
    for a in missing:
        lines.append(f"  - **{a}**")
        paths = sources.get(a) or []
        if not paths:
            lines.append("    - （无来源路径信息）")
        for p in paths:
            lines.append("    - " + " › ".join(p))
    return lines


def render_markdown_report(review: dict) -> str:
    result = review["result"]
    check = result["label_check"]
    checked = result.get("label_check_version", review["label_a"])

    def fmt(items):
        return "、".join(items) or "无"

    parts = [
        f"# 食品过敏原标签审查报告\n",
        f"- 审查名称：{review['name']}",
        f"- 报告编号：#{review['id']}",
        f"- 生成时间：{review['created_at']}\n",
        f"## 标签核对结论（核对对象：{checked}）\n",
        f"- 标签声明：{fmt(review['declared'])}",
        *_missing_md(check),
        f"- **标签多报**：{fmt(check['over_reported'])}",
        f"- 声明一致：{fmt(check['matched'])}\n",
        f"## {review['label_a']}\n",
        f"过敏原全集：{fmt(result['version_a']['allergens'])}\n",
        _tree_md(result["version_a"]["tree"]),
    ]
    version_b = result.get("version_b")
    if version_b:
        parts += [
            f"\n## {review['label_b']}\n",
            f"过敏原全集：{fmt(version_b['allergens'])}\n",
            _tree_md(version_b["tree"]),
        ]
    diff = result.get("diff")
    if diff:
        parts += [
            f"\n## 版本对比（{review['label_a']} → {review['label_b']}）\n",
            f"- 新增过敏原：{fmt(diff['added'])}",
            f"- 减少过敏原：{fmt(diff['removed'])}",
            f"- 保持不变：{fmt(diff['unchanged'])}",
        ]
        changes = result.get("ingredient_changes") or []
        if changes:
            parts.append("\n| 配料 | 变化 |")
            parts.append("| --- | --- |")
            for c in changes:
                parts.append(f"| {c['ingredient']} | {change_text(c)} |")
    parts.append("\n---\n本报告由食品过敏原标签审查台自动生成。")
    return "\n".join(parts)
