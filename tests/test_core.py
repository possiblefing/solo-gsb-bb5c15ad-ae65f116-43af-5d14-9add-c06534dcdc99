import unittest

from app.analysis import (attach_allergens, collect_allergens, compare_label,
                          diff_allergens, diff_ingredients,
                          missing_allergen_sources, normalize_name)
from app.parser import parse_recipe


def lookup(d):
    return {normalize_name(k): set(v) for k, v in d.items()}


class ParserTest(unittest.TestCase):
    def test_nested_parens(self):
        tree = parse_recipe(
            "配料表：小麦粉，巧克力酱（可可脂，奶粉（乳清粉，乳糖），白砂糖），食盐")
        self.assertEqual([n.name for n in tree], ["小麦粉", "巧克力酱", "食盐"])
        choco = tree[1]
        self.assertEqual([c.name for c in choco.children],
                         ["可可脂", "奶粉", "白砂糖"])
        milk = choco.children[1]
        self.assertEqual([c.name for c in milk.children], ["乳清粉", "乳糖"])

    def test_english_parens_and_commas(self):
        tree = parse_recipe("flour, sauce(milk, sugar)")
        self.assertEqual([n.name for n in tree], ["flour", "sauce"])
        self.assertEqual([c.name for c in tree[1].children], ["milk", "sugar"])

    def test_anonymous_group(self):
        tree = parse_recipe("小麦粉，（甲，乙），食盐")
        self.assertEqual([n.name for n in tree], ["小麦粉", "甲", "乙", "食盐"])

    def test_empty_and_prefix(self):
        self.assertEqual(parse_recipe(""), [])
        self.assertEqual(parse_recipe("Ingredients: flour, sugar")[0].name, "flour")

    def test_unbalanced_paren_tolerated(self):
        tree = parse_recipe("小麦粉，酱（甲，乙")
        self.assertEqual(len(tree), 2)
        self.assertEqual([c.name for c in tree[1].children], ["甲", "乙"])


class AnalysisTest(unittest.TestCase):
    def setUp(self):
        self.dictionary = lookup({
            "小麦粉": ["麸质谷物"],
            "乳清粉": ["乳"],
            "乳糖": ["乳"],
            "可可脂": [],
            "大豆卵磷脂": ["大豆"],
        })

    def test_propagation_through_levels(self):
        tree = parse_recipe("小麦粉，巧克力酱（可可脂，奶粉（乳清粉，乳糖），大豆卵磷脂）")
        attach_allergens(tree, self.dictionary)
        choco = tree[1]
        self.assertEqual(choco.total_allergens, {"乳", "大豆"})
        self.assertEqual(choco.own_allergens, set())  # 复合原料自身无标注
        self.assertEqual(collect_allergens(tree), {"乳", "大豆", "麸质谷物"})

    def test_unknown_leaf_flagged(self):
        tree = parse_recipe("小麦粉，神秘香精")
        attach_allergens(tree, self.dictionary)
        self.assertFalse(tree[0].unknown)
        self.assertTrue(tree[1].unknown)

    def test_compound_not_flagged_unknown(self):
        tree = parse_recipe("酱（乳清粉）")
        attach_allergens(tree, self.dictionary)
        self.assertFalse(tree[0].unknown)  # 复合原料不提示未识别
        self.assertEqual(tree[0].total_allergens, {"乳"})

    def test_compare_label(self):
        res = compare_label({"乳", "大豆"}, {"乳", "花生"})
        self.assertEqual(res["missing"], ["大豆"])
        self.assertEqual(res["over_reported"], ["花生"])
        self.assertEqual(res["matched"], ["乳"])

    def test_diff_allergens(self):
        d = diff_allergens({"乳", "大豆"}, {"乳", "芝麻"})
        self.assertEqual(d["added"], ["芝麻"])
        self.assertEqual(d["removed"], ["大豆"])
        self.assertEqual(d["unchanged"], ["乳"])

    def test_diff_ingredients(self):
        dictionary = lookup({"奶粉": ["乳"], "芝麻": ["芝麻"], "小麦粉": ["麸质谷物"]})
        tree_a = parse_recipe("小麦粉，酱（奶粉）")
        tree_b = parse_recipe("小麦粉，酱（芝麻），新配料（奶粉）")
        attach_allergens(tree_a, dictionary)
        attach_allergens(tree_b, dictionary)
        changes = diff_ingredients(tree_a, tree_b)
        by_name = {c["ingredient"]: c for c in changes}
        self.assertEqual(by_name["酱"]["change"], "modified")
        self.assertEqual(by_name["酱"]["added"], ["芝麻"])
        self.assertEqual(by_name["酱"]["removed"], ["乳"])
        self.assertEqual(by_name["新配料"]["change"], "added")


