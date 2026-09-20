"""配料占比区间顺序校验（临时预览）测试。

覆盖：
- 占比标注解析（[最小-最大%]、单值 [值%]、嵌套父配料标注、中文括号、前缀去除）；
- 缺少区间 / 格式错误 / 负值 / 下界大于上界四类问题定位到具体嵌套路径；
- 区间传播可行性判定与精确收紧（前向收上界、后向收下界）、单值锁定标记；
- 不可行时最短冲突片段（跨度最小、并列取最左）与完整嵌套路径；
- 预览只读：不产生任何审查记录。
"""
import os
import tempfile
import unittest

_DB_DIR = tempfile.mkdtemp(prefix="allergen-proportion-")
os.environ["DB_PATH"] = os.path.join(_DB_DIR, "proportion.db")

from app import db  # noqa: E402
from app.proportion import analyze_proportions  # noqa: E402


def _group(result, *path):
    return next(g for g in result["groups"] if tuple(g["path"]) == tuple(path))


class AnnotationParseTest(unittest.TestCase):
    def test_single_value_and_range(self):
        r = analyze_proportions("小麦粉[40-55%]，食盐[2%]")
        self.assertEqual(r["status"], "feasible")
        items = _group(r)["items"]
        self.assertEqual(items[0]["original"], [40.0, 55.0])
        self.assertEqual(items[1]["original"], [2.0, 2.0])
        self.assertTrue(items[1]["pinned"])  # 单值标注天然锁定

    def test_nested_parent_annotation_and_prefix(self):
        r = analyze_proportions(
            "配料表：巧克力酱[25-60%]（可可脂[10-18%]，奶粉[12-15%]），鸡蛋[5-20%]")
        self.assertEqual(r["status"], "feasible")
        self.assertEqual(r["group_count"], 2)
        self.assertEqual(r["item_count"], 4)
        child = _group(r, "巧克力酱")
        self.assertEqual([i["name"] for i in child["items"]], ["可可脂", "奶粉"])

    def test_decimal_and_whitespace_tolerated(self):
        r = analyze_proportions("小麦粉[40.5-55%]，食盐[ 1.5 %]")
        self.assertEqual(r["status"], "feasible")
        items = _group(r)["items"]
        self.assertEqual(items[0]["original"], [40.5, 55.0])
        self.assertEqual(items[1]["original"], [1.5, 1.5])

    def test_empty_recipe_is_error(self):
        r = analyze_proportions("配料表：")
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["errors"][0]["reason"], "empty")


class AnnotationErrorTest(unittest.TestCase):
    def test_missing_interval_located_with_path(self):
        r = analyze_proportions("巧克力酱[25-60%]（可可脂，奶粉[12-15%]），鸡蛋")
        self.assertEqual(r["status"], "error")
        by_path = {tuple(e["path"]): e["reason"] for e in r["errors"]}
        self.assertEqual(len(r["errors"]), 2)
        self.assertEqual(by_path[("巧克力酱", "可可脂")], "missing")
        self.assertEqual(by_path[("鸡蛋",)], "missing")

    def test_bad_format_negative_inverted(self):
        r = analyze_proportions(
            "甲[abc%]，乙[10]（丙[-5%]），丁[8-3%]，戊[1%]尾巴")
        self.assertEqual(r["status"], "error")
        reasons = {tuple(e["path"]): e["reason"] for e in r["errors"]}
        self.assertEqual(reasons[("甲",)], "bad_format")    # 非数字
        self.assertEqual(reasons[("乙",)], "bad_format")    # 缺少 %
        self.assertEqual(reasons[("乙", "丙")], "negative")  # 嵌套路径定位
        self.assertEqual(reasons[("丁",)], "inverted")      # 下界大于上界
        self.assertEqual(reasons[("戊",)], "bad_format")    # 标注后还有文字

    def test_negative_bounds_in_range(self):
        r = analyze_proportions("甲[-5-10%]，乙[5--2%]")
        self.assertEqual([e["reason"] for e in r["errors"]],
                         ["negative", "negative"])

    def test_errors_block_constraint_check(self):
        # 存在标注错误时不做约束校验，不返回收紧区间或冲突
        r = analyze_proportions("甲[50-60%]，乙")
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["groups"], [])
        self.assertEqual(r["conflicts"], [])


