"""端到端集成冒烟测试：不依赖 FastAPI，直接走 db + parser + analysis + report 全链路。"""
import os
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "smoke.db")

from app import db
from app.analysis import (attach_allergens, collect_allergens, compare_label,
                          diff_allergens, diff_ingredients, flatten)
from app.fingerprint import result_keys, scoped_fingerprint
from app.parser import parse_recipe
from app.report import render_html_report, render_markdown_report

db.init_db()

# 1. 导入字典
n = db.bulk_import("""\
小麦粉：麸质谷物
乳清粉：乳
乳糖：乳
奶粉：乳
可可脂：
大豆卵磷脂：大豆
鸡蛋：蛋类
椰子粉：坚果
芝麻：芝麻
食盐：
""")
assert n == 10, n
print(f"1. 字典导入 {n} 条 OK")

# 2. 两版配方
TEXT_A = "配料表：小麦粉，白砂糖，巧克力酱（可可脂，奶粉（乳清粉，乳糖），大豆卵磷脂），鸡蛋，食盐"
TEXT_B = "配料表：小麦粉，白砂糖，巧克力酱（可可脂，椰子粉），鸡蛋，芝麻酱（芝麻），食盐"
DECLARED = ["麸质谷物", "蛋类"]

dictionary = db.dict_lookup()

def build_version(tree):
    return {
        "tree": [x.to_dict() for x in tree],
        "allergens": sorted(collect_allergens(tree)),
        "unknown_ingredients": sorted({x.name for x in flatten(tree) if x.unknown}),
    }

tree_a = parse_recipe(TEXT_A)
attach_allergens(tree_a, dictionary)
va = build_version(tree_a)
tree_b = parse_recipe(TEXT_B)
attach_allergens(tree_b, dictionary)
vb = build_version(tree_b)

assert set(va["allergens"]) == {"麸质谷物", "乳", "大豆", "蛋类"}, va["allergens"]
assert set(vb["allergens"]) == {"麸质谷物", "坚果", "蛋类", "芝麻"}, vb["allergens"]
assert "白砂糖" in va["unknown_ingredients"]  # 未登记原料被标记
print("2. 两版配方解析与过敏原传播 OK")
print("   A 版过敏原:", va["allergens"])
print("   B 版过敏原:", vb["allergens"])

# 3. 标签核对（对照 B 版，含遗漏来源路径）
check = compare_label(vb["allergens"], DECLARED, vb["tree"])
assert check["missing"] == ["坚果", "芝麻"], check
assert check["over_reported"] == [], check
assert check["matched"] == ["蛋类", "麸质谷物"], check
assert check["missing_sources"]["坚果"] == [["巧克力酱", "椰子粉"]], check
assert check["missing_sources"]["芝麻"] == [["芝麻酱", "芝麻"]], check
print("3. 标签核对 OK  遗漏:", check["missing"], " 多报:", check["over_reported"])
print("   来源路径:", check["missing_sources"])

# 3b. 同名原料处于不同分支时来源路径分别保留，不合并
tree_c = parse_recipe("巧克力酱（奶粉（乳清粉）），夹心酱（奶粉（乳清粉））")
attach_allergens(tree_c, dictionary)
vc = build_version(tree_c)
check_c = compare_label(vc["allergens"], [], vc["tree"])
assert check_c["missing_sources"]["乳"] == [
    ["夹心酱", "奶粉"],
    ["夹心酱", "奶粉", "乳清粉"],
    ["巧克力酱", "奶粉"],
    ["巧克力酱", "奶粉", "乳清粉"],
], check_c["missing_sources"]
# 父节点自身标注与子配料传播各产生一条路径；同名分支分别保留、不合并
print("3b. 同名分支路径不合并 / 自身标注+子配料传播 OK")

# 4. 版本对比
diff = diff_allergens(va["allergens"], vb["allergens"])
assert diff["added"] == ["坚果", "芝麻"]
assert diff["removed"] == ["乳", "大豆"]
changes = diff_ingredients(tree_a, tree_b)
names = {c["ingredient"]: c["change"] for c in changes}
assert names.get("巧克力酱") == "modified" and names.get("芝麻酱") == "added", names
print("4. 版本对比 OK  新增:", diff["added"], " 减少:", diff["removed"])

