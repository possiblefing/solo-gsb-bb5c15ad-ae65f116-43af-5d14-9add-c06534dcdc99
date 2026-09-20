"""共线生产换线排程测试（不依赖 FastAPI，走 db + review + changeover 全链路）。

覆盖：
- 复用审查记录当前配方（两版取标签核对对象）的最终过敏原集合；
- 有向换线成本（前一产品含有、后一产品不含）与逐段 / 累计耗时；
- 子集动态规划最优顺序（对照暴力枚举）与并列时按产品 ID 序列取舍；
- 缺失 / 需复核记录禁止参与并指出原因；
- 数量与分钟数校验；全程只读、不改写审查记录。
"""
import itertools
import os
import tempfile
import unittest
from fractions import Fraction

_DB_DIR = tempfile.mkdtemp(prefix="allergen-changeover-")
os.environ["DB_PATH"] = os.path.join(_DB_DIR, "changeover.db")

from app import db  # noqa: E402
from app.changeover import (ChangeoverError,  # noqa: E402
                            build_changeover_preview, build_schedule,
                            list_changeover_products)
from app.recheck import build_recheck  # noqa: E402
from app.review import analyze  # noqa: E402

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


def make_review(text_a, text_b=None, name="排程测试", with_fingerprint=True):
    result, fp = analyze(text_a=text_a, text_b=text_b, label_a="现版配方",
                         label_b="新版配方", declared=[])
    return db.create_review(
        name=name, declared=[], text_a=text_a, text_b=text_b,
        label_a="现版配方", label_b="新版配方", result=result,
        dict_fingerprint=fp if with_fingerprint else None)


def brute_force_best(products, minutes):
    """暴力枚举全部生产顺序，返回 (最小总成本, 最小产品ID序列, 并列数量)。

    分钟数同样经 Fraction 精确化，与被测实现的比较口径一致。
    """
    exact = {a: Fraction(str(v)) for a, v in minutes.items()}
    best_cost, best_seq, count = None, None, 0
    for perm in itertools.permutations(products):
        cost = sum(
            (sum((exact[a] for a in set(x["allergens"]) - set(y["allergens"])),
                 Fraction(0)) for x, y in zip(perm, perm[1:])),
            Fraction(0))
        seq = tuple(p["id"] for p in perm)
        if best_cost is None or cost < best_cost:
            best_cost, best_seq, count = cost, seq, 1
        elif cost == best_cost:
            count += 1
            best_seq = min(best_seq, seq)
    return best_cost, best_seq, count


