"""目标过敏原最小改配方案测试（不依赖 FastAPI，走 db + review + reformulation 全链路）。

覆盖：
- 复用审查记录当前配方（两版取标签核对对象，即新版配方）的配方树；
- 目标过敏原全部来源路径（自身标注节点）与候选替换树的构造；
- 树形动态规划：选祖先阻断子树、不得再选后代，最低总成本覆盖全部来源；
- 同成本按节点路径序列字典序取最小（对照暴力枚举）与并列方案数；
- 未填写节点视为不可替换、无成本来源无解并逐条列出；
- 精确有理数（0.1 + 0.2 == 0.3）、成本/路径校验；
- 缺失 / 需复核记录禁止选择；全程只读、不改写审查记录。
"""
import os
import tempfile
import unittest
from itertools import combinations
from fractions import Fraction

_DB_DIR = tempfile.mkdtemp(prefix="allergen-reformulation-")
os.environ["DB_PATH"] = os.path.join(_DB_DIR, "reformulation.db")

from app import db  # noqa: E402
from app.recheck import build_recheck  # noqa: E402
from app.reformulation import (ReformulationError,  # noqa: E402
                               _build_candidate_tree, build_reformulation_plan,
                               get_reformulation_sources,
                               list_reformulation_records)
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
# 乳来源：巧克力酱›奶粉、…›奶粉›乳清粉、…›奶粉›乳糖（奶粉自身也是来源）
TEXT_MILK = "配料表：小麦粉，白砂糖，巧克力酱（可可脂，奶粉（乳清粉，乳糖），大豆卵磷脂），鸡蛋，食盐"
# 乳来源：巧克力酱›奶粉、巧克力酱›奶粉›乳清粉、巧克力酱›乳清粉（巧克力酱不是来源）
TEXT_BRANCH = "配料表：巧克力酱（可可脂，奶粉（乳清粉），乳清粉）"
TEXT_B = "配料表：小麦粉，椰子粉，鸡蛋，芝麻，食盐"


def make_review(text_a, text_b=None, name="改配测试", with_fingerprint=True):
    result, fp = analyze(text_a=text_a, text_b=text_b, label_a="现版配方",
                         label_b="新版配方", declared=[])
    return db.create_review(
        name=name, declared=[], text_a=text_a, text_b=text_b,
        label_a="现版配方", label_b="新版配方", result=result,
        dict_fingerprint=fp if with_fingerprint else None)


def brute_force(sources, prices):
    """暴力枚举候选节点的全部反链，返回 (最低成本 Fraction|None, 最小键, 方案数, 选中路径)。

    覆盖定义：每条来源路径的某个前缀（含自身）被选中。无解返回 (None, None, 0, [])。
    """
    tree = _build_candidate_tree(sources)
    paths = []

    def collect(node):
        if node.path:
            paths.append(node.path)
        for c in node.children:
            collect(c)

    collect(tree)
    source_tuples = [tuple(p) for p in sources]
    priced = {p: Fraction(str(v)) for p, v in prices.items()}

    def is_antichain(sel):
        for a, b in combinations(sel, 2):
            if a[:len(b)] == b or b[:len(a)] == a:
                return False
        return True

    def covers(sel):
        for src in source_tuples:
            if not any(src[:len(p)] == p for p in sel):
                return False
        return True

    best, best_key, ways, best_sel = None, None, 0, []
    for r in range(len(paths) + 1):
        for sel in combinations(paths, r):
            sel = set(sel)
            if any(p not in priced for p in sel):
                continue  # 未填写成本的节点不可替换
            if not is_antichain(sel) or not covers(sel):
                continue
            cost = sum((priced[p] for p in sel), Fraction(0))
            key = tuple(sorted(sel))
            if best is None or cost < best:
                best, best_key, ways, best_sel = cost, key, 1, sel
            elif cost == best:
                ways += 1
                if key < best_key:
                    best_key, best_sel = key, sel
    if best is None:
        return None, None, 0, []
    return best, best_key, ways, sorted(best_sel)


def sources_of(rid, allergen="乳"):
    return get_reformulation_sources(rid, allergen)["sources"]


def plan_paths(result):
    return [tuple(s["path"]) for s in result["selections"]]


