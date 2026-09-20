"""共用标签模板分组测试（不依赖 FastAPI，走 db + review + template_group 全链路）。

覆盖：
- 复用审查记录当前配方（两版取标签核对对象）的最终过敏原集合；
- 组模板 = 过敏原并集、附加声明 = 模板 − 自身，附加声明数超限不可成组；
- 子集动态规划求模板数最少的完整分组（对照暴力枚举），并列按组内 / 组间
  产品 ID 序列取唯一最小者；
- 缺失 / 需复核记录禁止参与并逐条指出原因；
- 数量与附加声明上限校验；全程只读、不改写审查记录。
"""
import os
import tempfile
import unittest

_DB_DIR = tempfile.mkdtemp(prefix="allergen-template-group-")
os.environ["DB_PATH"] = os.path.join(_DB_DIR, "template_group.db")

from app import db  # noqa: E402
from app.recheck import build_recheck  # noqa: E402
from app.review import analyze  # noqa: E402
from app.template_group import (TemplateGroupError,  # noqa: E402
                                build_grouping, build_template_group_preview,
                                list_template_products)

DICT_TEXT = """\
小麦粉：麸质谷物
乳清粉：乳
奶粉：乳
大豆卵磷脂：大豆
鸡蛋：蛋类
椰子粉：坚果
芝麻：芝麻
食盐：
"""
# A：麸质谷物、乳、大豆、蛋类；B：麸质谷物、坚果、蛋类、芝麻；C：麸质谷物、蛋类
TEXT_A = "配料表：小麦粉，奶粉（乳清粉），大豆卵磷脂，鸡蛋，食盐"
TEXT_B = "配料表：小麦粉，椰子粉，鸡蛋，芝麻，食盐"
TEXT_C = "配料表：小麦粉，鸡蛋，食盐"


def make_review(text_a, text_b=None, name="分组测试", with_fingerprint=True):
    result, fp = analyze(text_a=text_a, text_b=text_b, label_a="现版配方",
                         label_b="新版配方", declared=[])
    return db.create_review(
        name=name, declared=[], text_a=text_a, text_b=text_b,
        label_a="现版配方", label_b="新版配方", result=result,
        dict_fingerprint=fp if with_fingerprint else None)


def brute_force_best(products, limits):
    """暴力枚举全部完整分组，返回 (最少模板数, 最小组间ID序列, 方案数)。

    部分组一旦不可行，加入更多成员后仍不可行（并集只会变大），可安全剪枝。
    """
    products = sorted(products, key=lambda p: p["id"])
    n = len(products)
    ids = [p["id"] for p in products]
    sets = [set(p["allergens"]) for p in products]
    lim = [limits.get(pid, 0) for pid in ids]

    def feasible(group):
        union = set().union(*(sets[i] for i in group))
        return all(len(union - sets[i]) <= lim[i] for i in group)

    def canon(groups):
        return tuple(sorted(tuple(sorted(ids[i] for i in g)) for g in groups))

    best_count, best_key, ways = None, None, 0

    def dfs(idx, groups):
        nonlocal best_count, best_key, ways
        if idx == n:
            count, key = len(groups), canon(groups)
            if best_count is None or count < best_count:
                best_count, best_key, ways = count, key, 1
            elif count == best_count:
                ways += 1
                best_key = min(best_key, key)
            return
        for g in groups:
            g.append(idx)
            if feasible(g):
                dfs(idx + 1, groups)
            g.pop()
        groups.append([idx])
        dfs(idx + 1, groups)
        groups.pop()

    dfs(0, [])
    return best_count, best_key, ways


def grouping_key(result):
    """从分组结果提取组间 ID 序列（与暴力枚举的规范化键同口径）。"""
    return tuple(sorted(tuple(m["id"] for m in g["members"])
                        for g in result["groups"]))