# 5. 入库 + 报告导出
result = {
    "version_a": va, "version_b": vb, "diff": diff,
    "ingredient_changes": changes,
    "label_check": check, "label_check_version": "新版配方",
}
rid = db.create_review(name="巧克力饼干 配方变更审查", declared=DECLARED,
                       text_a=TEXT_A, text_b=TEXT_B,
                       label_a="现版配方", label_b="新版配方", result=result,
                       dict_fingerprint=scoped_fingerprint(
                           result_keys(result), dictionary))
review = db.get_review(rid)
html_report = render_html_report(review)
md_report = render_markdown_report(review)
assert "标签遗漏" in html_report and "坚果" in html_report and "芝麻" in html_report
assert "巧克力酱" in html_report and "来源路径" in html_report
assert "巧克力酱 › 椰子粉" in md_report and "芝麻酱 › 芝麻" in md_report
assert "# 食品过敏原标签审查报告" in md_report and "新增过敏原" in md_report
assert len(db.list_reviews()) == 1

# 5b. 模拟升级前的旧记录（结果中无 missing_sources），重新打开时自动补齐
old_result = dict(result)
old_result["label_check"] = {k: v for k, v in check.items()
                             if k != "missing_sources"}
old_rid = db.create_review(name="旧版记录", declared=DECLARED,
                           text_a=TEXT_A, text_b=TEXT_B,
                           label_a="现版配方", label_b="新版配方",
                           result=old_result)
old_review = db.get_review(old_rid)
assert old_review["result"]["label_check"]["missing_sources"]["坚果"] == \
    [["巧克力酱", "椰子粉"]]
print(f"5. 审查记录入库 (#{rid}) 与 HTML/Markdown 报告导出 OK；旧记录回填 OK")

# 6. 字典时效复核
from app.recheck import build_recheck

# 6a. 刚创建的记录指纹与当前字典一致，时效正常
assert db.get_review(rid)["needs_recheck"] is False

# 6b. 无关原料增删 / 顺序变化 / 同值重导不得误报
db.upsert_entry("无关原料", ["芥末"])
assert db.get_review(rid)["needs_recheck"] is False
db.delete_entry("无关原料")
db.bulk_import("\n".join(reversed("""\
小麦粉：麸质谷物
乳清粉：乳
乳糖：乳
奶粉：乳
可可脂：
大豆卵磷脂：大豆
鸡蛋：蛋类
椰子粉：坚果
芝麻：芝麻
食盐：
""".strip().splitlines())))
assert db.get_review(rid)["needs_recheck"] is False, "顺序/无关变化不得误报"

# 6c. 配方实际涉及原料的映射变化 -> 需复核（两版取并集，椰子粉仅在 B 版）
db.upsert_entry("椰子粉", ["坚果", "大豆"])
stale = db.get_review(rid)
assert stale["needs_recheck"] and stale["recheck_reason"] == "stale"
outcome = build_recheck(stale)
assert outcome["changed"]
# B 版（核对对象）新增遗漏“大豆”，来源路径为 巧克力酱 › 椰子粉
assert "大豆" in outcome["diff"]["label"]["missing_delta"]["added"]
assert ["巧克力酱", "椰子粉"] in \
    outcome["new_result"]["label_check"]["missing_sources"]["大豆"]

# 6d. 确认复核 -> 覆盖结果、刷新指纹、恢复时效正常
db.update_review_result(rid, outcome["new_result"], outcome["fingerprint"])
assert db.get_review(rid)["needs_recheck"] is False
# 复核后报告可继续导出，且包含新结论
assert "大豆" in render_markdown_report(db.get_review(rid))

# 6e. 升级前旧记录无指纹 -> 按需复核；复核确认后补上指纹
assert db.get_review(old_rid)["needs_recheck"] is True
assert db.get_review(old_rid)["recheck_reason"] == "legacy"
legacy_outcome = build_recheck(db.get_review(old_rid))
db.update_review_result(old_rid, legacy_outcome["new_result"],
                        legacy_outcome["fingerprint"])
assert db.get_review(old_rid)["needs_recheck"] is False
print("6. 字典时效复核（指纹/误报防护/前后差异/确认覆盖/旧记录） OK")