class IntervalPropagationTest(unittest.TestCase):
    def test_forward_upper_tightening(self):
        # 后项上界不得超过前项上界：巧克力酱 hi 60 被小麦粉 55 收紧
        r = analyze_proportions("小麦粉[40-55%]，巧克力酱[25-60%]，食盐[1-2%]")
        items = _group(r)["items"]
        self.assertEqual(items[1]["tightened"], [25.0, 55.0])
        self.assertFalse(items[1]["pinned"])

    def test_backward_lower_tightening(self):
        # 前项下界不得低于后项下界：可可脂 lo 10 被奶粉 12 收紧
        r = analyze_proportions("酱[30-40%]（可可脂[10-18%]，奶粉[12-15%]）")
        child = _group(r, "酱")["items"]
        self.assertEqual(child[0]["tightened"], [12.0, 18.0])
        self.assertEqual(child[1]["tightened"], [12.0, 15.0])

    def test_pinned_by_propagation(self):
        # 鸡蛋 [5-20] 与食盐 [20-30] 互相锁定为 20
        r = analyze_proportions("鸡蛋[5-20%]，食盐[20-30%]")
        items = _group(r)["items"]
        self.assertEqual(items[0]["tightened"], [20.0, 20.0])
        self.assertEqual(items[1]["tightened"], [20.0, 20.0])
        self.assertTrue(all(i["pinned"] for i in items))

    def test_tightened_interval_is_exact_projection(self):
        r = analyze_proportions("甲[10-50%]，乙[20-40%]，丙[15-45%]")
        items = _group(r)["items"]
        self.assertEqual(items[0]["tightened"], [20.0, 50.0])  # lo 被乙抬到 20
        self.assertEqual(items[1]["tightened"], [20.0, 40.0])
        self.assertEqual(items[2]["tightened"], [15.0, 40.0])  # hi 被乙压到 40

    def test_single_item_group_trivially_feasible(self):
        r = analyze_proportions("酱[30-40%]（奶粉[12-15%]）")
        self.assertEqual(r["status"], "feasible")
        child = _group(r, "酱")["items"]
        self.assertEqual(child[0]["tightened"], [12.0, 15.0])
        self.assertFalse(child[0]["pinned"])


class ConflictTest(unittest.TestCase):
    def test_shortest_fragment_prefers_inner_pair(self):
        # (甲,乙) 跨度 1 与 (甲,丙) 跨度 2 同时存在时，取最短的 (甲,乙)
        r = analyze_proportions("甲[10-12%]，乙[20-25%]，丙[15-30%]")
        self.assertEqual(r["status"], "infeasible")
        [c] = r["conflicts"]
        self.assertEqual([f["name"] for f in c["fragment"]], ["甲", "乙"])
        self.assertEqual(c["path"], [])

    def test_fragment_can_be_interior(self):
        # 冲突对在组中间：片段只含 乙、丙
        r = analyze_proportions("甲[30-40%]，乙[10-12%]，丙[20-25%]")
        [c] = r["conflicts"]
        self.assertEqual([f["name"] for f in c["fragment"]], ["乙", "丙"])

    def test_fragment_includes_innocent_middle_item(self):
        # 见证对 (甲,丙) 不相邻时，片段保留中间的乙
        r = analyze_proportions("甲[10-12%]，乙[1-50%]，丙[15-20%]")
        [c] = r["conflicts"]
        self.assertEqual([f["name"] for f in c["fragment"]], ["甲", "乙", "丙"])
        self.assertIn("15%", c["message"])
        self.assertIn("12%", c["message"])

    def test_nested_group_conflict_path(self):
        r = analyze_proportions(
            "小麦粉[40-55%]，巧克力酱[25-35%]（可可脂[5-8%]，奶粉[12-15%]）")
        self.assertEqual(r["status"], "infeasible")
        [c] = r["conflicts"]
        self.assertEqual(c["path"], ["巧克力酱"])
        self.assertEqual([f["name"] for f in c["fragment"]], ["可可脂", "奶粉"])
        # 整单不可行时不返回收紧区间（即使顶层组本身可行）
        self.assertEqual(r["groups"], [])

    def test_multiple_groups_each_report_own_conflict(self):
        r = analyze_proportions(
            "甲[10-12%]，乙[20-25%]（丙[5-8%]，丁[12-15%]）")
        self.assertEqual(r["status"], "infeasible")
        self.assertEqual(len(r["conflicts"]), 2)
        paths = sorted(tuple(c["path"]) for c in r["conflicts"])
        self.assertEqual(paths, [(), ("乙",)])


class PreviewReadonlyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.DB_PATH = os.path.join(_DB_DIR, "readonly.db")
        db.init_db()

    def test_preview_creates_no_review_or_dict_entry(self):
        before = len(db.list_reviews())
        dict_before = {e["ingredient"] for e in db.list_dict()}
        analyze_proportions("小麦粉[40-55%]，食盐[2%]")
        analyze_proportions("小麦粉[40-55%]，食盐")  # 错误路径同样不落库
        self.assertEqual(len(db.list_reviews()), before)
        self.assertEqual({e["ingredient"] for e in db.list_dict()}, dict_before)


if __name__ == "__main__":
    unittest.main()