class GroupingTest(unittest.TestCase):
    """纯计算：并集模板、附加声明上限、子集 DP、并列处理（不触碰数据库）。"""

    def test_template_is_union_and_extra_tracked(self):
        products = [
            {"id": 1, "name": "甲", "allergens": ["乳"]},
            {"id": 2, "name": "乙", "allergens": ["乳", "大豆"]},
        ]
        r = build_grouping(products, {1: 1, 2: 0})
        # 甲可附加 1 个声明（大豆），乙上限 0；两产品可共用一张模板
        self.assertEqual(r["template_count"], 1)
        [g] = r["groups"]
        self.assertEqual(g["template"], ["乳", "大豆"])
        self.assertEqual([m["id"] for m in g["members"]], [1, 2])
        ma, mb = g["members"]
        self.assertEqual(ma["extra"], ["大豆"])
        self.assertEqual(ma["extra_count"], 1)
        self.assertEqual(ma["max_extra"], 1)
        self.assertEqual(mb["extra"], [])
        self.assertEqual(mb["extra_count"], 0)

    def test_limit_blocks_grouping(self):
        # 乙上限 0 且自身不含乳：与甲同组需附加「乳」，不可成组
        products = [
            {"id": 1, "name": "甲", "allergens": ["乳"]},
            {"id": 2, "name": "乙", "allergens": ["大豆"]},
        ]
        r = build_grouping(products, {1: 1, 2: 0})
        self.assertEqual(r["template_count"], 2)
        self.assertEqual(grouping_key(r), ((1,), (2,)))
        self.assertEqual(r["optimal_count"], 1)

    def test_default_limit_is_zero(self):
        # 未配置上限的产品按 0 个附加声明计：只有过敏原相同才能同组
        products = [
            {"id": 1, "name": "甲", "allergens": ["乳"]},
            {"id": 2, "name": "乙", "allergens": ["乳"]},
            {"id": 3, "name": "丙", "allergens": ["乳", "大豆"]},
        ]
        r = build_grouping(products, {})
        self.assertEqual(r["template_count"], 2)
        self.assertEqual(grouping_key(r), ((1, 2), (3,)))
        self.assertEqual(r["products"][0]["max_extra"], 0)

    def test_optimal_matches_brute_force(self):
        products = [
            {"id": 11, "name": "P11", "allergens": ["乳", "大豆"]},
            {"id": 22, "name": "P22", "allergens": ["乳"]},
            {"id": 33, "name": "P33", "allergens": ["大豆", "蛋类"]},
            {"id": 44, "name": "P44", "allergens": []},
            {"id": 55, "name": "P55", "allergens": ["乳", "蛋类", "坚果"]},
        ]
        limits = {11: 2, 22: 1, 33: 1, 44: 0, 55: 2}
        r = build_grouping(products, limits)
        count, key, ways = brute_force_best(products, limits)
        self.assertEqual(r["template_count"], count)
        self.assertEqual(grouping_key(r), key)
        self.assertEqual(r["optimal_count"], ways)
        # 每组模板 = 成员过敏原并集；每个成员附加声明数不超上限
        for g in r["groups"]:
            union = sorted({a for m in g["members"] for a in m["allergens"]})
            self.assertEqual(g["template"], union)
            for m in g["members"]:
                self.assertEqual(m["extra"],
                                 sorted(set(union) - set(m["allergens"])))
                self.assertLessEqual(m["extra_count"], m["max_extra"])
        # 完整分组：每个产品恰好归入一组
        member_ids = sorted(m["id"] for g in r["groups"] for m in g["members"])
        self.assertEqual(member_ids, [11, 22, 33, 44, 55])

    def test_tie_break_by_id_sequence(self):
        # 2×2 结构：{{1,2},{3,4}} 与 {{1,3},{2,4}} 同为 2 张模板
        products = [
            {"id": 1, "name": "P1", "allergens": ["乳", "芝麻"]},
            {"id": 2, "name": "P2", "allergens": ["乳", "坚果"]},
            {"id": 3, "name": "P3", "allergens": ["大豆", "芝麻"]},
            {"id": 4, "name": "P4", "allergens": ["大豆", "坚果"]},
        ]
        limits = {1: 1, 2: 1, 3: 1, 4: 1}
        r = build_grouping(products, limits)
        count, key, ways = brute_force_best(products, limits)
        self.assertEqual(r["template_count"], 2)
        self.assertEqual(ways, 2)
        self.assertEqual(r["optimal_count"], 2)
        # 组内按 ID 升序、组间按 ID 序列字典序取最小者
        self.assertEqual(grouping_key(r), ((1, 2), (3, 4)))
        self.assertEqual(grouping_key(r), key)

    def test_string_and_float_limits_accepted(self):
        products = [
            {"id": 1, "name": "甲", "allergens": ["乳"]},
            {"id": 2, "name": "乙", "allergens": ["大豆"]},
        ]
        r = build_grouping(products, {1: "1", 2: 1.0})
        self.assertEqual(r["template_count"], 1)