# 7. 配料占比区间顺序校验（临时预览，不写历史）
from app.proportion import analyze_proportions

# 7a. 可行：前向/后向区间传播收紧，鸡蛋与食盐互相锁定为 20%
prop = analyze_proportions(
    "配料表：小麦粉[40-55%]，巧克力酱[25-60%]（可可脂[10-18%]，奶粉[12-15%]），"
    "鸡蛋[5-20%]，食盐[20-30%]")
assert prop["status"] == "feasible" and prop["group_count"] == 2, prop
top = next(g for g in prop["groups"] if g["path"] == [])["items"]
assert top[1]["tightened"] == [25.0, 55.0]          # 上界被小麦粉收紧
assert top[2]["tightened"] == [20.0, 20.0] and top[2]["pinned"]
assert top[3]["tightened"] == [20.0, 20.0] and top[3]["pinned"]
child = next(g for g in prop["groups"] if g["path"] == ["巧克力酱"])["items"]
assert child[0]["tightened"] == [12.0, 18.0]        # 下界被奶粉收紧

# 7b. 不可行：返回最短冲突片段及完整嵌套路径
prop = analyze_proportions(
    "小麦粉[40-55%]，巧克力酱[25-35%]（可可脂[5-8%]，奶粉[12-15%]）")
assert prop["status"] == "infeasible", prop
[prop_conflict] = prop["conflicts"]
assert prop_conflict["path"] == ["巧克力酱"]
assert [f["name"] for f in prop_conflict["fragment"]] == ["可可脂", "奶粉"]

# 7c. 标注问题定位到具体路径，且全程不产生审查记录
prop = analyze_proportions("巧克力酱[25-60%]（可可脂，奶粉[12-15%]），鸡蛋")
assert prop["status"] == "error"
assert {tuple(e["path"]) for e in prop["errors"]} == {
    ("巧克力酱", "可可脂"), ("鸡蛋",)}
assert len(db.list_reviews()) == 2  # 仍只有第 5 步创建的两条
print("7. 配料占比区间顺序校验（收紧/锁定/最短冲突片段/路径定位/只读） OK")

# 8. 原料别名（仅接入正式审查与字典时效复核）
from app.aliases import validate_alias_batch  # noqa: E402
from app.review import analyze  # noqa: E402

# 8a. 整批原子校验：自环 / 任意长度环 / 同一别名多目标全部拒绝且不写入
bad = validate_alias_batch("脱盐乳清：脱盐乳清", db.alias_existing())
assert bad["errors"] and bad["errors"][0]["type"] == "self_loop"
bad = validate_alias_batch("甲：乙\n乙：丙\n丙：甲", db.alias_existing())
assert bad["errors"][0]["type"] == "cycle"
assert bad["errors"][0]["cycle"] == ["甲", "乙", "丙", "甲"]
assert bad["errors"][0]["lines"] == [1, 2, 3]
bad = validate_alias_batch("脱盐乳清：乳清粉\n脱盐乳清：奶粉",
                           db.alias_existing())
assert [e["type"] for e in bad["errors"]] == ["multi_target"]
assert bad["entries"] == []
assert db.list_aliases() == []  # 拒绝整批，库内无任何别名

# 8b. 合法别名链写入：脱盐乳清 -> 乳清粉（-> 最终标准名即乳清粉）
ok = validate_alias_batch("脱盐乳清：乳清粉", db.alias_existing())
assert not ok["errors"] and ok["entries"] == [("脱盐乳清", "乳清粉")]
db.bulk_upsert_aliases(ok["entries"])
resolver = db.alias_resolver()
assert resolver.resolve("脱盐乳清") == "乳清粉"

# 8c. 正式审查：别名链解析到最终标准名参与字典查询，但树/路径保留原名
result_alias, fp_alias = analyze(
    text_a="配料表：小麦粉，巧克力酱（脱盐乳清），鸡蛋",
    text_b=None, label_a="现版配方", label_b="新版配方",
    declared=["麸质谷物", "蛋类"])
assert "乳" in result_alias["version_a"]["allergens"]
assert "脱盐乳清" not in result_alias["version_a"]["unknown_ingredients"]
tree_names = result_alias["version_a"]["tree"]
assert tree_names[1]["name"] == "巧克力酱"
assert tree_names[1]["children"][0]["name"] == "脱盐乳清"  # 原始名称
assert result_alias["label_check"]["missing_sources"]["乳"] == \
    [["巧克力酱", "脱盐乳清"]]
