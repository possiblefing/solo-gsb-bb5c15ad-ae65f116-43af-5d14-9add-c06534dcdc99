"""原料别名有向图测试（纯逻辑，不依赖 FastAPI / 数据库）。

覆盖：
- 「别名：标准名」解析（中英文冒号、注释空行、格式错误行）；
- 整批原子校验：自环、任意长度环（含与既有别名连成的环）、
  同一别名多目标；错误返回冲突行，环返回从最早冲突行展开的最短环路；
- 同目标重复行幂等折叠、合法链路整批通过；
- AliasResolver 任意长度链解析到最终标准名；
- resolve_preview 的解析链、改写标记、字典命中与过敏原。
"""
import unittest

from app.aliases import (AliasResolver, parse_alias_lines,
                         resolve_preview, validate_alias_batch)
from app.analysis import normalize_name


def _existing(*pairs):
    return {normalize_name(a): (a, t) for a, t in pairs}


class ParseLinesTest(unittest.TestCase):
    def test_separators_comments_blanks(self):
        rows, errors = parse_alias_lines(
            "乳清粉：奶粉\nwhey: milk powder\n# 注释\n\n  脱脂奶粉 ： 奶粉 ")
        self.assertFalse(errors)
        self.assertEqual(
            [(alias, target) for _, alias, target in rows],
            [("乳清粉", "奶粉"), ("whey", "milk powder"), ("脱脂奶粉", "奶粉")])

    def test_format_errors(self):
        rows, errors = parse_alias_lines("奶粉\n：x\na：\n  :  ")
        self.assertEqual(rows, [])
        self.assertEqual([e["line"] for e in errors], [1, 2, 3, 4])
        self.assertTrue(all(e["type"] == "format" for e in errors))


class ValidateBatchTest(unittest.TestCase):
    def test_valid_chain_and_dedup(self):
        out = validate_alias_batch(
            "乳清粉：奶粉\n奶粉：乳基\n乳清粉：奶粉", {})
        self.assertEqual(out["errors"], [])
        # 归一化后同别名同目标只保留一条（展示名取首行）
        self.assertEqual(out["entries"],
                         [("乳清粉", "奶粉"), ("奶粉", "乳基")])

    def test_self_loop_rejected(self):
        out = validate_alias_batch("奶粉：奶粉\n乳清粉：奶粉", {})
        self.assertEqual(len(out["errors"]), 1)
        err = out["errors"][0]
        self.assertEqual(err["type"], "self_loop")
        self.assertEqual(err["lines"], [1])
        self.assertEqual(out["entries"], [])

    def test_multi_target_with_conflict_lines(self):
        out = validate_alias_batch("a：x\nb：y\na：z", {})
        types = [e["type"] for e in out["errors"]]
        self.assertEqual(types, ["multi_target"])
        err = out["errors"][0]
        self.assertEqual(err["lines"], [1, 3])
        self.assertEqual(err["alias"], "a")

    def test_same_target_repeated_is_not_multi_target(self):
        out = validate_alias_batch("a：x\na:x", {})
        self.assertEqual(out["errors"], [])

    def test_normalized_case_space_collision_is_same_alias(self):
        # 归一化后同名（大小写 / 空白差异）指向不同目标仍算多目标
        out = validate_alias_batch("Milk Powder：x\nmilkpowder：y", {})
        self.assertEqual([e["type"] for e in out["errors"]], ["multi_target"])

    def test_cycle_any_length_returns_shortest_loop(self):
        out = validate_alias_batch("a：b\nb：c\nc：a", {})
        self.assertEqual(len(out["errors"]), 1)
        err = out["errors"][0]
        self.assertEqual(err["type"], "cycle")
        # 功能图中环上节点的唯一简单环即最短环路，首尾闭合表示
        self.assertEqual(err["cycle"], ["a", "b", "c", "a"])
        self.assertEqual(err["lines"], [1, 2, 3])

    def test_cycle_starts_from_earliest_conflict_line(self):
        out = validate_alias_batch("b：a\na：b", {})
        err = out["errors"][0]
        self.assertEqual(err["cycle"], ["b", "a", "b"])  # 最早冲突行为第 1 行
        self.assertEqual(err["lines"], [1, 2])

    def test_cycle_formed_with_existing_aliases(self):
        existing = _existing(("b", "c"), ("c", "d"))
        out = validate_alias_batch("d：b", existing)
        self.assertEqual(len(out["errors"]), 1)
        self.assertEqual(out["errors"][0]["type"], "cycle")
        self.assertEqual(out["errors"][0]["cycle"], ["d", "b", "c", "d"])
        self.assertEqual(out["errors"][0]["lines"], [1])

    def test_preexisting_cycle_unrelated_to_batch_ignored(self):
        # 既有脏数据成环但本批条目不在环上：不阻断合法新条目
        existing = _existing(("b", "c"), ("c", "b"))
        out = validate_alias_batch("x：y", existing)
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["entries"], [("x", "y")])

    def test_atomic_batch_collects_multiple_errors(self):
        out = validate_alias_batch(
            "奶粉：奶粉\na：x\na：y\nb：c\nc：b\n坏行", {})
        types = [e["type"] for e in out["errors"]]
        self.assertIn("self_loop", types)
        self.assertIn("multi_target", types)
        self.assertIn("cycle", types)
        self.assertIn("format", types)
        self.assertEqual(out["entries"], [])  # 任何错误都拒绝整批

    def test_error_ordering_is_by_line(self):
        out = validate_alias_batch("b：a\na：b\nx：x", {})
        self.assertEqual([e["type"] for e in out["errors"]],
                         ["cycle", "self_loop"])