class PreviewFlowTest(unittest.TestCase):
    """全链路：候选清单、资格校验、分组结果与只读性。"""

    def setUp(self):
        # 每个用例使用独立数据库，互不污染
        db.DB_PATH = os.path.join(
            _DB_DIR, f"template_group_{self.id().rsplit('.', 1)[-1]}.db")
        db.init_db()
        db.bulk_import(DICT_TEXT)

    def _fp(self, rid):
        with db._connect() as conn:
            return conn.execute(
                "SELECT dict_fingerprint FROM reviews WHERE id=?",
                (rid,)).fetchone()[0]

    def test_current_allergens_from_review(self):
        rid_a = make_review(TEXT_A, name="单版")
        rid_ab = make_review(TEXT_A, TEXT_B, name="两版")
        products = {p["id"]: p for p in list_template_products()}
        self.assertEqual(products[rid_a]["allergens"],
                         sorted(["麸质谷物", "乳", "大豆", "蛋类"]))
        self.assertEqual(products[rid_a]["source_version"], "现版配方")
        # 两版记录取标签核对对象（新版配方）的最终过敏原集合
        self.assertEqual(products[rid_ab]["allergens"],
                         sorted(["麸质谷物", "坚果", "蛋类", "芝麻"]))
        self.assertEqual(products[rid_ab]["source_version"], "新版配方")
        self.assertTrue(products[rid_a]["eligible"])
        self.assertIsNone(products[rid_a]["blocked_reason"])
        # 清单按产品 ID 升序
        ids = [p["id"] for p in list_template_products()]
        self.assertEqual(ids, sorted(ids))

    def test_preview_end_to_end_and_readonly(self):
        rid1 = make_review(TEXT_A, name="产品甲")
        rid2 = make_review(TEXT_B, name="产品乙")
        rid3 = make_review(TEXT_C, name="产品丙")
        before = {rid: db.get_review(rid) for rid in (rid1, rid2, rid3)}
        limits = {rid1: 2, rid2: 2, rid3: 2}
        r = build_template_group_preview([rid3, rid1, rid2],
                                         {str(k): v for k, v in limits.items()})
        products = [
            {"id": rid1, "name": "产品甲",
             "allergens": ["麸质谷物", "乳", "大豆", "蛋类"]},
            {"id": rid2, "name": "产品乙",
             "allergens": ["麸质谷物", "坚果", "蛋类", "芝麻"]},
            {"id": rid3, "name": "产品丙", "allergens": ["麸质谷物", "蛋类"]},
        ]
        count, key, ways = brute_force_best(products, limits)
        self.assertEqual(r["template_count"], count)
        self.assertEqual(grouping_key(r), key)
        self.assertEqual(r["optimal_count"], ways)
        # 丙并入任一组都需附加 2 个声明：上限 2 时可两两成组，最少 2 张模板
        self.assertEqual(r["template_count"], 2)
        self.assertEqual(r["optimal_count"], 3)
        # 组间 ID 序列最小者：{甲} 与 {乙, 丙}
        self.assertEqual(grouping_key(r), ((rid1,), (rid2, rid3)))
        # 只读：记录内容、指纹、时效状态与记录数均不变
        for rid, old in before.items():
            self.assertEqual(db.get_review(rid), old)
            self.assertEqual(self._fp(rid), old["dict_fingerprint"])
        self.assertEqual(len(db.list_reviews()), 3)

    def test_missing_record_blocked(self):
        rid = make_review(TEXT_A)
        with self.assertRaises(TemplateGroupError) as ctx:
            build_template_group_preview([rid, 999999], {})
        [p] = ctx.exception.problems
        self.assertEqual(p["reason"], "missing")
        self.assertEqual(p["id"], 999999)
        self.assertIn("不存在", p["message"])

    def test_stale_record_blocked_until_rechecked(self):
        rid1 = make_review(TEXT_A, name="产品甲")
        rid2 = make_review(TEXT_C, name="产品丙")
        db.upsert_entry("乳清粉", ["乳", "花生"])  # 相关原料映射变更 → 需复核
        self.assertTrue(db.get_review(rid1)["needs_recheck"])
        with self.assertRaises(TemplateGroupError) as ctx:
            build_template_group_preview([rid1, rid2], {})
        [p] = ctx.exception.problems
        self.assertEqual(p["reason"], "stale")
        self.assertEqual(p["id"], rid1)
        self.assertIn("需复核", p["message"])
        # 候选清单同步标记禁止参与并给出原因
        products = {p["id"]: p for p in list_template_products()}
        self.assertFalse(products[rid1]["eligible"])
        self.assertIn("需复核", products[rid1]["blocked_reason"])
        self.assertTrue(products[rid2]["eligible"])
        # 完成字典时效复核后恢复可参与
        outcome = build_recheck(db.get_review(rid1))
        db.update_review_result(rid1, outcome["new_result"],
                                outcome["fingerprint"])
        self.assertFalse(db.get_review(rid1)["needs_recheck"])
        r = build_template_group_preview([rid1, rid2], {})
        self.assertGreaterEqual(r["template_count"], 1)

    def test_legacy_record_blocked(self):
        rid1 = make_review(TEXT_A, with_fingerprint=False)
        rid2 = make_review(TEXT_C)
        with self.assertRaises(TemplateGroupError) as ctx:
            build_template_group_preview([rid1, rid2], {})
        [p] = ctx.exception.problems
        self.assertEqual(p["reason"], "legacy")
        self.assertIn("指纹", p["message"])

    def test_selection_count_and_duplicates(self):
        rid = make_review(TEXT_A)
        with self.assertRaises(TemplateGroupError):
            build_template_group_preview([rid], {})            # 少于 2 个
        with self.assertRaises(TemplateGroupError):
            build_template_group_preview(list(range(13)), {})  # 超过 12 个
        rid2 = make_review(TEXT_C)
        with self.assertRaises(TemplateGroupError) as ctx:
            build_template_group_preview([rid, rid, rid2], {})
        self.assertIn("重复", str(ctx.exception))

    def test_max_extra_validation(self):
        rid1 = make_review(TEXT_A)
        rid2 = make_review(TEXT_C)
        with self.assertRaises(TemplateGroupError):
            build_template_group_preview([rid1, rid2], {str(rid1): -1})
        with self.assertRaises(TemplateGroupError):
            build_template_group_preview([rid1, rid2], {str(rid1): 1.5})
        with self.assertRaises(TemplateGroupError):
            build_template_group_preview([rid1, rid2], {str(rid1): "abc"})
        with self.assertRaises(TemplateGroupError):
            build_template_group_preview([rid1, rid2], {str(rid1): True})
        # 未配置的产品按 0 个附加声明计
        r = build_template_group_preview([rid1, rid2], {})
        used = {p["id"]: p["max_extra"] for p in r["products"]}
        self.assertEqual(used, {rid1: 0, rid2: 0})


if __name__ == "__main__":
    unittest.main()