rid_alias = db.create_review(
    name="别名审查", declared=["麸质谷物", "蛋类"],
    text_a="配料表：小麦粉，巧克力酱（脱盐乳清），鸡蛋", text_b=None,
    label_a="现版配方", label_b="新版配方",
    result=result_alias, dict_fingerprint=fp_alias)
assert db.get_review(rid_alias)["needs_recheck"] is False

# 8d. 别名规则变化但最终标准名/映射不变（缩短等价链路）不复核；
#     改写为不同最终标准名 -> 需复核
ok = validate_alias_batch("脱盐乳清：中转名\n中转名：乳清粉",
                          db.alias_existing())
db.bulk_upsert_aliases(ok["entries"])
assert db.get_review(rid_alias)["needs_recheck"] is False
ok = validate_alias_batch("脱盐乳清：奶粉", db.alias_existing())
db.bulk_upsert_aliases(ok["entries"])
assert db.get_review(rid_alias)["needs_recheck"] is True
# 无关别名变化不复核其它记录
assert db.get_review(rid)["needs_recheck"] is False

# 8e. 供应商风险预览不接入别名（脱盐乳清仍按未登记处理）
from app.supplier_risk import analyze_supplier_risk
risk = analyze_supplier_risk(text="脱盐乳清", plans_text="", declared=[])
assert "脱盐乳清" in risk["unknown_ingredients"]
print("8. 原料别名（原子校验/最短环路/链式解析/原名展示/时效复核边界/不接入风险预览） OK")

# 9. 共线生产换线排程（临时预览，不改写审查记录）
from app.changeover import (ChangeoverError, build_changeover_preview,
                            build_schedule, list_changeover_products)

# 9a. 候选产品清单：rid_alias 处于需复核状态，禁止参与并给出原因；
#     两版记录取标签核对对象（新版配方）的最终过敏原集合
products = list_changeover_products()
by_id = {p["id"]: p for p in products}
assert by_id[rid_alias]["eligible"] is False
assert "需复核" in by_id[rid_alias]["blocked_reason"]
assert by_id[rid]["eligible"] and by_id[old_rid]["eligible"]
assert by_id[rid]["source_version"] == "新版配方"
assert set(by_id[rid]["allergens"]) == {"麸质谷物", "坚果", "蛋类", "芝麻", "大豆"}

# 9b. 正常排程：段数 = 产品数 - 1，总耗时 = 各段之和；
#     两条记录新版配方过敏原集合相同 → 两方向成本均为 0，并列按 ID 升序
before = db.get_review(rid)
sched = build_changeover_preview([old_rid, rid], {"麸质谷物": 10, "坚果": 20})
assert len(sched["segments"]) == 1
assert sched["total_minutes"] == sum(s["minutes"] for s in sched["segments"])
assert sched["total_minutes"] == sched["segments"][-1]["cumulative_minutes"]
assert sched["order"] == sorted([old_rid, rid]) and sched["total_minutes"] == 0
assert sched["optimal_count"] == 2

# 9c. 缺失 / 需复核记录禁止参与并指出原因
try:
    build_changeover_preview([rid, rid_alias], {})
    raise AssertionError("需复核记录应被拒绝")
except ChangeoverError as exc:
    assert exc.problems and exc.problems[0]["reason"] == "stale"
    assert "需复核" in exc.problems[0]["message"]
try:
    build_changeover_preview([rid, 999999], {})
    raise AssertionError("缺失记录应被拒绝")
except ChangeoverError as exc:
    assert exc.problems[0]["reason"] == "missing"

# 9d. 子集 DP（纯计算）：成本有向、并列按产品 ID 序列取最小
prods = [
    {"id": 1, "name": "甲", "allergens": ["乳", "大豆"]},
    {"id": 2, "name": "乙", "allergens": ["乳"]},
    {"id": 3, "name": "丙", "allergens": []},
]
r = build_schedule(prods, {"乳": 5, "大豆": 10})
assert r["order"] == [3, 2, 1] and r["total_minutes"] == 0
assert r["optimal_count"] == 1
same = [{"id": i, "name": str(i), "allergens": ["乳"]} for i in (7, 5, 9)]
r = build_schedule(same, {"乳": 5})
assert r["order"] == [5, 7, 9] and r["optimal_count"] == 6

