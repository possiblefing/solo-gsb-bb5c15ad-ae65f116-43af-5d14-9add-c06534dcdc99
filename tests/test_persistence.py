"""遗漏来源追踪的持久化与报告集成测试（不依赖 FastAPI，走 db 全链路）。

覆盖：
- 审查结果中的 missing_sources 随记录持久化，重新打开仍可查看；
- 升级前生成、结果里没有 missing_sources 的历史记录，重新打开时自动补齐；
- 两版配方时按实际核对版本（B 版）的配料树补齐；
- HTML / Markdown 报告同步输出来源路径。
"""
import json
import os
import tempfile
import unittest

_DB_DIR = tempfile.mkdtemp(prefix="allergen-test-")
os.environ["DB_PATH"] = os.path.join(_DB_DIR, "persist.db")

from app import db  # noqa: E402
from app.analysis import (attach_allergens, collect_allergens, compare_label,
                          flatten)  # noqa: E402
from app.parser import parse_recipe  # noqa: E402
from app.report import render_html_report, render_markdown_report  # noqa: E402


def _build_version(tree):
    return {
        "tree": [n.to_dict() for n in tree],
        "allergens": sorted(collect_allergens(tree)),
        "unknown_ingredients": sorted({n.name for n in flatten(tree) if n.unknown}),
    }


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
DECLARED = ["麸质谷物", "蛋类"]


class PersistenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()
        db.bulk_import(DICT_TEXT)
        cls.dictionary = db.dict_lookup()

    def _result(self):
        tree = parse_recipe(TEXT_A)
        attach_allergens(tree, self.dictionary)
        version = _build_version(tree)
        check = compare_label(version["allergens"], DECLARED, version["tree"])
        return {
            "version_a": version, "version_b": None, "diff": None,
            "ingredient_changes": None, "label_check": check,
            "label_check_version": "现版配方",
        }

    def test_sources_persisted_and_visible_on_reopen(self):
        result = self._result()
        rid = db.create_review(
            name="持久化测试", declared=DECLARED, text_a=TEXT_A, text_b=None,
            label_a="现版配方", label_b="新版配方", result=result)
        reopened = db.get_review(rid)
        sources = reopened["result"]["label_check"]["missing_sources"]
        self.assertEqual(sources["乳"], [
            ["巧克力酱", "奶粉"],
            ["巧克力酱", "奶粉", "乳清粉"],
            ["巧克力酱", "奶粉", "乳糖"],
        ])
        self.assertEqual(sources["大豆"], [["巧克力酱", "大豆卵磷脂"]])

    def test_legacy_record_backfilled_on_reopen(self):
        # 模拟升级前的旧记录：结果 JSON 中没有 missing_sources
        result = self._result()
        del result["label_check"]["missing_sources"]
        rid = db.create_review(
            name="旧记录", declared=DECLARED, text_a=TEXT_A, text_b=None,
            label_a="现版配方", label_b="新版配方", result=result)

        # 直接写入的库内 JSON 确实缺字段
        with db._connect() as conn:
            raw = json.loads(conn.execute(
                "SELECT result FROM reviews WHERE id = ?", (rid,)).fetchone()["result"])
        self.assertNotIn("missing_sources", raw["label_check"])

        # 重新打开时即时补齐，历史记录仍可展开路径
        reopened = db.get_review(rid)
        sources = reopened["result"]["label_check"]["missing_sources"]
        self.assertIn(["巧克力酱", "奶粉", "乳清粉"], sources["乳"])
        self.assertIn(["巧克力酱", "奶粉"], sources["乳"])

    def test_backfill_uses_checked_version_tree(self):
        # 两版配方：遗漏来自 B 版（坚果），补齐时必须使用 B 版树而非 A 版
        tree_a = parse_recipe(
            "小麦粉，巧克力酱（奶粉（乳清粉）），鸡蛋")
        tree_b = parse_recipe(
            "小麦粉，巧克力酱（可可脂，坚果酱（腰果酱（腰果））），鸡蛋")
        extra = {"坚果酱": [], "腰果酱": [], "腰果": ["坚果"]}
        dictionary = {**self.dictionary, **{k: set(v) for k, v in extra.items()}}
        attach_allergens(tree_a, dictionary)
        attach_allergens(tree_b, dictionary)
        va, vb = _build_version(tree_a), _build_version(tree_b)
        result = {
            "version_a": va, "version_b": vb, "diff": None,
            "ingredient_changes": None,
            "label_check": compare_label(vb["allergens"], DECLARED),  # 旧版无第三参
            "label_check_version": "新版配方",
        }
        rid = db.create_review(
            name="两版旧记录", declared=DECLARED,
            text_a="A", text_b="B", label_a="现版配方", label_b="新版配方",
            result=result)
        reopened = db.get_review(rid)
        sources = reopened["result"]["label_check"]["missing_sources"]
        self.assertEqual(sources["坚果"],
                         [["巧克力酱", "坚果酱", "腰果酱", "腰果"]])

    def test_reports_render_paths(self):
        result = self._result()
        rid = db.create_review(
            name="报告测试", declared=DECLARED, text_a=TEXT_A, text_b=None,
            label_a="现版配方", label_b="新版配方", result=result)
        review = db.get_review(rid)
        html_text = render_html_report(review)
        md_text = render_markdown_report(review)
        for needle in ("巧克力酱", "奶粉", "乳清粉", "来源路径"):
            self.assertIn(needle, html_text)
        self.assertIn("巧克力酱 › 奶粉 › 乳清粉", md_text)
        self.assertIn("**乳**", md_text)

    def test_html_report_escapes_path_names(self):
        # 原料名中的特殊字符在路径输出时必须转义，避免注入
        tree = parse_recipe("坏酱（<script>x</script>（乳清粉））")
        attach_allergens(tree, self.dictionary)
        version = _build_version(tree)
        check = compare_label(version["allergens"], [], version["tree"])
        result = {"version_a": version, "version_b": None, "diff": None,
                  "ingredient_changes": None, "label_check": check,
                  "label_check_version": "现版配方"}
        rid = db.create_review(
            name="转义测试", declared=[], text_a="x", text_b=None,
            label_a="现版配方", label_b="新版配方", result=result)
        html_text = render_html_report(db.get_review(rid))
        self.assertNotIn("<script>x</script>", html_text)
        self.assertIn("&lt;script&gt;x&lt;/script&gt;", html_text)


if __name__ == "__main__":
    unittest.main()
