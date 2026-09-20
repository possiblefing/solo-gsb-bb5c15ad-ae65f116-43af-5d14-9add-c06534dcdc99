"""过敏原检测套餐规划测试（不依赖 FastAPI，走 db + review + allergen_panel 全链路）。

覆盖：
- 仅允许选择一条时效正常记录，读取其当前配方（两版取新版）最终过敏原；
- 「套餐名：价格：过敏原列表」逐行解析：三段格式、价格精确小数、空过敏原、
  注释空行、套餐名去空与重名整批拒绝（逐行给出问题）；
- 子集动态规划：最低总价覆盖、同价按套餐名序列字典序取唯一、并列数，
  对照暴力枚举；0 价冗余套餐（覆盖至少一个必须项但加入后不新增覆盖）
  作为不同同价组合计入并列、其展示方案覆盖贡献为空且累计覆盖不变，
  正价冗余永不进入最低价；
- 精确有理数（0.1 + 0.2 == 0.3）；
- 无法覆盖时列出缺口；覆盖贡献与累计覆盖；额外过敏原与无用套餐不参与组合；
- 缺失 / 非法 ID / 需复核记录拒绝并说明；全程只读、不写库。
"""
import os
import tempfile
import unittest
from fractions import Fraction
from itertools import combinations

_DB_DIR = tempfile.mkdtemp(prefix="allergen-panel-")
os.environ["DB_PATH"] = os.path.join(_DB_DIR, "panel.db")