# 9e. 只读：记录内容与时效状态不变，不产生新记录
assert db.get_review(rid) == before
assert db.get_review(rid_alias)["needs_recheck"] is True
assert len(db.list_reviews()) == 3
print("9. 共线生产换线排程（候选资格/有向成本/子集DP/并列/禁止参与/只读） OK")

# 10. 共用标签模板分组（临时预览，不改写审查记录）
from app.template_group import (TemplateGroupError, build_grouping,
                                build_template_group_preview,
                                list_template_products)

# 10a. 候选清单：rid_alias 需复核禁止参与；两版记录取新版配方最终过敏原集合
tpl = {p["id"]: p for p in list_template_products()}
assert tpl[rid_alias]["eligible"] is False
assert "需复核" in tpl[rid_alias]["blocked_reason"]
assert tpl[rid]["eligible"] and tpl[old_rid]["eligible"]
assert tpl[rid]["source_version"] == "新版配方"
assert set(tpl[rid]["allergens"]) == {"麸质谷物", "坚果", "蛋类", "芝麻", "大豆"}

# 10b. 两条记录当前配方过敏原集合相同、上限均按 0 计 → 合并为 1 张模板
before = db.get_review(rid)
grp = build_template_group_preview([old_rid, rid], {})
assert grp["template_count"] == 1 and grp["optimal_count"] == 1
[g] = grp["groups"]
assert [m["id"] for m in g["members"]] == sorted([old_rid, rid])
assert g["template"] == tpl[rid]["allergens"]
assert all(m["extra"] == [] and m["extra_count"] == 0 for m in g["members"])

# 10c. 缺失 / 需复核记录禁止参与并逐条指出原因
try:
    build_template_group_preview([rid, rid_alias], {})
    raise AssertionError("需复核记录应被拒绝")
except TemplateGroupError as exc:
    assert exc.problems and exc.problems[0]["reason"] == "stale"
    assert "需复核" in exc.problems[0]["message"]
try:
    build_template_group_preview([rid, 999999], {})
    raise AssertionError("缺失记录应被拒绝")
except TemplateGroupError as exc:
    assert exc.problems[0]["reason"] == "missing"

# 10d. 子集 DP（纯计算）：模板 = 并集、附加声明超限不可成组、并列按 ID 序列
prods = [
    {"id": 1, "name": "甲", "allergens": ["乳"]},
    {"id": 2, "name": "乙", "allergens": ["乳", "大豆"]},
    {"id": 3, "name": "丙", "allergens": ["大豆"]},
]
r = build_grouping(prods, {1: 1, 2: 0, 3: 0})
assert r["template_count"] == 2 and r["optimal_count"] == 1
assert [[m["id"] for m in g["members"]] for g in r["groups"]] == [[1, 2], [3]]
assert r["groups"][0]["template"] == ["乳", "大豆"]
assert r["groups"][0]["members"][0]["extra"] == ["大豆"]
# 2×2 结构：{{1,2},{3,4}} 与 {{1,3},{2,4}} 并列，按组间 ID 序列取前者
grid = [
    {"id": 1, "name": "P1", "allergens": ["乳", "芝麻"]},
    {"id": 2, "name": "P2", "allergens": ["乳", "坚果"]},
    {"id": 3, "name": "P3", "allergens": ["大豆", "芝麻"]},
    {"id": 4, "name": "P4", "allergens": ["大豆", "坚果"]},
]
r = build_grouping(grid, {1: 1, 2: 1, 3: 1, 4: 1})
assert r["template_count"] == 2 and r["optimal_count"] == 2
assert [[m["id"] for m in g["members"]] for g in r["groups"]] == [[1, 2], [3, 4]]

# 10e. 只读：记录内容与时效状态不变，不产生新记录
assert db.get_review(rid) == before
assert db.get_review(rid_alias)["needs_recheck"] is True
assert len(db.list_reviews()) == 3
print("10. 共用标签模板分组（候选资格/并集模板/附加声明上限/子集DP/并列/禁止参与/只读） OK")

