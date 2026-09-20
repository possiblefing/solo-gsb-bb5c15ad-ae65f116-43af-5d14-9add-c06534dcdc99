"""字典时效复核测试：稳定指纹、失效判定（仅相关原料映射变化才失效）、
一键复核的前后差异与确认覆盖、旧记录无指纹的按需复核。

不依赖 FastAPI，直接走 db + review + recheck 全链路。
"""
import os
import tempfile
import unittest

_DB_DIR = tempfile.mkdtemp(prefix="allergen-recheck-")
os.environ["DB_PATH"] = os.path.join(_DB_DIR, "recheck.db")

from app import db  # noqa: E402
from app.analysis import normalize_name  # noqa: E402
from app.fingerprint import (recipe_keys, result_keys,  # noqa: E402
                             scoped_fingerprint)
from app.recheck import build_recheck  # noqa: E402
from app.review import analyze  # noqa: E402

DICT_TEXT = """\
小麦粉：麸质谷物
乳清粉：乳
乳糖：乳
奶粉：乳
可可脂：
大豆卵磷脂：大豆
鸡蛋：蛋类
食盐：
"""
TEXT_A = ("配料表：小麦粉，巧克力酱（可可脂，奶粉（乳清粉，乳糖），大豆卵磷脂），"
          "鸡蛋，食盐")
TEXT_B = "配料表：小麦粉，巧克力酱（可可脂，椰子粉），鸡蛋，芝麻酱（芝麻），食盐"
DECLARED = ["麸质谷物", "蛋类"]


def make_review(text_a=TEXT_A, text_b=None, declared=None, name="复核测试"):
    result, fp = analyze(
        text_a=text_a, text_b=text_b,
        label_a="现版配方", label_b="新版配方",
        declared=DECLARED if declared is None else declared)
    rid = db.create_review(
        name=name, declared=sorted(set(DECLARED if declared is None else declared)),
        text_a=text_a, text_b=text_b,
        label_a="现版配方", label_b="新版配方",
        result=result, dict_fingerprint=fp)
    return rid, result, fp