class ScheduleTest(unittest.TestCase):
    """纯计算：有向成本、子集 DP、并列处理、逐段明细（不触碰数据库）。"""

    def test_directed_cost_asymmetric(self):
        products = [
            {"id": 1, "name": "甲", "allergens": ["乳", "大豆"]},
            {"id": 2, "name": "乙", "allergens": ["乳"]},
        ]
        r = build_schedule(products, {"乳": 5, "大豆": 10})
        # 乙→甲 无需清洁（乙的过敏原甲都含）；甲→乙 需清洁大豆（10 分钟）
        self.assertEqual(r["order"], [2, 1])
        self.assertEqual(r["total_minutes"], 0)
        self.assertEqual(len(r["segments"]), 1)
        self.assertEqual(r["segments"][0]["allergens"], [])
        self.assertEqual(r["segments"][0]["minutes"], 0)
        self.assertEqual(r["segments"][0]["cumulative_minutes"], 0)

    def test_optimal_matches_brute_force(self):
        products = [
            {"id": 11, "name": "P11", "allergens": ["乳", "大豆"]},
            {"id": 22, "name": "P22", "allergens": ["乳"]},
            {"id": 33, "name": "P33", "allergens": ["大豆", "蛋类"]},
            {"id": 44, "name": "P44", "allergens": []},
            {"id": 55, "name": "P55", "allergens": ["乳", "蛋类", "坚果"]},
        ]
        minutes = {"乳": 10, "大豆": 20, "蛋类": 5, "坚果": 7}
        r = build_schedule(products, minutes)
        cost, seq, count = brute_force_best(products, minutes)
        self.assertEqual(r["total_minutes"], int(cost))
        self.assertEqual(tuple(r["order"]), seq)
        self.assertEqual(r["optimal_count"], count)
        # 逐段明细：方向、差集过敏原、耗时与累计耗时均一致
        self.assertEqual(len(r["segments"]), len(products) - 1)
        sets = {p["id"]: set(p["allergens"]) for p in products}
        acc = 0
        for seg, (a, b) in zip(r["segments"], zip(r["order"], r["order"][1:])):
            self.assertEqual(seg["from"]["id"], a)
            self.assertEqual(seg["to"]["id"], b)
            expect = sorted(sets[a] - sets[b])
            self.assertEqual(seg["allergens"], expect)
            self.assertEqual([d["allergen"] for d in seg["details"]], expect)
            self.assertEqual(seg["minutes"], sum(minutes[x] for x in expect))
            acc += seg["minutes"]
            self.assertEqual(seg["cumulative_minutes"], acc)
        self.assertEqual(r["total_minutes"], acc)

    def test_tie_break_by_id_sequence(self):
        # 三个产品过敏原完全相同：全部 3! 种顺序成本都是 0
        products = [{"id": i, "name": f"P{i}", "allergens": ["乳"]}
                    for i in (9, 5, 7)]
        r = build_schedule(products, {"乳": 5})
        self.assertEqual(r["total_minutes"], 0)
        self.assertEqual(r["order"], [5, 7, 9])   # 并列时取 ID 序列最小者
        self.assertEqual(r["optimal_count"], 6)

    def test_float_artifact_tie_detected(self):
        # 0.1 + 0.2 与 0.3 在二进制浮点下不等，精确化后应判为并列
        products = [
            {"id": 1, "name": "甲", "allergens": ["乳", "大豆"]},
            {"id": 2, "name": "乙", "allergens": ["蛋类"]},
            {"id": 3, "name": "丙", "allergens": []},
        ]
        minutes = {"乳": 0.1, "大豆": 0.2, "蛋类": 0.3}
        r = build_schedule(products, minutes)
        cost, seq, count = brute_force_best(products, minutes)
        self.assertEqual(tuple(r["order"]), seq)
        self.assertEqual(r["order"], [1, 3, 2])
        self.assertEqual(r["optimal_count"], 4)
        self.assertEqual(count, 4)
        self.assertAlmostEqual(r["total_minutes"], 0.3)

    def test_unconfigured_allergen_defaults_zero(self):
        products = [
            {"id": 1, "name": "甲", "allergens": ["乳", "大豆"]},
            {"id": 2, "name": "乙", "allergens": ["乳"]},
        ]
        r = build_schedule(products, {})   # 未配置任何分钟数
        self.assertEqual(r["cleaning_minutes"], {"乳": 0, "大豆": 0})
        self.assertEqual(r["total_minutes"], 0)
        self.assertEqual(r["optimal_count"], 2)