# 11. 目标过敏原最小改配方案（临时预览，不改写审查记录）
from app.reformulation import (ReformulationError, build_reformulation_plan,
                               get_reformulation_sources,
                               list_reformulation_records)

# 先恢复 8d 被改写成需复核的别名记录（脱盐乳清 -> 乳清粉），供乳来源场景使用
db.bulk_upsert_aliases(
    validate_alias_batch("脱盐乳清：乳清粉", db.alias_existing())["entries"])
assert db.get_review(rid_alias)["needs_recheck"] is False
# 另建一条带「自身标注 + 子配料传播」三层乳来源的时效正常记录
milk_result, milk_fp = analyze(
    text_a="配料表：巧克力酱（可可脂，奶粉（乳清粉，乳糖））",
    text_b=None, label_a="现版配方", label_b="新版配方", declared=[])
milk_text = "配料表：巧克力酱（可可脂，奶粉（乳清粉，乳糖））"
milk_rid = db.create_review(
    name="乳改配", declared=[], text_a=milk_text, text_b=None,
    label_a="现版配方", label_b="新版配方",
    result=milk_result, dict_fingerprint=milk_fp)

# 11a. 候选记录清单：复用当前配方（两版取 B）；rid 的 B 版无乳
recs = {r["id"]: r for r in list_reformulation_records()}
assert recs[rid]["eligible"] and recs[old_rid]["eligible"]
assert recs[rid]["source_version"] == "新版配方"
assert "坚果" in recs[rid]["allergens"] and "乳" not in recs[rid]["allergens"]
assert recs[milk_rid]["eligible"]

# 11b. 乳来源为 巧克力酱›奶粉 及其两个叶子（自身标注 + 子配料传播各计一条）
src = get_reformulation_sources(milk_rid, "乳")
assert src["sources"] == [
    ["巧克力酱", "奶粉"],
    ["巧克力酱", "奶粉", "乳清粉"],
    ["巧克力酱", "奶粉", "乳糖"],
]
candidate_keys = {tuple(n["path"]) for n in src["candidate_nodes"]}
assert ("巧克力酱",) in candidate_keys and ("巧克力酱", "奶粉") in candidate_keys

# 11c. 树形 DP：选祖先奶粉（100）阻断全部 3 条，优于两叶子各 60（120）
before = db.get_review(milk_rid)
plan = build_reformulation_plan(milk_rid, "乳", [
    {"path": ["巧克力酱", "奶粉"], "cost": 100},
    {"path": ["巧克力酱", "奶粉", "乳清粉"], "cost": 60},
    {"path": ["巧克力酱", "奶粉", "乳糖"], "cost": 60},
])
assert plan["feasible"] and plan["total_cost"] == 100
assert [s["path"] for s in plan["selections"]] == [["巧克力酱", "奶粉"]]
assert plan["selections"][0]["blocked_count"] == 3

# 11d. 奶粉自身是来源：只填叶子成本无法阻断「巧克力酱›奶粉」→ 无解
plan = build_reformulation_plan(milk_rid, "乳", [
    {"path": ["巧克力酱", "奶粉", "乳清粉"], "cost": 1},
    {"path": ["巧克力酱", "奶粉", "乳糖"], "cost": 1},
])
assert not plan["feasible"]
assert plan["uncoverable_sources"] == [["巧克力酱", "奶粉"]]

# 11e. 并列：顶层祖先巧克力酱 120 与 奶粉 120 同价（均阻断全部 3 条），
#      字典序取更靠上的巧克力酱；只替换叶子因奶粉自身是来源而不可行
plan = build_reformulation_plan(milk_rid, "乳", [
    {"path": ["巧克力酱"], "cost": 120},
    {"path": ["巧克力酱", "奶粉"], "cost": 120},
    {"path": ["巧克力酱", "奶粉", "乳清粉"], "cost": 1},
    {"path": ["巧克力酱", "奶粉", "乳糖"], "cost": 1},
])
assert plan["feasible"] and plan["total_cost"] == 120 and plan["optimal_count"] == 2
assert [s["path"] for s in plan["selections"]] == [["巧克力酱"]]

