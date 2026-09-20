"""原料别名接入正式审查与字典时效复核的集成测试（走 db 全链路）。

覆盖：
- 别名链解析到最终标准名参与字典查询 / 未识别判定 / 指纹；
- 配料树与遗漏来源路径仍显示原料原始名称；
- 别名规则变化后，仅当记录实际原料的最终标准名或其过敏原映射改变时
  才标记需复核（缩短等价链路、无关别名变化均不误报）；
- 别名批量接口的整批原子性（环 / 自环 / 多目标时不写入任何一条）；
- 别名仅接入正式审查：供应商方案风险预览不受别名影响。
"""
import os
import tempfile
import unittest

_DB_DIR = tempfile.mkdtemp(prefix="allergen-alias-flow-")
os.environ["DB_PATH"] = os.path.join(_DB_DIR, "alias_flow.db")

from app import db  # noqa: E402
from app.aliases import validate_alias_batch  # noqa: E402
from app.analysis import normalize_name  # noqa: E402
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
TEXT_A = ("配料表：小麦粉，巧克力酱（可可脂，浓缩乳清蛋白（乳清粉），乳糖），"
          "鸡蛋，食盐")
DECLARED = ["麸质谷物", "蛋类"]


class AliasReviewFlowTest(unittest.TestCase):
    def setUp(self):
        db.DB_PATH = os.path.join(
            _DB_DIR, f"alias_{self.id().rsplit('.', 1)[-1]}.db")
        db.init_db()
        db.bulk_import(DICT_TEXT)

    def _make(self, text_a=TEXT_A, declared=None):
        result, fp = analyze(
            text_a=text_a, text_b=None,
            label_a="现版配方", label_b="新版配方",
            declared=DECLARED if declared is None else declared)
        rid = db.create_review(
            name="别名测试",
            declared=sorted(set(DECLARED if declared is None else declared)),
            text_a=text_a, text_b=None,
            label_a="现版配方", label_b="新版配方",
            result=result, dict_fingerprint=fp)
        return rid, result, fp

    def test_alias_chain_resolves_for_dict_lookup(self):
        # 浓缩乳清蛋白 -> 乳清粉 -> 乳；配方未直接出现字典名
        self._apply("浓缩乳清蛋白：乳清粉")
        rid, result, _ = self._make()
        allergens = result["version_a"]["allergens"]
        self.assertIn("乳", allergens)
        # 未识别名单不再包含别名原料
        self.assertNotIn("浓缩乳清蛋白",
                         result["version_a"]["unknown_ingredients"])
        # 遗漏含乳，且来源路径显示原始名称
        check = result["label_check"]
        self.assertIn("乳", check["missing"])
        self.assertIn(
            ["巧克力酱", "浓缩乳清蛋白", "乳清粉"],
            check["missing_sources"]["乳"])

    def test_tree_keeps_raw_names_under_alias(self):
        self._apply("浓缩乳清蛋白：乳清粉")
        _, result, _ = self._make()

        def names(nodes):
            out = []
            for n in nodes:
                out.append(n["name"])
                out.extend(names(n["children"]))
            return out
        self.assertIn("浓缩乳清蛋白", names(result["version_a"]["tree"]))

    def test_alias_leaf_registration_resolves_unknown(self):
        # 无别名时叶子原料「脱盐乳清」未识别；加别名解析到字典项后应需复核
        text = TEXT_A.replace("乳糖）", "乳糖，脱盐乳清）")
        rid, result, _ = self._make(text_a=text)
        self.assertIn("脱盐乳清",
                      result["version_a"]["unknown_ingredients"])
        self.assertFalse(db.get_review(rid)["needs_recheck"])
        self._apply("脱盐乳清：乳清粉")
        self.assertTrue(db.get_review(rid)["needs_recheck"])
        outcome = build_recheck(db.get_review(rid))
        self.assertIn("乳", outcome["new_result"]["version_a"]["allergens"])
        self.assertNotIn("脱盐乳清",
                         outcome["new_result"]["version_a"]
                         ["unknown_ingredients"])

    def test_alias_chain_rewrite_to_same_standard_keeps_fresh(self):
        # a -> 乳清粉 与 a -> 中转 -> 乳清粉 的最终标准名同为「乳清粉」，
        # 过敏原映射也相同（中转本身不在配方中，不参与指纹），不应标需复核
        self._apply("浓缩乳清蛋白：中转名\n中转名：乳清粉")
        rid, _, _ = self._make()
        self.assertIn("乳", db.get_review(rid)["result"]
                      ["version_a"]["allergens"])
        self.assertFalse(db.get_review(rid)["needs_recheck"])

    def test_final_standard_change_invalidates(self):
        # a -> 乳清粉 改为 a -> 奶粉：两者过敏原恰好都是「乳」，
        # 但最终标准名改变了，按需求仍应标记需复核
        self._apply("浓缩乳清蛋白：乳清粉")
        rid, _, _ = self._make()
        self.assertFalse(db.get_review(rid)["needs_recheck"])
        self._apply("浓缩乳清蛋白：奶粉")
        self.assertEqual(self._standard(rid), normalize_name("奶粉"))
        self.assertTrue(db.get_review(rid)["needs_recheck"])

    def _standard(self, rid):
        return db.alias_resolver().resolve(normalize_name("浓缩乳清蛋白"))

    def test_mapping_change_under_standard_invalidates(self):
        # 别名不变，最终标准名（乳清粉）的过敏原映射变化 -> 需复核
        self._apply("浓缩乳清蛋白：乳清粉")
        rid, _, _ = self._make()
        db.upsert_entry("乳清粉", ["乳", "大豆"])
        self.assertTrue(db.get_review(rid)["needs_recheck"])

    def test_unrelated_alias_change_keeps_fresh(self):
        self._apply("浓缩乳清蛋白：乳清粉")
        rid, _, _ = self._make()
        # 与配方无关的别名增删不影响指纹
        self._apply("神秘别名：鸡蛋")
        self.assertFalse(db.get_review(rid)["needs_recheck"])
        db.delete_alias("神秘别名")
        self.assertFalse(db.get_review(rid)["needs_recheck"])

    def test_redundant_alias_chain_shortening_keeps_fresh(self):
        # a -> b -> 乳清粉 建立后出记录；再把 a 直接指向乳清粉（删除式改写），
        # 最终标准名与映射不变 -> 不复核
        self._apply("中转名：乳清粉\n浓缩乳清蛋白：中转名")
        rid, _, _ = self._make()
        self.assertIn("乳", db.get_review(rid)["result"]
                      ["version_a"]["allergens"])
        self._apply("浓缩乳清蛋白：乳清粉")
        self.assertFalse(db.get_review(rid)["needs_recheck"])

    def test_bulk_alias_is_atomic(self):
        # 含环的整批必须全部拒绝
        bad = validate_alias_batch("甲：乙\n乙：甲", db.alias_existing())
        self.assertTrue(bad["errors"])
        before = {a["alias"] for a in db.list_aliases()}
        db.bulk_upsert_aliases(bad["entries"])  # entries 为空，无写入
        self.assertEqual({a["alias"] for a in db.list_aliases()}, before)

        # 自环 + 多目标混合同样拒绝
        bad = validate_alias_batch(
            "x：x\na：乳\n a：蛋", db.alias_existing())
        self.assertTrue(bad["errors"])
        self.assertEqual(bad["entries"], [])

    def test_valid_bulk_persists_and_deletes(self):
        self._apply("浓缩乳清蛋白：乳清粉\n别名乙：鸡蛋")
        stored = {a["alias"]: a["target"] for a in db.list_aliases()}
        self.assertEqual(stored.get("浓缩乳清蛋白"), "乳清粉")
        self.assertEqual(stored.get("别名乙"), "鸡蛋")
        db.delete_alias("别名乙")
        self.assertNotIn("别名乙",
                         {a["alias"] for a in db.list_aliases()})

    def test_alias_not_applied_to_supplier_preview(self):
        # 别名仅接入正式审查：供应商风险预览沿用原始字典，不做别名解析
        from app.supplier_risk import analyze_supplier_risk
        self._apply("浓缩乳清蛋白：乳清粉")
        out = analyze_supplier_risk(
            text="浓缩乳清蛋白", plans_text="", declared=[])
        self.assertIn("浓缩乳清蛋白", out["unknown_ingredients"])

    def _apply(self, text):
        outcome = validate_alias_batch(text, db.alias_existing())
        assert not outcome["errors"], outcome["errors"]
        db.bulk_upsert_aliases(outcome["entries"])


if __name__ == "__main__":
    unittest.main()