class ResolverTest(unittest.TestCase):
    def setUp(self):
        self.resolver = AliasResolver(
            {normalize_name(a): normalize_name(t)
             for a, t in [("乳清粉", "奶粉"), ("奶粉", "乳基"),
                          ("脱脂奶粉", "奶粉")]})

    def test_resolve_chain_to_standard(self):
        self.assertEqual(self.resolver.resolve(normalize_name("乳清粉")),
                         normalize_name("乳基"))
        self.assertEqual(self.resolver.resolve(normalize_name("脱脂奶粉")),
                         normalize_name("乳基"))
        self.assertEqual(self.resolver.resolve(normalize_name("鸡蛋")),
                         normalize_name("鸡蛋"))

    def test_chain_includes_head_and_tail(self):
        self.assertEqual(self.resolver.chain(normalize_name("乳清粉")),
                         [normalize_name(x) for x in ("乳清粉", "奶粉", "乳基")])
        self.assertEqual(self.resolver.chain(normalize_name("鸡蛋")),
                         [normalize_name("鸡蛋")])

    def test_resolve_is_memoized(self):
        first = self.resolver.resolve(normalize_name("乳清粉"))
        second = self.resolver.resolve(normalize_name("奶粉"))
        self.assertEqual(first, second)
        self.assertIn(normalize_name("奶粉"), self.resolver._cache)


class ResolvePreviewTest(unittest.TestCase):
    def setUp(self):
        self.edges = {normalize_name(a): normalize_name(t)
                      for a, t in [("乳清粉", "奶粉"), ("奶粉", "乳基")]}
        self.dictionary = {normalize_name("乳基"): {"乳"},
                           normalize_name("鸡蛋"): {"蛋类"}}

    def test_preview_resolves_and_marks_dict(self):
        items = resolve_preview(
            ["乳清粉", "奶粉", "鸡蛋", "神秘物"], self.edges, self.dictionary)
        by_name = {it["name"]: it for it in items}
        whey = by_name["乳清粉"]
        self.assertTrue(whey["resolved"])
        self.assertEqual(whey["chain"], ["乳清粉", "奶粉", "乳基"])
        self.assertEqual(whey["standard"], "乳基")
        self.assertTrue(whey["in_dict"])
        self.assertEqual(whey["allergens"], ["乳"])
        self.assertFalse(by_name["鸡蛋"]["resolved"])
        self.assertTrue(by_name["鸡蛋"]["in_dict"])
        self.assertFalse(by_name["神秘物"]["in_dict"])
        self.assertEqual(by_name["神秘物"]["allergens"], [])

    def test_preview_dedup_by_normalized_name(self):
        items = resolve_preview(
            ["乳清粉", " 乳清粉 ", "乳清粉"], self.edges, self.dictionary)
        self.assertEqual(len(items), 1)

    def test_preview_marks_leaf_vs_compound(self):
        # 复合父配料 is_leaf=False，叶子 is_leaf=True
        items = resolve_preview(
            ["巧克力酱", "乳清粉"], self.edges, self.dictionary,
            leaf_keys={normalize_name("乳清粉")})
        by_name = {it["name"]: it for it in items}
        self.assertFalse(by_name["巧克力酱"]["is_leaf"])
        self.assertTrue(by_name["乳清粉"]["is_leaf"])

    def test_preview_uses_display_map(self):
        displays = {normalize_name("Whey"): "Whey",
                    normalize_name("Milk Base"): "Milk Base"}
        edges = {normalize_name("Whey"): normalize_name("Milk Base")}
        items = resolve_preview(["Whey"], edges, {}, displays)
        self.assertEqual(items[0]["chain"], ["Whey", "Milk Base"])
        self.assertEqual(items[0]["standard"], "Milk Base")


if __name__ == "__main__":
    unittest.main()