class RecheckFlowTest(unittest.TestCase):
    def setUp(self):
        # 每个用例使用独立数据库，互不污染
        db.DB_PATH = os.path.join(_DB_DIR, f"recheck_{self.id().rsplit('.', 1)[-1]}.db")
        db.init_db()
        db.bulk_import(DICT_TEXT)

    def _row_fp(self, rid):
        with db._connect() as conn:
            return conn.execute(
                "SELECT dict_fingerprint FROM reviews WHERE id=?", (rid,)
            ).fetchone()[0]

    def test_fresh_right_after_create(self):
        rid, _, _ = make_review()
        item = next(r for r in db.list_reviews() if r["id"] == rid)
        self.assertFalse(item["needs_recheck"])
        self.assertIsNone(item["recheck_reason"])
        detail = db.get_review(rid)
        self.assertFalse(detail["needs_recheck"])

    def test_unrelated_dict_changes_do_not_invalidate(self):
        # 1) 新增配方未涉及的原料；2) 删除无关原料；
        # 3) 同内容重复导入（updated_at 变化、列表顺序变化）
        rid, _, _ = make_review()
        db.upsert_entry("无关新原料", ["坚果"])
        self.assertFalse(db.get_review(rid)["needs_recheck"])
        db.delete_entry("无关新原料")
        self.assertFalse(db.get_review(rid)["needs_recheck"])
        # 以不同顺序、相同映射重新导入：指纹由排序后内容决定，不应失效
        db.bulk_import("\n".join(reversed(DICT_TEXT.strip().splitlines())))
        self.assertFalse(db.get_review(rid)["needs_recheck"])

    def test_reupsert_same_allergens_keeps_fresh(self):
        rid, _, _ = make_review()
        db.upsert_entry("乳清粉", ["乳"])       # 过敏原集合不变
        db.upsert_entry("可可脂", [])           # 无过敏原条目重写
        self.assertFalse(db.get_review(rid)["needs_recheck"])

    def test_relevant_mapping_change_invalidates(self):
        rid, _, _ = make_review()
        db.upsert_entry("乳清粉", ["乳", "大豆"])
        detail = db.get_review(rid)
        self.assertTrue(detail["needs_recheck"])
        self.assertEqual(detail["recheck_reason"], "stale")
        item = next(r for r in db.list_reviews() if r["id"] == rid)
        self.assertTrue(item["needs_recheck"])

    def test_leaf_registration_invalidates(self):
        # 为原本“未识别”的叶子原料（白砂糖）补登记，映射发生变化，应失效
        text = TEXT_A.replace("，鸡蛋，食盐", "，白砂糖，鸡蛋，食盐")
        rid, result, _ = make_review(text_a=text)
        self.assertIn(normalize_name("白砂糖"), result_keys(result))
        self.assertIn("白砂糖", result["version_a"]["unknown_ingredients"])
        db.upsert_entry("白砂糖", [])  # 已登记空集合：此前 absent，现在 present
        self.assertTrue(db.get_review(rid)["needs_recheck"])
        outcome = build_recheck(db.get_review(rid))
        # 未识别原料列表里“白砂糖”消失
        self.assertEqual(
            outcome["diff"]["versions"]["version_a"]
            ["unknown_ingredients"]["removed"], ["白砂糖"])
        db.delete_entry("白砂糖")      # 回到 absent，时效恢复正常
        self.assertFalse(db.get_review(rid)["needs_recheck"])

    def test_relevant_entry_removal_invalidates(self):
        rid, _, _ = make_review()
        db.delete_entry("乳清粉")  # 配方叶子原料，删除后退回未识别
        self.assertTrue(db.get_review(rid)["needs_recheck"])

    def test_recheck_preview_and_confirm(self):
        rid, old, old_fp = make_review()
        # 大豆卵磷脂额外标注“鱼”：产生新的遗漏与来源路径
        db.upsert_entry("大豆卵磷脂", ["大豆", "鱼"])
        outcome = build_recheck(db.get_review(rid))
        self.assertTrue(outcome["changed"])
        # “鱼”成为新增遗漏
        self.assertIn("鱼", outcome["diff"]["label"]["missing_delta"]["added"])
        self.assertEqual(
            outcome["diff"]["label"]["missing_delta"]["removed"], [])
        va_diff = outcome["diff"]["versions"]["version_a"]["allergens"]
        self.assertEqual(va_diff["added"], ["鱼"])
        # 仅预览不改动记录：指纹保持不变
        self.assertEqual(self._row_fp(rid), old_fp)

        # 确认复核：结果覆盖、指纹更新、状态恢复正常
        new_fp = outcome["fingerprint"]
        self.assertNotEqual(new_fp, old_fp)
        db.update_review_result(rid, outcome["new_result"], new_fp)
        detail = db.get_review(rid)
        self.assertFalse(detail["needs_recheck"])
        self.assertIn("鱼", detail["result"]["version_a"]["allergens"])
        self.assertEqual(self._row_fp(rid), new_fp)

    def test_recheck_preview_sources_path_change(self):
        # 只改变来源路径而不改变过敏原全集：为复合父配料“巧克力酱”
        # 自身标注“乳”，遗漏仍含“乳”，但多出终止于父节点的来源路径
        rid, _, _ = make_review()
        db.upsert_entry("巧克力酱", ["乳"])
        outcome = build_recheck(db.get_review(rid))
        changes = outcome["diff"]["label"]["path_changes"]
        milk = next((c for c in _filter_changes(changes, "乳")), None)
        self.assertIsNotNone(milk)
        self.assertIn(["巧克力酱"], milk["paths_added"])
        self.assertEqual(milk["paths_removed"], [])

    def test_recheck_no_change_still_refreshes_fingerprint(self):
        rid, _, _ = make_review()
        outcome = build_recheck(db.get_review(rid))
        self.assertFalse(outcome["changed"])
        # 指纹相同，确认后仍为时效正常
        db.update_review_result(rid, outcome["new_result"], outcome["fingerprint"])
        self.assertFalse(db.get_review(rid)["needs_recheck"])

    def test_legacy_record_without_fingerprint(self):
        # 模拟升级前旧记录：指纹列为 NULL
        result, fp = analyze(
            text_a=TEXT_A, text_b=None, label_a="现版配方",
            label_b="新版配方", declared=DECLARED)
        rid = db.create_review(
            name="旧记录", declared=sorted(DECLARED), text_a=TEXT_A, text_b=None,
            label_a="现版配方", label_b="新版配方", result=result,
            dict_fingerprint=None)
        item = next(r for r in db.list_reviews() if r["id"] == rid)
        self.assertTrue(item["needs_recheck"])
        self.assertEqual(item["recheck_reason"], "legacy")

        # 复核确认后补上指纹
        outcome = build_recheck(db.get_review(rid))
        db.update_review_result(rid, outcome["new_result"], outcome["fingerprint"])
        self.assertFalse(db.get_review(rid)["needs_recheck"])

    def test_two_version_fingerprint_covers_both_recipes(self):
        rid, result, _ = make_review(text_b=TEXT_B)
        keys = result_keys(result)
        self.assertIn(normalize_name("椰子粉"), keys)   # 仅 B 版出现
        self.assertIn(normalize_name("乳清粉"), keys)   # 仅 A 版出现
        # 改动 B 版独有原料映射 → 失效；此前 A-only 测试已覆盖反向
        db.upsert_entry("椰子粉", ["坚果", "大豆"])
        self.assertTrue(db.get_review(rid)["needs_recheck"])

    def test_label_declared_not_part_of_fingerprint(self):
        rid, _, _ = make_review(declared=["麸质谷物"])
        # 指纹只取决于配方涉及的原料映射，与标签声明无关：
        # 重新解析同样配方（声明变化由人工触发，不经字典）指纹应一致
        keys = recipe_keys(TEXT_A)
        fp2 = scoped_fingerprint(keys, db.dict_lookup())
        self.assertEqual(fp2, self._row_fp(rid))


