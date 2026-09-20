"""供应商方案风险区间（临时预览）测试。

覆盖：
- 方案文本解析（分号分隔互斥方案、逗号分隔过敏原、空方案「无」、
  同名重复报错、完全相同方案去重）；
- 不枚举方案组合的必然（交集）/可能（并集）传播，含嵌套树与同名多分支；
- 确定遗漏 / 潜在遗漏 / 确定多报 / 潜在多报 / 一致五类划分，
  以及两类遗漏各自的来源路径（自身必然标注 vs 自身仅可能标注）；
- 单一方案（含无方案）时结果与现有审查口径完全一致；
- 空方案叶子不再算作未识别；
- 预览只读：不产生任何审查记录。
"""
import os
import tempfile
import unittest

_DB_DIR = tempfile.mkdtemp(prefix="allergen-supplier-")
os.environ["DB_PATH"] = os.path.join(_DB_DIR, "supplier.db")

from app import db  # noqa: E402
from app.analysis import normalize_name  # noqa: E402
from app.review import analyze  # noqa: E402
from app.supplier_risk import (SupplierPlanError, analyze_supplier_risk,  # noqa: E402
                              parse_supplier_plans)

DICT_TEXT = """\
小麦粉：麸质谷物
乳清粉：乳
乳糖：乳
奶粉：乳
可可脂：
大豆卵磷脂：大豆
鸡蛋：蛋类
椰子粉：坚果
食盐：
"""
TEXT = ("配料表：小麦粉，巧克力酱（可可脂，奶粉（乳清粉，乳糖），大豆卵磷脂），"
        "鸡蛋，食盐")
DECLARED = ["麸质谷物", "蛋类", "鱼"]


class ParsePlansTest(unittest.TestCase):
    def test_multiple_mutex_options(self):
        plans = parse_supplier_plans("奶粉：乳；无\n可可脂：无；坚果")
        self.assertEqual([sorted(o) for o in plans[normalize_name("奶粉")].options],
                         [["乳"], []])  # 保留输入顺序
        self.assertEqual([sorted(o) for o in plans[normalize_name("可可脂")].options],
                         [[], ["坚果"]])

    def test_separators_and_empty_plan(self):
        plans = parse_supplier_plans("奶粉: 乳,大豆；无；坚果、鱼")
        opts = plans[normalize_name("奶粉")].options
        self.assertEqual([sorted(o) for o in opts],
                         [["乳", "大豆"], [], ["坚果", "鱼"]])

    def test_identical_options_deduped(self):
        plans = parse_supplier_plans("奶粉：乳，大豆；大豆，乳；乳，大豆")
        self.assertEqual(len(plans[normalize_name("奶粉")].options), 1)

    def test_comments_and_blank_lines_ignored(self):
        plans = parse_supplier_plans("# 注释\n\n奶粉：乳；无\n")
        self.assertEqual(set(plans), {normalize_name("奶粉")})

    def test_errors(self):
        for bad in ("奶粉", "：乳", "奶粉：", "奶粉：无，乳",
                    "奶粉：乳\n奶粉：无", "奶粉：；"):
            with self.assertRaises(SupplierPlanError):
                parse_supplier_plans(bad)
        # 行尾多余分号容错：不算缺失方案
        self.assertIn(normalize_name("奶粉"), parse_supplier_plans("奶粉：乳；"))


class RiskPropagationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.DB_PATH = os.path.join(_DB_DIR, "propagate.db")
        db.init_db()
        db.bulk_import(DICT_TEXT)

    def _node(self, r, *path):
        nodes = r["tree"]
        node = None
        for name in path:
            node = next(n for n in nodes if n["name"] == name)
            nodes = node["children"]
        return node

    def test_intersection_union_at_leaf_and_root(self):
        r = analyze_supplier_risk(
            text=TEXT, plans_text="可可脂：无；坚果\n奶粉：乳；无",
            declared=DECLARED)
        cocoa = self._node(r, "巧克力酱", "可可脂")
        self.assertEqual(cocoa["own_necessary"], [])
        self.assertEqual(cocoa["own_possible"], ["坚果"])
        self.assertTrue(cocoa["overridden"])
        # 奶粉：方案交集为空、并集为乳；但乳清粉/乳糖（字典）必然携带乳
        choco = self._node(r, "巧克力酱")
        self.assertEqual(sorted(choco["total_necessary"]), ["乳", "大豆"])
        self.assertEqual(sorted(choco["total_possible"]), ["乳", "坚果", "大豆"])
        self.assertEqual(r["allergens_necessary"], ["乳", "大豆", "蛋类", "麸质谷物"])
        self.assertEqual(r["allergens_possible"],
                         ["乳", "坚果", "大豆", "蛋类", "麸质谷物"])

    def test_no_cartesian_enumeration_linear_in_option_count(self):
        # 三个原料各两个方案：笛卡尔积为 8 种组合，但节点区间只由
        # 「同原料各方案交集/并集 + 树传播」决定，直接给出五个分类
        r = analyze_supplier_risk(
            text="乳清粉，大豆卵磷脂，椰子粉",
            plans_text="乳清粉：乳；无\n大豆卵磷脂：大豆；无\n椰子粉：坚果；无",
            declared=[])
        self.assertEqual(r["allergens_necessary"], [])
        self.assertEqual(r["allergens_possible"], ["乳", "坚果", "大豆"])
        c = r["label_check"]
        self.assertEqual(c["certain_missing"], [])
        self.assertEqual(sorted(c["potential_missing"]), ["乳", "坚果", "大豆"])

    def test_possible_child_becomes_certain_via_other_branch(self):
        # 奶粉方案让乳仅可能，但同分支下乳清粉必然含乳 -> 乳必然
        r = analyze_supplier_risk(
            text="酱（奶粉，乳清粉）", plans_text="奶粉：乳；无", declared=[])
        sauce = self._node(r, "酱")
        self.assertIn("乳", sauce["total_necessary"])

    def test_same_named_ingredient_paths_kept_apart(self):
        # 两个分支下同名方案原料的潜在路径分别保留，不与必然来源合并
        r = analyze_supplier_risk(
            text="巧克力酱（奶粉），夹心酱（奶粉）",
            plans_text="奶粉：乳；无", declared=[])
        self.assertEqual(
            r["label_check"]["potential_missing_sources"]["乳"],
            [["夹心酱", "奶粉"], ["巧克力酱", "奶粉"]])
        # 再出现一个必然携带乳的乳清粉：乳升格为确定遗漏，潜在遗漏消失；
        # 确定来源只列自身必然标注的乳清粉，方案奶粉自身是交集为空的节点
        r2 = analyze_supplier_risk(
            text="巧克力酱（奶粉），夹心酱（奶粉），乳清粉",
            plans_text="奶粉：乳；无", declared=[])
        self.assertNotIn("乳", r2["label_check"]["potential_missing"])
        self.assertIn("乳", r2["label_check"]["certain_missing"])
        self.assertEqual(
            r2["label_check"]["certain_missing_sources"]["乳"],
            [["乳清粉"]])

    def test_empty_plan_leaf_not_unknown_and_removes_necessity(self):
        # 供应商明确承诺「无」：覆盖字典、不标记未识别，并移除必然过敏原
        r = analyze_supplier_risk(
            text="酱（乳清粉）", plans_text="乳清粉：无", declared=[])
        self.assertEqual(r["unknown_ingredients"], [])
        self.assertEqual(r["allergens_necessary"], [])
        self.assertEqual(r["allergens_possible"], [])
        leaf = self._node(r, "酱", "乳清粉")
        self.assertTrue(leaf["overridden"] and leaf["plan_count"] == 1)

    def test_unknown_leaf_still_flagged(self):
        r = analyze_supplier_risk(text="神秘香精，鸡蛋", plans_text="", declared=[])
        self.assertEqual(r["unknown_ingredients"], ["神秘香精"])


class RiskClassificationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.DB_PATH = os.path.join(_DB_DIR, "classify.db")
        db.init_db()
        db.bulk_import(DICT_TEXT)

    def test_five_categories_and_sources(self):
        r = analyze_supplier_risk(
            text=TEXT,
            plans_text="可可脂：无；坚果\n奶粉：乳；无",
            declared=["麸质谷物", "蛋类", "鱼"])
        c = r["label_check"]
        # 乳清粉/乳糖保证乳必然、大豆卵磷脂保证大豆必然 -> 确定遗漏
        self.assertEqual(c["certain_missing"], ["乳", "大豆"])
        self.assertEqual(c["certain_missing_sources"]["乳"], [
            ["巧克力酱", "奶粉", "乳清粉"],
            ["巧克力酱", "奶粉", "乳糖"],
        ])
        self.assertEqual(c["certain_missing_sources"]["大豆"],
                         [["巧克力酱", "大豆卵磷脂"]])
        # 坚果只在可可脂方案 2 出现 -> 潜在遗漏，路径到自身仅可能标注的可可脂
        self.assertEqual(c["potential_missing"], ["坚果"])
        self.assertEqual(c["potential_missing_sources"]["坚果"],
                         [["巧克力酱", "可可脂"]])
        # 鱼任何方案都不可能含有 -> 确定多报
        self.assertEqual(c["certain_over_reported"], ["鱼"])
        self.assertEqual(c["potential_over_reported"], [])
        self.assertEqual(c["matched"], ["蛋类", "麸质谷物"])
        self.assertFalse(r["single_plan"])

    def test_potential_over_reported(self):
        # 声明坚果：仅可可脂方案 2 引入 -> 潜在多报而非确定多报
        r = analyze_supplier_risk(
            text=TEXT, plans_text="可可脂：无；坚果",
            declared=["麸质谷物", "乳", "大豆", "蛋类", "坚果"])
        c = r["label_check"]
        self.assertEqual(c["certain_over_reported"], [])
        self.assertEqual(c["potential_over_reported"], ["坚果"])
        self.assertEqual(c["potential_missing"], [])
        self.assertEqual(c["certain_missing"], [])

    def test_declared_possible_only_allergen_not_missing(self):
        # 潜在过敏原若已声明，既不是遗漏也不是一致（不是必然）
        r = analyze_supplier_risk(
            text=TEXT, plans_text="可可脂：无；坚果",
            declared=["坚果"])
        c = r["label_check"]
        self.assertEqual(c["potential_missing"], [])
        self.assertEqual(c["matched"], [])