class TreeDPTest(unittest.TestCase):
    """树形动态规划纯计算：最低成本、反链阻断、并列字典序与无解（对照暴力枚举）。"""

    def _check(self, sources, price_map):
        prices = [{"path": list(p), "cost": v} for p, v in price_map.items()]
        # 直接复用编排函数需要记录，这里走内部树 + 暴力比对：
        tree = _build_candidate_tree(sources)
        from app.reformulation import _solve, _validate_costs
        priced = _validate_costs(prices, tree)
        state = _solve(tree, priced)
        bc, bk, ways, bsel = brute_force(sources,
                                         {tuple(item["path"]): item["cost"]
                                          for item in prices})
        return state, (bc, bk, ways, bsel)

    def test_ancestor_blocks_subtree(self):
        sources = [["酱", "奶粉"], ["酱", "奶粉", "清"], ["酱", "奶粉", "糖"],
                   ["酱", "直"]]
        price = {("酱",): 8, ("酱", "奶粉"): 5, ("酱", "奶粉", "清"): 3,
                 ("酱", "奶粉", "糖"): 4, ("酱", "直"): 4}
        state, ref = self._check(sources, price)
        self.assertEqual(state[0], ref[0])
        self.assertEqual(state[1], ref[1])
        self.assertEqual(state[2], ref[2])
        # 奶粉 5 + 直 4 = 9 优于整树 8? 否：整树 8 最优
        self.assertEqual(state[0], Fraction(8))
        self.assertEqual(state[1], (("酱",),))

    def test_own_source_cannot_be_covered_by_descendants(self):
        # 奶粉自身是来源：只替换叶子清、糖无法阻断「酱›奶粉」这条路径
        sources = [["酱", "奶粉"], ["酱", "奶粉", "清"], ["酱", "奶粉", "糖"]]
        state, ref = self._check(
            sources, {("酱", "奶粉", "清"): 1, ("酱", "奶粉", "糖"): 1,
                      ("酱", "奶粉"): 10})
        self.assertEqual(state[0], Fraction(10))
        self.assertEqual(state[1], (("酱", "奶粉"),))
        self.assertEqual(state, (ref[0], ref[1], ref[2]))

    def test_tie_break_and_count(self):
        # 酱(9) 与 奶粉(5)+直(4)=9 并列：键 (酱,) 字典序小于 (…奶粉,…直)
        sources = [["酱", "奶粉"], ["酱", "奶粉", "清"], ["酱", "直"]]
        price = {("酱",): 9, ("酱", "奶粉"): 5, ("酱", "奶粉", "清"): 3,
                 ("酱", "直"): 4}
        state, ref = self._check(sources, price)
        self.assertEqual(state[0], Fraction(9))
        self.assertEqual(state[2], 2)
        self.assertEqual(state[1], (("酱",),))
        self.assertEqual((state[0], state[1], state[2]), ref[:3])

    def test_matches_brute_force_mixed(self):
        import random
        random.seed(42)
        sources = [["酱", "奶粉"], ["酱", "奶粉", "清"], ["酱", "奶粉", "糖"],
                   ["酱", "直"], ["小麦"]]
        candidates = {tuple(p[:i]) for p in sources for i in range(1, len(p) + 1)}
        for trial in range(40):
            price = {p: random.choice([1, 3, 5, 8, 13]) for p in candidates}
            state, ref = self._check(sources, price)
            if ref[0] is None:
                self.assertIsNone(state)
            else:
                self.assertEqual((state[0], state[1], state[2]), ref[:3])

    def test_exact_fraction_tie(self):
        # 0.1 + 0.2 与 0.3 应判为并列（Fraction 精确比较）
        sources = [["酱", "奶粉"], ["酱", "奶粉", "清"], ["酱", "直"]]
        price = {("酱",): 0.3, ("酱", "奶粉"): 0.1,
                 ("酱", "奶粉", "清"): 0.1, ("酱", "直"): 0.2}
        state, ref = self._check(sources, price)
        self.assertEqual(float(state[0]), 0.3)
        self.assertEqual(state[2], 2)
        self.assertEqual((state[0], state[1], state[2]), ref[:3])