def tree_dicts(text, dictionary):
    tree = parse_recipe(text)
    attach_allergens(tree, dictionary)
    return [n.to_dict() for n in tree]


class MissingSourcesTest(unittest.TestCase):
    """遗漏来源追踪：多层嵌套路径、同名分支不合并、自身标注/子配料传播。"""

    def setUp(self):
        self.dictionary = lookup({
            "小麦粉": ["麸质谷物"],
            "巧克力酱": [],
            "奶粉": [],
            "乳清粉": ["乳"],
            "乳糖": ["乳"],
            "大豆卵磷脂": ["大豆"],
            "可可脂": [],
        })

    def _sources(self, text, allergens=("乳",)):
        return missing_allergen_sources(tree_dicts(text, self.dictionary), allergens)

    def test_deeply_nested_path(self):
        src = self._sources(
            "小麦粉，巧克力酱（可可脂，奶粉（乳清粉，乳糖），大豆卵磷脂）")
        # 多层嵌套路径完整保留，叶子（实际自身标注者）收尾；按路径整体稳定排序
        self.assertEqual(src["乳"], [
            ["巧克力酱", "奶粉", "乳清粉"],
            ["巧克力酱", "奶粉", "乳糖"],
        ])
        src_all = self._sources(
            "小麦粉，巧克力酱（可可脂，奶粉（乳清粉，乳糖），大豆卵磷脂）",
            ("乳", "大豆"))
        self.assertEqual(src_all["大豆"], [["巧克力酱", "大豆卵磷脂"]])

    def test_same_named_ingredient_in_different_branches_kept_apart(self):
        # “奶粉”分别出现在巧克力酱与夹心酱两个分支下 —— 路径不得合并
        text = ("巧克力酱（奶粉（乳清粉）），"
                "夹心酱（奶粉（乳清粉），白砂糖），乳清粉")
        src = self._sources(text)
        self.assertEqual(src["乳"], [
            ["乳清粉"],
            ["夹心酱", "奶粉", "乳清粉"],
            ["巧克力酱", "奶粉", "乳清粉"],
        ])

    def test_own_annotation_and_child_propagation_both_traced(self):
        dictionary = lookup({
            "复合奶基": ["乳"],          # 父节点自身标注
            "乳清粉": ["乳"],            # 子配料携带（向上传播）
        })
        tree = tree_dicts("复合奶基（乳清粉）", dictionary)
        src = missing_allergen_sources(tree, ["乳"])
        # 自身标注 -> 到父节点为止；子配料传播 -> 深入到叶子，两条路径都保留
        self.assertEqual(src["乳"], [
            ["复合奶基"],
            ["复合奶基", "乳清粉"],
        ])

    def test_identical_paths_deduped_and_stably_sorted(self):
        dictionary = lookup({"乳清粉": ["乳", "大豆"], "奶粉": ["乳"]})
        text = "酱甲（奶粉，乳清粉），酱乙（奶粉），酱丙（奶粉（乳清粉））"
        tree = tree_dicts(text, dictionary)
        src = missing_allergen_sources(tree, ["乳"])
        expected = [
            ["酱丙", "奶粉"],
            ["酱丙", "奶粉", "乳清粉"],
            ["酱乙", "奶粉"],
            ["酱甲", "乳清粉"],
            ["酱甲", "奶粉"],
        ]
        # 同一节点路径只记一次（不因过敏原集合多成员重复），整体稳定排序
        self.assertEqual(src["乳"], expected)
        self.assertEqual(len(src["乳"]), len({tuple(p) for p in src["乳"]}))
        # 结果稳定：重复调用顺序一致
        self.assertEqual(missing_allergen_sources(tree, ["乳"]), src)

    def test_allergens_not_present_have_no_entry(self):
        src = self._sources("巧克力酱（奶粉（乳清粉））", ("乳", "花生"))
        self.assertNotIn("花生", src)
        self.assertEqual(src["乳"], [["巧克力酱", "奶粉", "乳清粉"]])

    def test_compare_label_attaches_sources(self):
        tree = tree_dicts(
            "小麦粉，巧克力酱（奶粉（乳清粉））", self.dictionary)
        check = compare_label(["麸质谷物", "乳"], ["麸质谷物"], tree)
        self.assertEqual(check["missing"], ["乳"])
        self.assertEqual(check["missing_sources"],
                         {"乳": [["巧克力酱", "奶粉", "乳清粉"]]})
        # 无遗漏时附带空映射，结构稳定
        ok = compare_label(["乳"], ["乳"], tree)
        self.assertEqual(ok["missing"], [])
        self.assertEqual(ok["missing_sources"], {})

    def test_compare_label_without_tree_keeps_legacy_shape(self):
        check = compare_label(["乳"], [])
        self.assertNotIn("missing_sources", check)


if __name__ == "__main__":
    unittest.main()