# 11f. 精确有理数：用字符串小数传入 0.3 与 0.3 判并列（Fraction(str) 精确化）
plan = build_reformulation_plan(milk_rid, "乳", [
    {"path": ["巧克力酱"], "cost": "0.3"},
    {"path": ["巧克力酱", "奶粉"], "cost": "0.3"},
])
assert plan["feasible"] and plan["total_cost"] == 0.3 and plan["optimal_count"] == 2

# 11g. 需复核 / 缺失 / 非法过敏原 / 非法路径均拒绝
db.upsert_entry("椰子粉", ["坚果", "大豆", "芝麻"])  # 制造 stale（rid/old_rid 涉及椰子粉）
try:
    get_reformulation_sources(rid, "坚果")
    raise AssertionError("需复核记录应被拒绝")
except ReformulationError as exc:
    assert exc.problems[0]["reason"] == "stale"
try:
    build_reformulation_plan(999999, "乳", [])
    raise AssertionError("缺失记录应被拒绝")
except ReformulationError as exc:
    assert exc.problems[0]["reason"] == "missing"
try:
    get_reformulation_sources(milk_rid, "不存在的过敏原")
    raise AssertionError("非配方过敏原应被拒绝")
except ReformulationError:
    pass
try:
    build_reformulation_plan(milk_rid, "乳",
                             [{"path": ["巧克力酱", "可可脂"], "cost": 1}])
    raise AssertionError("非来源路径应被拒绝")
except ReformulationError:
    pass

# 11h. 只读：记录内容、指纹与记录数均不变
assert db.get_review(milk_rid) == before
assert len(db.list_reviews()) == 4
print("11. 目标过敏原最小改配方案（当前配方/来源路径/树形DP/祖先阻断/并列/无解/禁止选择/只读） OK")

# 12. 过敏原检测套餐规划（临时预览，不改写审查记录）
from app.allergen_panel import (PanelError, build_panel_preview,
                                list_panel_records)

# 恢复 11g 被改写的椰子粉映射（仅影响新建记录；rid 的指纹仍是 6d 的口径，保持需复核）
db.upsert_entry("椰子粉", ["坚果"])
# 为本节新建一条时效正常的两版记录，当前配方（B）含 麸质谷物/坚果/蛋类/芝麻
panel_result, panel_fp = analyze(
    text_a=TEXT_A, text_b=TEXT_B, label_a="现版配方", label_b="新版配方",
    declared=[])
panel_rid = db.create_review(
    name="套餐规划", declared=DECLARED, text_a=TEXT_A, text_b=TEXT_B,
    label_a="现版配方", label_b="新版配方",
    result=panel_result, dict_fingerprint=panel_fp)

# 12a. 候选清单：两版记录取新版配方最终过敏原；需复核记录禁止选择
panel_recs = {r["id"]: r for r in list_panel_records()}
assert panel_recs[panel_rid]["source_version"] == "新版配方"
assert "坚果" in panel_recs[panel_rid]["allergens"]
assert "乳" not in panel_recs[panel_rid]["allergens"]
assert panel_recs[milk_rid]["eligible"]

# 12b. 最低总价组合 + 覆盖贡献 / 累计覆盖；额外过敏原原样展示、空包不参与
before_panel = db.get_review(panel_rid)
before_milk = db.get_review(milk_rid)
packages_text = """\
基础套餐：9.90：麸质谷物，蛋类
坚果套餐：10.00：坚果
芝麻加购：5.00：芝麻
全包套餐：30.00：麸质谷物，坚果，蛋类，芝麻，乳
鱼类单项：8.00：鱼类
空套餐：0：
"""
plan = build_panel_preview(panel_rid, packages_text)
assert plan["required_allergens"] == ["坚果", "芝麻", "蛋类", "麸质谷物"]
assert plan["feasible"] and plan["total_price"] == 24.9
# 结果按套餐名序列（字典序）展示：坚果套餐 < 基础套餐 < 芝麻加购
assert [s["name"] for s in plan["selections"]] == [
    "坚果套餐", "基础套餐", "芝麻加购"]
assert plan["selections"][0]["contribution"] == ["坚果"]
assert plan["selections"][1]["contribution"] == ["蛋类", "麸质谷物"]
assert plan["selections"][-1]["cumulative_covered"] == \
    ["坚果", "芝麻", "蛋类", "麸质谷物"]