class PreviewFlowTest(unittest.TestCase):
    """全链路：候选清单、资格校验、排程结果与只读性。"""

    def setUp(self):
        # 每个用例使用独立数据库，互不污染
        db.DB_PATH = os.path.join(
            _DB_DIR, f"changeover_{self.id().rsplit('.', 1)[-1]}.db")
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
        products = {p["id"]: p for p in list_changeover_products()}
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
        ids = [p["id"] for p in list_changeover_products()]
        self.assertEqual(ids, sorted(ids))

    def test_preview_end_to_end_and_readonly(self):
        rid1 = make_review(TEXT_A, name="产品甲")
        rid2 = make_review(TEXT_B, name="产品乙")
        rid3 = make_review(TEXT_C, name="产品丙")
        before = {rid: db.get_review(rid) for rid in (rid1, rid2, rid3)}
        minutes = {"乳": 10, "大豆": 20, "坚果": 15, "芝麻": 5}
        r = build_changeover_preview([rid3, rid1, rid2], minutes)
        products = [
            {"id": rid1, "name": "产品甲",
             "allergens": ["麸质谷物", "乳", "大豆", "蛋类"]},
            {"id": rid2, "name": "产品乙",
             "allergens": ["麸质谷物", "坚果", "蛋类", "芝麻"]},
            {"id": rid3, "name": "产品丙", "allergens": ["麸质谷物", "蛋类"]},
        ]
        cost, seq, count = brute_force_best(products, minutes)
        self.assertEqual(r["total_minutes"], int(cost))
        self.assertEqual(tuple(r["order"]), seq)
        self.assertEqual(r["optimal_count"], count)
        self.assertEqual(len(r["segments"]), 2)
        self.assertEqual(r["total_minutes"],
                         sum(s["minutes"] for s in r["segments"]))
        # 只读：记录内容、指纹、时效状态与记录数均不变
        for rid, old in before.items():
            self.assertEqual(db.get_review(rid), old)
            self.assertEqual(self._fp(rid), old["dict_fingerprint"])
        self.assertEqual(len(db.list_reviews()), 3)

    def test_missing_record_blocked(self):
        rid = make_review(TEXT_A)
        with self.assertRaises(ChangeoverError) as ctx:
            build_changeover_preview([rid, 999999], {})
        [p] = ctx.exception.problems
        self.assertEqual(p["reason"], "missing")
        self.assertEqual(p["id"], 999999)
        self.assertIn("不存在", p["message"])

    def test_stale_record_blocked_until_rechecked(self):
        rid1 = make_review(TEXT_A, name="产品甲")
        rid2 = make_review(TEXT_C, name="产品丙")
        db.upsert_entry("乳清粉", ["乳", "花生"])  # 相关原料映射变更 → 需复核
        self.assertTrue(db.get_review(rid1)["needs_recheck"])
        with self.assertRaises(ChangeoverError) as ctx:
            build_changeover_preview([rid1, rid2], {})
        [p] = ctx.exception.problems
        self.assertEqual(p["reason"], "stale")
        self.assertEqual(p["id"], rid1)
        self.assertIn("需复核", p["message"])
        # 候选清单同步标记禁止参与并给出原因
        products = {p["id"]: p for p in list_changeover_products()}
        self.assertFalse(products[rid1]["eligible"])
        self.assertIn("需复核", products[rid1]["blocked_reason"])
        self.assertTrue(products[rid2]["eligible"])
        # 完成字典时效复核后恢复可参与
        outcome = build_recheck(db.get_review(rid1))
        db.update_review_result(rid1, outcome["new_result"],
                                outcome["fingerprint"])
        self.assertFalse(db.get_review(rid1)["needs_recheck"])
        r = build_changeover_preview([rid1, rid2], {})
        self.assertEqual(len(r["segments"]), 1)

    def test_legacy_record_blocked(self):
        rid1 = make_review(TEXT_A, with_fingerprint=False)
        rid2 = make_review(TEXT_C)
        with self.assertRaises(ChangeoverError) as ctx:
            build_changeover_preview([rid1, rid2], {})
        [p] = ctx.exception.problems
        self.assertEqual(p["reason"], "legacy")
        self.assertIn("指纹", p["message"])

    def test_selection_count_and_duplicates(self):
        rid = make_review(TEXT_A)
        with self.assertRaises(ChangeoverError):
            build_changeover_preview([rid], {})            # 少于 2 个
        with self.assertRaises(ChangeoverError):
            build_changeover_preview(list(range(11)), {})  # 超过 10 个
        rid2 = make_review(TEXT_C)
        with self.assertRaises(ChangeoverError) as ctx:
            build_changeover_preview([rid, rid, rid2], {})
        self.assertIn("重复", str(ctx.exception))

    def test_minutes_validation(self):
        rid1 = make_review(TEXT_A)
        rid2 = make_review(TEXT_C)
        with self.assertRaises(ChangeoverError):
            build_changeover_preview([rid1, rid2], {"乳": -5})
        with self.assertRaises(ChangeoverError):
            build_changeover_preview([rid1, rid2], {"乳": "abc"})
        # 未配置的过敏原按 0 分钟计
        r = build_changeover_preview([rid1, rid2], {"乳": 10})
        self.assertEqual(r["cleaning_minutes"].get("大豆"), 0)


if __name__ == "__main__":
    unittest.main()