from app import db  # noqa: E402
from app.allergen_panel import (PanelError, build_panel_preview,  # noqa: E402
                                list_panel_records, parse_packages,
                                solve_panel)
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
椰子粉：坚果
芝麻：芝麻
食盐：
"""
TEXT_MILK = "配料表：小麦粉，白砂糖，巧克力酱（可可脂，奶粉（乳清粉，乳糖），大豆卵磷脂），鸡蛋，食盐"
TEXT_B = "配料表：小麦粉，椰子粉，鸡蛋，芝麻，食盐"


def make_review(text_a, text_b=None, name="套餐测试", with_fingerprint=True):
    result, fp = analyze(text_a=text_a, text_b=text_b, label_a="现版配方",
                         label_b="新版配方", declared=[])
    return db.create_review(
        name=name, declared=[], text_a=text_a, text_b=text_b,
        label_a="现版配方", label_b="新版配方", result=result,
        dict_fingerprint=fp if with_fingerprint else None)


def brute_force(target, packages):
    """暴力枚举全部套餐子集，返回 (最低总价 Fraction|None, 名称键, 方案数)。"""
    required = set(target)
    useful = [p for p in packages if set(p["allergens"]) & required]
    best, best_key, ways = None, None, 0
    for r in range(len(useful) + 1):
        for combo in combinations(useful, r):
            covered = set()
            for p in combo:
                covered |= set(p["allergens"]) & required
            if not required <= covered:
                continue
            cost = sum((p["price"] for p in combo), Fraction(0))
            key = tuple(sorted(p["name"] for p in combo))
            if best is None or cost < best:
                best, best_key, ways = cost, key, 1
            elif cost == best:
                ways += 1
                if key < best_key:
                    best_key = key
    return best, best_key, ways


class ParseTest(unittest.TestCase):
    """套餐行解析：格式、精确价格、重名、注释。"""

    def test_basic_parse(self):
        pkgs = parse_packages(
            "# 注释\n\n基础套餐：9.90：麸质谷物，蛋类\n"
            "空包:0:\n乳品：10：乳；坚果")
        self.assertEqual([p["name"] for p in pkgs], ["基础套餐", "空包", "乳品"])
        self.assertEqual(pkgs[0]["price"], Fraction("9.90"))
        self.assertEqual(pkgs[0]["allergens"], ["蛋类", "麸质谷物"])
        self.assertEqual(pkgs[1]["allergens"], [])
        self.assertEqual(pkgs[2]["allergens"], ["乳", "坚果"])

    def test_format_and_price_and_duplicate(self):
        with self.assertRaises(PanelError) as ctx:
            parse_packages("只有名\n  ：1：乳\nA：1：乳\nA：2：蛋类")
        types = [(p["line"], p["type"]) for p in ctx.exception.problems]
        self.assertEqual(types, [(1, "format"), (2, "empty_name"),
                                 (4, "duplicate_name")])
        with self.assertRaises(PanelError) as ctx:
            parse_packages("A：abc：乳")
        self.assertEqual(ctx.exception.problems[0]["type"], "bad_price")
        with self.assertRaises(PanelError) as ctx:
            parse_packages("A：-1：乳")
        self.assertEqual(ctx.exception.problems[0]["type"], "bad_price")
        with self.assertRaises(PanelError) as ctx:
            parse_packages("A：1：2：乳")  # 三段以上
        self.assertEqual(ctx.exception.problems[0]["type"], "format")

    def test_exponent_price_rejected(self):
        with self.assertRaises(PanelError):
            parse_packages("A：1e3：乳")


class DPTest(unittest.TestCase):
    """子集动态规划纯计算：最低总价、并列字典序（对照暴力枚举）、缺口。"""

    def _pkgs(self, spec):
        return [{"name": n, "price": Fraction(str(price)), "allergens": list(al)}
                for n, price, al in spec]

    def test_min_cost_and_contribution(self):
        target = ["乳", "大豆", "蛋类", "麸质谷物"]
        pkgs = self._pkgs([
            ("基础", "9.9", ["麸质谷物", "蛋类"]),
            ("乳品", "10", ["乳"]),
            ("全包", "30", ["乳", "大豆", "蛋类", "麸质谷物"]),
            ("大豆加购", "5", ["大豆"]),
            ("零价空包", "0", []),
        ])
        r = solve_panel(target, pkgs)
        self.assertTrue(r["feasible"])
        self.assertEqual(r["total_price"], 24.9)
        self.assertEqual([s["name"] for s in r["selections"]],
                         ["乳品", "基础", "大豆加购"])
        # 覆盖贡献与累计覆盖按套餐名序列展示
        self.assertEqual(r["selections"][0]["contribution"], ["乳"])
        self.assertEqual(r["selections"][0]["cumulative_covered"], ["乳"])
        self.assertEqual(r["selections"][1]["contribution"], ["蛋类", "麸质谷物"])
        self.assertEqual(r["selections"][2]["contribution"], ["大豆"])
        self.assertEqual(r["selections"][2]["cumulative_covered"],
                         ["乳", "大豆", "蛋类", "麸质谷物"])
        # 无必须项覆盖的套餐不参与组合
        self.assertEqual([p["name"] for p in r["unused_packages"]], ["零价空包"])

    def test_tie_break_and_count(self):
        # 0.1 + 0.2 与 0.3 同价：(X,Y) 与 (Z) 并列，字典序取 (X,Y)
        target = ["乳", "大豆", "蛋类", "麸质谷物"]
        pkgs = self._pkgs([
            ("X", "0.1", ["乳"]),
            ("Y", "0.2", ["大豆", "蛋类", "麸质谷物"]),
            ("Z", "0.3", ["乳", "大豆", "蛋类", "麸质谷物"]),
        ])
        r = solve_panel(target, pkgs)
        self.assertEqual(r["total_price"], 0.3)
        self.assertEqual(r["optimal_count"], 2)
        self.assertEqual([s["name"] for s in r["selections"]], ["X", "Y"])

    def test_zero_price_redundant_counts_as_tie(self):
        # 加赠（0 元，仅覆盖麸质谷物）覆盖至少一个必须项；加入「基础」后
        # 不新增覆盖，旧实现漏算该并列：(基础) 与 (基础,加赠) 同价 14.9
        target = ["蛋类", "麸质谷物"]
        pkgs = self._pkgs([
            ("基础", "14.9", ["蛋类", "麸质谷物"]),
            ("加赠", "0", ["麸质谷物"]),
        ])
        r = solve_panel(target, pkgs)
        self.assertTrue(r["feasible"])
        self.assertEqual(r["total_price"], 14.9)
        self.assertEqual(r["optimal_count"], 2)
        # 两个名称序列 ('基础',) 与 ('加赠','基础')，字典序取后者
        self.assertEqual([s["name"] for s in r["selections"]], ["加赠", "基础"])
        # 加赠先贡献麸质谷物，基础加入时麸质谷物已覆盖 → 只贡献蛋类
        self.assertEqual(r["selections"][0]["contribution"], ["麸质谷物"])
        self.assertEqual(r["selections"][0]["cumulative_covered"], ["麸质谷物"])
        self.assertEqual(r["selections"][1]["contribution"], ["蛋类"])
        self.assertEqual(r["selections"][1]["cumulative_covered"],
                         ["蛋类", "麸质谷物"])
        bc, bkey, bways = brute_force(target, pkgs)
        self.assertEqual(Fraction("14.9"), bc)
        self.assertEqual(bways, 2)
        self.assertEqual(bkey, ("加赠", "基础"))

    def test_zero_price_redundant_empty_contribution_and_cumulative(self):
        # 0 元冗余套餐名排在「主套餐」之后时：两个组合并列，但不含它的
        # 名称序列是前缀、字典序最小 → 展示方案不含赠品
        target = ["乳", "蛋类"]
        pkgs = self._pkgs([
            ("主套餐", "10", ["乳", "蛋类"]),
            ("赠品", "0", ["乳"]),
        ])
        r = solve_panel(target, pkgs)
        self.assertEqual(r["total_price"], 10)
        self.assertEqual(r["optimal_count"], 2)
        self.assertEqual([s["name"] for s in r["selections"]], ["主套餐"])

        # 0 元冗余套餐名夹在两个付费套餐之间（A付 < A赠 < B豆）：含它的
        # 序列字典序更小而被展示；乳已被前面的 A付 覆盖 → 覆盖贡献为空、
        # 累计覆盖保持不变
        target3 = ["乳", "蛋类", "大豆"]
        pkgs3 = self._pkgs([
            ("A付", "5", ["乳", "蛋类"]),
            ("A赠", "0", ["乳"]),
            ("B豆", "5", ["大豆"]),
        ])
        r3 = solve_panel(target3, pkgs3)
        self.assertEqual(r3["total_price"], 10)
        self.assertEqual(r3["optimal_count"], 2)
        self.assertEqual([s["name"] for s in r3["selections"]],
                         ["A付", "A赠", "B豆"])
        rows = {s["name"]: s for s in r3["selections"]}
        self.assertEqual(rows["A付"]["contribution"], ["乳", "蛋类"])
        self.assertEqual(rows["A赠"]["contribution"], [])
        self.assertEqual(rows["A赠"]["cumulative_covered"], ["乳", "蛋类"])
        self.assertEqual(rows["B豆"]["contribution"], ["大豆"])
        self.assertEqual(rows["B豆"]["cumulative_covered"],
                         ["乳", "大豆", "蛋类"])
        bc, bkey, bways = brute_force(target3, pkgs3)
        self.assertEqual(bways, 2)
        self.assertEqual(bkey, ("A付", "A赠", "B豆"))

    def test_positive_price_redundant_never_optimal(self):
        # 正价冗余套餐（加购 5 元，仅覆盖已被基础包含的乳）不得进入最低价，
        # 也不制造并列：最低价只有 (基础)
        target = ["乳", "蛋类"]
        pkgs = self._pkgs([
            ("基础", "10", ["乳", "蛋类"]),
            ("加购", "5", ["乳"]),
        ])
        r = solve_panel(target, pkgs)
        self.assertTrue(r["feasible"])
        self.assertEqual(r["total_price"], 10)
        self.assertEqual(r["optimal_count"], 1)
        self.assertEqual([s["name"] for s in r["selections"]], ["基础"])
        bc, bkey, bways = brute_force(target, pkgs)
        self.assertEqual(bc, Fraction("10"))
        self.assertEqual(bways, 1)
        self.assertEqual(bkey, ("基础",))

    def test_zero_redundant_dedup_among_zero_covers(self):
        # 三个 0 元套餐即可覆盖全部必须项：
        # 5 个 0 元覆盖组合 {A}、{B,C}、{A,B}、{A,C}、{A,B,C}（后三个含
        # 冗余 0 元选择），字典序最小为单元素的 ('A',)
        target = ["乳", "蛋类"]
        pkgs = self._pkgs([
            ("A", "0", ["乳", "蛋类"]),
            ("B", "0", ["乳"]),
            ("C", "0", ["蛋类"]),
        ])
        r = solve_panel(target, pkgs)
        self.assertEqual(r["total_price"], 0)
        self.assertEqual(r["optimal_count"], 5)
        self.assertEqual([s["name"] for s in r["selections"]], ["A"])
        bc, bkey, bways = brute_force(target, pkgs)
        self.assertEqual(bways, 5)
        self.assertEqual(bkey, ("A",))

    def test_matches_brute_force(self):
        import random
        random.seed(7)
        target = ["A", "B", "C", "D"]
        universe = ["A", "B", "C", "D", "X", "Y"]
        for trial in range(60):
            spec = []
            for i in range(random.randint(1, 7)):
                spec.append((f"P{i}", random.choice(["1", "2", "3", "0.5", "0"]),
                             random.sample(universe, random.randint(0, 4))))
            pkgs = self._pkgs(spec)
            r = solve_panel(target, pkgs)
            bc, bkey, bways = brute_force(target, pkgs)
            if bc is None:
                self.assertFalse(r["feasible"])
                # 缺口 = 没有任何套餐覆盖的必查项
                reach = set().union(*[set(p["allergens"]) for p in pkgs])
                self.assertEqual(r["uncovered"],
                                 sorted(set(target) - reach))
            else:
                self.assertTrue(r["feasible"])
                self.assertEqual(Fraction(str(r["total_price"])), bc)
                self.assertEqual(r["optimal_count"], bways)
                self.assertEqual(tuple(s["name"] for s in r["selections"]), bkey)

    def test_fuzz_zero_and_positive_redundant(self):
        # 高强度对照：价格池含 0 / 正价 / 精确小数，套餐数更多，确保
        # 0 元冗余计入并列、正价冗余不入最低价、字典序重建与暴力枚举一致
        import random
        random.seed(20260920)
        target = ["A", "B", "C"]
        universe = ["A", "B", "C", "X"]
        prices = ["0", "0", "1", "2", "0.3", "0.1", "5"]
        for trial in range(300):
            spec = []
            for i in range(random.randint(1, 9)):
                spec.append((f"P{i}", random.choice(prices),
                             random.sample(universe, random.randint(0, 3))))
            pkgs = self._pkgs(spec)
            r = solve_panel(target, pkgs)
            bc, bkey, bways = brute_force(target, pkgs)
            if bc is None:
                self.assertFalse(r["feasible"])
                continue
            self.assertTrue(r["feasible"])
            self.assertEqual(Fraction(str(r["total_price"])), bc)
            self.assertEqual(r["optimal_count"], bways)
            key = tuple(s["name"] for s in r["selections"])
            self.assertEqual(key, bkey)
            # 展示方案确实以最低总价覆盖，且空贡献套餐不改变累计覆盖
            covered = set()
            for s in r["selections"]:
                before = set(covered)
                covered |= set(s["contribution"])
                self.assertEqual(set(s["cumulative_covered"]), covered)
                self.assertTrue(set(s["contribution"]) <= set(target) - before)
            self.assertTrue(set(target) <= covered)
            self.assertEqual(sum((Fraction(str(s["price"]))
                                  for s in r["selections"]), Fraction(0)), bc)

    def test_empty_target(self):
        r = solve_panel([], self._pkgs([("A", "1", ["乳"])]))
        self.assertTrue(r["feasible"])
        self.assertEqual(r["total_price"], 0)
        self.assertEqual(r["selections"], [])
        self.assertEqual(r["optimal_count"], 1)

    def test_extra_allergens_reported(self):
        pkgs = self._pkgs([("A", "5", ["乳", "鱼类"])])
        r = solve_panel(["乳"], pkgs)
        self.assertTrue(r["feasible"])
        self.assertEqual(r["selections"][0]["extra_allergens"], ["鱼类"])
        self.assertEqual(r["packages"][0]["covers_required"], ["乳"])


class PreviewFlowTest(unittest.TestCase):
    """全链路：记录资格、两版取 B、缺口、需复核 / 缺失拒绝、只读。"""

    def setUp(self):
        db.DB_PATH = os.path.join(
            _DB_DIR, f"panel_{self.id().rsplit('.', 1)[-1]}.db")
        db.init_db()
        db.bulk_import(DICT_TEXT)

    def _fp(self, rid):
        with db._connect() as conn:
            return conn.execute(
                "SELECT dict_fingerprint FROM reviews WHERE id=?",
                (rid,)).fetchone()[0]

    def test_required_allergens_from_current_version(self):
        rid = make_review(TEXT_MILK, TEXT_B, name="两版")
        records = {r["id"]: r for r in list_panel_records()}
        self.assertEqual(records[rid]["source_version"], "新版配方")
        r = build_panel_preview(rid, "全包：99：麸质谷物，坚果，蛋类，芝麻")
        self.assertEqual(r["required_allergens"],
                         ["坚果", "芝麻", "蛋类", "麸质谷物"])
        self.assertTrue(r["feasible"])

    def test_feasible_plan_and_gap(self):
        rid = make_review(TEXT_MILK)
        r = build_panel_preview(rid, "乳包：10：乳\n基础：9.9：麸质谷物，蛋类")
        self.assertFalse(r["feasible"])
        self.assertEqual(r["total_price"], None)
        self.assertEqual(r["uncovered"], ["大豆"])
        r = build_panel_preview(
            rid, "乳包：10：乳\n基础：9.9：麸质谷物，蛋类\n大豆：5：大豆")
        self.assertTrue(r["feasible"])
        self.assertEqual(r["total_price"], 24.9)
        self.assertEqual(r["optimal_count"], 1)

    def test_zero_redundant_tie_in_preview(self):
        # 全链路：0 元加赠覆盖必须项「麸质谷物」，加入 14.9 基础包后不新增
        # 覆盖，仍作为第二个最低价组合计数并按字典序展示在最前
        rid = make_review("配料表：小麦粉，鸡蛋", name="两必项")
        r = build_panel_preview(
            rid,
            "基础包：14.9：麸质谷物，蛋类\n加赠包：0：麸质谷物")
        self.assertTrue(r["feasible"])
        self.assertEqual(r["required_allergens"], ["蛋类", "麸质谷物"])
        self.assertEqual(r["total_price"], 14.9)
        self.assertEqual(r["optimal_count"], 2)
        self.assertEqual([s["name"] for s in r["selections"]],
                         ["加赠包", "基础包"])
        self.assertEqual(r["selections"][0]["contribution"], ["麸质谷物"])
        self.assertEqual(r["selections"][1]["contribution"], ["蛋类"])
        self.assertEqual(r["selections"][1]["cumulative_covered"],
                         ["蛋类", "麸质谷物"])
        # 两个套餐都覆盖至少一个必须项，不属于 unused
        self.assertEqual(r["unused_packages"], [])

    def test_empty_packages_rejected(self):
        rid = make_review(TEXT_MILK)
        with self.assertRaises(PanelError):
            build_panel_preview(rid, "# 只有注释\n")

    def test_missing_and_invalid_record_blocked(self):
        with self.assertRaises(PanelError) as ctx:
            build_panel_preview(999999, "A：1：乳")
        self.assertEqual(ctx.exception.problems[0]["reason"], "missing")
        with self.assertRaises(PanelError) as ctx:
            build_panel_preview("x", "A：1：乳")
        self.assertEqual(ctx.exception.problems[0]["reason"], "invalid")

    def test_stale_record_blocked_until_rechecked(self):
        rid = make_review(TEXT_MILK)
        db.upsert_entry("乳清粉", ["乳", "花生"])  # 映射变更 → 需复核
        self.assertTrue(db.get_review(rid)["needs_recheck"])
        with self.assertRaises(PanelError) as ctx:
            build_panel_preview(rid, "A：1：乳")
        [p] = ctx.exception.problems
        self.assertEqual(p["reason"], "stale")
        self.assertIn("需复核", p["message"])
        records = {r["id"]: r for r in list_panel_records()}
        self.assertFalse(records[rid]["eligible"])
        # 复核后恢复
        outcome = build_recheck(db.get_review(rid))
        db.update_review_result(rid, outcome["new_result"],
                                outcome["fingerprint"])
        self.assertFalse(db.get_review(rid)["needs_recheck"])
        r = build_panel_preview(rid, "全包：1：乳，花生，大豆，蛋类，麸质谷物")
        self.assertTrue(r["feasible"])

    def test_legacy_record_blocked(self):
        rid = make_review(TEXT_MILK, with_fingerprint=False)
        with self.assertRaises(PanelError) as ctx:
            build_panel_preview(rid, "A：1：乳")
        self.assertEqual(ctx.exception.problems[0]["reason"], "legacy")

    def test_deleted_record_blocked(self):
        rid = make_review(TEXT_MILK)
        db.delete_review(rid)
        with self.assertRaises(PanelError) as ctx:
            build_panel_preview(rid, "A：1：乳")
        self.assertEqual(ctx.exception.problems[0]["reason"], "missing")

    def test_readonly(self):
        rid = make_review(TEXT_MILK)
        before = db.get_review(rid)
        count = len(db.list_reviews())
        build_panel_preview(rid, "全包：99：麸质谷物，乳，大豆，蛋类")
        build_panel_preview(rid, "乳：1：乳")  # 有缺口的无解计算也不写库
        self.assertEqual(db.get_review(rid), before)
        self.assertEqual(self._fp(rid), before["dict_fingerprint"])
        self.assertEqual(len(db.list_reviews()), count)


if __name__ == "__main__":
    unittest.main()