assert [p["name"] for p in plan["unused_packages"]] == ["空套餐", "鱼类单项"]
assert plan["selections"][1]["extra_allergens"] == []
assert plan["optimal_count"] == 1

# 12c. 精确小数并列：0.1+0.2 与 0.3 同价，按套餐名序列字典序取最小者（全包 码位更靠前）
tie_packages = """\
单项A：0.1：坚果，芝麻
单项B：0.2：蛋类，麸质谷物
全包：0.3：坚果，芝麻，蛋类，麸质谷物
"""
plan = build_panel_preview(panel_rid, tie_packages)
assert plan["feasible"] and plan["total_price"] == 0.3 and plan["optimal_count"] == 2
assert [s["name"] for s in plan["selections"]] == ["全包"]

# 12c2. 0 元冗余套餐计入并列：0元加赠（0 元，只覆盖芝麻/坚果，后被全包
# 覆盖）不新增覆盖也作为不同最低价组合；正价冗余套餐（加价购）不得进入最低价
zero_tie = """\
全包：24.9：麸质谷物，坚果，蛋类，芝麻
0元加赠：0：芝麻，坚果
加价购：5：坚果
"""
plan = build_panel_preview(panel_rid, zero_tie)
assert plan["feasible"] and plan["total_price"] == 24.9
assert plan["optimal_count"] == 2
# ('全包',) 与 ('0元加赠','全包') 并列；'0' 码位最小，展示含 0元加赠者
assert [s["name"] for s in plan["selections"]] == ["0元加赠", "全包"]
rows = {s["name"]: s for s in plan["selections"]}
assert rows["0元加赠"]["contribution"] == ["坚果", "芝麻"]
assert rows["全包"]["contribution"] == ["蛋类", "麸质谷物"]
assert rows["全包"]["cumulative_covered"] == ["坚果", "芝麻", "蛋类", "麸质谷物"]
assert [p["name"] for p in plan["unused_packages"]] == []

# 12d. 无法覆盖时列出缺口
plan = build_panel_preview(panel_rid, "A：1：坚果\nB：2：麸质谷物")
assert not plan["feasible"] and plan["uncovered"] == ["芝麻", "蛋类"]

# 12e. 套餐行整批拒绝：格式 / 价格 / 重名逐行指出
try:
    build_panel_preview(panel_rid, "A：1：坚果\nA：2：蛋类\n坏价：x：芝麻")
    raise AssertionError("非法套餐行应被拒绝")
except PanelError as exc:
    assert {p["type"] for p in exc.problems} == {"duplicate_name", "bad_price"}

# 12f. 缺失 / 需复核记录拒绝并说明（rid 的指纹仍是含「大豆」口径，保持需复核）
assert db.get_review(rid)["needs_recheck"]
try:
    build_panel_preview(rid, "A：1：坚果")
    raise AssertionError("需复核记录应被拒绝")
except PanelError as exc:
    assert exc.problems[0]["reason"] == "stale" and "需复核" in exc.problems[0]["message"]
try:
    build_panel_preview(999999, "A：1：坚果")
    raise AssertionError("缺失记录应被拒绝")
except PanelError as exc:
    assert exc.problems[0]["reason"] == "missing"

# 12g. 无过敏原配方：空组合 0 元覆盖
empty_result, empty_fp = analyze(
    text_a="配料表：可可脂，食盐", text_b=None,
    label_a="现版配方", label_b="新版配方", declared=[])
empty_rid = db.create_review(
    name="无过敏原", declared=[], text_a="配料表：可可脂，食盐", text_b=None,
    label_a="现版配方", label_b="新版配方",
    result=empty_result, dict_fingerprint=empty_fp)
plan = build_panel_preview(empty_rid, "A：1：乳")
assert plan["feasible"] and plan["total_price"] == 0 and plan["selections"] == []

# 12h. 只读：不新增 / 不改写既有记录（empty_rid 为本段新建的第 6 条）
assert len(db.list_reviews()) == 6
assert db.get_review(panel_rid) == before_panel
assert db.get_review(milk_rid) == before_milk
print("12. 过敏原检测套餐规划（当前配方必须项/精确小数/子集DP/字典序并列/缺口/禁止选择/只读） OK")

print("\n全部集成冒烟测试通过 ✔")