class PreviewFlowTest(unittest.TestCase):
    """全链路：来源路径、候选树、成本校验、无解与只读性。"""

    def setUp(self):
        db.DB_PATH = os.path.join(
            _DB_DIR, f"reformulation_{self.id().rsplit('.', 1)[-1]}.db")
        db.init_db()
        db.bulk_import(DICT_TEXT)

    def _fp(self, rid):
        with db._connect() as conn:
            return conn.execute(
                "SELECT dict_fingerprint FROM reviews WHERE id=?",
                (rid,)).fetchone()[0]

    def test_sources_and_candidate_tree(self):
        rid = make_review(TEXT_MILK)
        s = get_reformulation_sources(rid, "乳")
        self.assertEqual(s["sources"], [
            ["巧克力酱", "奶粉"],
            ["巧克力酱", "奶粉", "乳清粉"],
            ["巧克力酱", "奶粉", "乳糖"],
        ])
        paths = {tuple(n["path"]): n["is_source"]
                 for n in s["candidate_nodes"]}
        self.assertIn(("巧克力酱",), paths)            # 前缀也是候选替换点
        self.assertFalse(paths[("巧克力酱",)])
        self.assertTrue(paths[("巧克力酱", "奶粉")])
        self.assertTrue(paths[("巧克力酱", "奶粉", "乳清粉")])
        self.assertEqual(s["review"]["source_version"], "现版配方")

    def test_two_versions_use_version_b(self):
        rid = make_review(TEXT_MILK, TEXT_B, name="两版")
        records = {r["id"]: r for r in list_reformulation_records()}
        self.assertEqual(records[rid]["source_version"], "新版配方")
        # 乳仅在 A 版：当前配方（B）取树时不得作为目标过敏原
        with self.assertRaises(ReformulationError):
            get_reformulation_sources(rid, "乳")
        # 坚果仅在 B 版（顶层椰子粉），来源为 椰子粉
        s = get_reformulation_sources(rid, "坚果")
        self.assertEqual(s["sources"], [["椰子粉"]])

    def test_ancestor_cheapest_plan_and_blocks(self):
        rid = make_review(TEXT_MILK)
        r = build_reformulation_plan(rid, "乳", [
            {"path": ["巧克力酱", "奶粉"], "cost": 100},
            {"path": ["巧克力酱", "奶粉", "乳清粉"], "cost": 60},
            {"path": ["巧克力酱", "奶粉", "乳糖"], "cost": 60},
        ])
        self.assertTrue(r["feasible"])
        self.assertEqual(r["total_cost"], 100)
        self.assertEqual(r["optimal_count"], 1)
        [sel] = r["selections"]
        self.assertEqual(sel["path"], ["巧克力酱", "奶粉"])
        self.assertEqual(sel["blocked_count"], 3)
        self.assertEqual(sel["blocked_sources"], r["sources"])

    def test_own_source_forces_ancestor_or_self(self):
        # 仅给两个叶子填成本：奶粉自身来源无法覆盖 → 无解，列出全部不可覆盖来源
        rid = make_review(TEXT_MILK)
        r = build_reformulation_plan(rid, "乳", [
            {"path": ["巧克力酱", "奶粉", "乳清粉"], "cost": 1},
            {"path": ["巧克力酱", "奶粉", "乳糖"], "cost": 1},
        ])
        self.assertFalse(r["feasible"])
        self.assertEqual(r["total_cost"], None)
        self.assertEqual(r["optimal_count"], 0)
        self.assertEqual(r["uncoverable_sources"], [["巧克力酱", "奶粉"]])

    def test_non_source_parent_can_split_to_leaves(self):
        rid = make_review(TEXT_BRANCH)
        sources = sources_of(rid)
        self.assertEqual(sources, [
            ["巧克力酱", "乳清粉"], ["巧克力酱", "奶粉"],
            ["巧克力酱", "奶粉", "乳清粉"]])
        # 巧克力酱自身不是来源：奶粉分支必须选奶粉（自身来源），
        # 直挂分支选叶子乳清粉；5 + 4 = 9，优于整树 10
        r = build_reformulation_plan(rid, "乳", [
            {"path": ["巧克力酱"], "cost": 10},
            {"path": ["巧克力酱", "奶粉"], "cost": 5},
            {"path": ["巧克力酱", "乳清粉"], "cost": 4},
        ])
        self.assertTrue(r["feasible"])
        self.assertEqual(r["total_cost"], 9)
        self.assertEqual(plan_paths(r),
                         [("巧克力酱", "乳清粉"), ("巧克力酱", "奶粉")])
        # 与暴力枚举完全一致
        price = {("巧克力酱",): 10, ("巧克力酱", "奶粉"): 5,
                 ("巧克力酱", "乳清粉"): 4}
        bc, bk, ways, _ = brute_force(sources, price)
        self.assertEqual(Fraction(str(r["total_cost"])), bc)
        self.assertEqual(r["optimal_count"], ways)
        self.assertEqual(tuple(plan_paths(r)), bk)

    def test_no_cost_at_all_is_infeasible(self):
        rid = make_review(TEXT_MILK)
        r = build_reformulation_plan(rid, "乳", [])
        self.assertFalse(r["feasible"])
        self.assertEqual(len(r["uncoverable_sources"]), 3)

    def test_invalid_cost_and_path_rejected(self):
        rid = make_review(TEXT_MILK)
        with self.assertRaises(ReformulationError):
            build_reformulation_plan(
                rid, "乳", [{"path": ["巧克力酱", "奶粉"], "cost": -1}])
        with self.assertRaises(ReformulationError):
            build_reformulation_plan(
                rid, "乳", [{"path": ["巧克力酱", "奶粉"], "cost": "abc"}])
        with self.assertRaises(ReformulationError):
            build_reformulation_plan(
                rid, "乳", [{"path": ["巧克力酱", "奶粉"], "cost": True}])
        # 路径不在来源路径上（可可脂是配方节点但非乳来源）
        with self.assertRaises(ReformulationError):
            build_reformulation_plan(
                rid, "乳", [{"path": ["巧克力酱", "可可脂"], "cost": 1}])
        # 路径不存在于配方树
        with self.assertRaises(ReformulationError):
            build_reformulation_plan(
                rid, "乳", [{"path": ["不存在"], "cost": 1}])
        # 重复填写同一路径
        with self.assertRaises(ReformulationError):
            build_reformulation_plan(rid, "乳", [
                {"path": ["巧克力酱", "奶粉"], "cost": 1},
                {"path": ["巧克力酱", "奶粉"], "cost": 2}])
        # 空路径
        with self.assertRaises(ReformulationError):
            build_reformulation_plan(rid, "乳", [{"path": [], "cost": 1}])

    def test_bad_allergen_rejected(self):
        rid = make_review(TEXT_MILK)
        with self.assertRaises(ReformulationError):
            get_reformulation_sources(rid, "不存在的过敏原")
        with self.assertRaises(ReformulationError):
            get_reformulation_sources(rid, "  ")

    def test_missing_record_blocked(self):
        with self.assertRaises(ReformulationError) as ctx:
            get_reformulation_sources(999999, "乳")
        [p] = ctx.exception.problems
        self.assertEqual(p["reason"], "missing")
        self.assertIn("不存在", p["message"])

    def test_invalid_record_id_blocked(self):
        with self.assertRaises(ReformulationError) as ctx:
            build_reformulation_plan("x", "乳", [])
        [p] = ctx.exception.problems
        self.assertEqual(p["reason"], "invalid")

    def test_stale_record_blocked_until_rechecked(self):
        rid = make_review(TEXT_MILK)
        db.upsert_entry("乳清粉", ["乳", "花生"])  # 相关原料映射变更 → 需复核
        self.assertTrue(db.get_review(rid)["needs_recheck"])
        with self.assertRaises(ReformulationError) as ctx:
            get_reformulation_sources(rid, "乳")
        [p] = ctx.exception.problems
        self.assertEqual(p["reason"], "stale")
        self.assertEqual(p["id"], rid)
        self.assertIn("需复核", p["message"])
        records = {r["id"]: r for r in list_reformulation_records()}
        self.assertFalse(records[rid]["eligible"])
        self.assertIn("需复核", records[rid]["blocked_reason"])
        # 完成字典时效复核后恢复可选择
        outcome = build_recheck(db.get_review(rid))
        db.update_review_result(rid, outcome["new_result"],
                                outcome["fingerprint"])
        self.assertFalse(db.get_review(rid)["needs_recheck"])
        s = get_reformulation_sources(rid, "花生")
        self.assertTrue(s["sources"])

    def test_legacy_record_blocked(self):
        rid = make_review(TEXT_MILK, with_fingerprint=False)
        with self.assertRaises(ReformulationError) as ctx:
            get_reformulation_sources(rid, "乳")
        [p] = ctx.exception.problems
        self.assertEqual(p["reason"], "legacy")
        self.assertIn("指纹", p["message"])

    def test_readonly(self):
        rid = make_review(TEXT_MILK)
        before = db.get_review(rid)
        count = len(db.list_reviews())
        get_reformulation_sources(rid, "乳")
        build_reformulation_plan(rid, "乳", [
            {"path": ["巧克力酱", "奶粉"], "cost": 100},
            {"path": ["巧克力酱", "奶粉", "乳清粉"], "cost": 60},
            {"path": ["巧克力酱", "奶粉", "乳糖"], "cost": 60}])
        # 无解路径同样不得写库
        build_reformulation_plan(rid, "乳", [])
        self.assertEqual(db.get_review(rid), before)
        self.assertEqual(self._fp(rid), before["dict_fingerprint"])
        self.assertEqual(len(db.list_reviews()), count)


if __name__ == "__main__":
    unittest.main()