def _filter_changes(changes, allergen):
    return [c for c in changes if c["allergen"] == allergen]


class ScopedFingerprintTest(unittest.TestCase):
    def test_order_independent(self):
        dictionary = {normalize_name(k): set(v) for k, v in {
            "乳清粉": ["乳"], "奶粉": ["乳", "大豆"], "可可脂": []}.items()}
        keys = ["奶粉", "乳清粉", "可可脂"]
        fp1 = scoped_fingerprint(keys, dictionary)
        fp2 = scoped_fingerprint(reversed(keys), dictionary)
        self.assertEqual(fp1, fp2)

    def test_subset_unrelated_changes_ignored(self):
        d1 = {normalize_name("乳清粉"): {"乳"}}
        d2 = {normalize_name("乳清粉"): {"乳"}, normalize_name("鱼粉"): {"鱼类"}}
        keys = [normalize_name("乳清粉")]
        self.assertEqual(scoped_fingerprint(keys, d1),
                         scoped_fingerprint(keys, d2))

    def test_present_absent_distinguished(self):
        # 未登记 vs 已登记空过敏原集合是不同映射
        d1 = {normalize_name("可可脂"): set()}
        keys = [normalize_name("可可脂")]
        self.assertNotEqual(scoped_fingerprint(keys, {}),
                            scoped_fingerprint(keys, d1))

    def test_allergen_set_change_detected(self):
        d1 = {normalize_name("乳清粉"): {"乳"}}
        d2 = {normalize_name("乳清粉"): {"乳", "大豆"}}
        keys = [normalize_name("乳清粉")]
        self.assertNotEqual(scoped_fingerprint(keys, d1),
                            scoped_fingerprint(keys, d2))


if __name__ == "__main__":
    unittest.main()