class SinglePlanConsistencyTest(unittest.TestCase):
    """单一方案时风险预览必须与现有审查口径一致（单方案覆盖字典）。"""

    @classmethod
    def setUpClass(cls):
        db.DB_PATH = os.path.join(_DB_DIR, "single.db")
        db.init_db()
        db.bulk_import(DICT_TEXT)

    def _assert_equivalent_to_review(self, plans_text, declared,
                                     dictionary_mutator=None):
        """方案均为单方案时，风险五类结果与「用方案替换字典后」的正式审查一致。"""
        r = analyze_supplier_risk(
            text=TEXT, plans_text=plans_text, declared=declared)
        self.assertTrue(r["single_plan"])
        c = r["label_check"]
        # 用方案覆盖当前字典后走现有审查流程
        plans = parse_supplier_plans(plans_text)
        dictionary = db.dict_lookup()
        for key, spec in plans.items():
            dictionary[key] = set(spec.options[0])
        result, _ = analyze(
            text_a=TEXT, text_b=None, label_a="现版", label_b="新版",
            declared=sorted(set(declared)), dictionary=dictionary)
        lc = result["label_check"]
        self.assertEqual(c["certain_missing"], lc["missing"])
        self.assertEqual(c["certain_missing_sources"], lc["missing_sources"])
        self.assertEqual(c["certain_over_reported"], lc["over_reported"])
        self.assertEqual(c["matched"], lc["matched"])
        self.assertEqual(c["potential_missing"], [])
        self.assertEqual(c["potential_over_reported"], [])
        self.assertEqual(r["allergens_necessary"],
                         result["version_a"]["allergens"])
        self.assertEqual(r["allergens_possible"],
                         result["version_a"]["allergens"])

    def test_single_plan_equal_to_dict(self):
        self._assert_equivalent_to_review(
            "奶粉：乳\n可可脂：坚果", ["麸质谷物", "蛋类", "鱼"])

    def test_single_plan_overrides_dict(self):
        self._assert_equivalent_to_review(
            "奶粉：无\n可可脂：坚果\n大豆卵磷脂：大豆，鱼",
            ["麸质谷物", "蛋类"])

    def test_no_plans_equivalent_to_review(self):
        self._assert_equivalent_to_review("", ["麸质谷物", "蛋类", "鱼"])

    def test_multi_plan_flag_false_only_when_branching(self):
        # 未在配方中出现的多方案原料不应影响 single_plan 判定
        r = analyze_supplier_risk(
            text=TEXT, plans_text="奶粉：乳\n神秘香精：乳；无",
            declared=DECLARED)
        self.assertTrue(r["single_plan"])
        self.assertEqual(r["unused_plan_ingredients"], ["神秘香精"])


class PreviewIsReadonlyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.DB_PATH = os.path.join(_DB_DIR, "readonly.db")
        db.init_db()
        db.bulk_import(DICT_TEXT)

    def test_preview_creates_no_review_or_dict_entry(self):
        before = len(db.list_reviews())
        dict_before = {e["ingredient"] for e in db.list_dict()}
        analyze_supplier_risk(
            text=TEXT, plans_text="神秘新原料：乳；无\n可可脂：无；坚果",
            declared=DECLARED)
        self.assertEqual(len(db.list_reviews()), before)
        self.assertEqual({e["ingredient"] for e in db.list_dict()}, dict_before)


if __name__ == "__main__":
    unittest.main()
