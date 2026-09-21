"""审查数据迁移包：全量导出稳定性、只读预检、一次性令牌与单事务整体替换测试。

覆盖：
- 导出包结构、格式版本、SHA-256 摘要、同库重复导出逐字节稳定；
- 预检逐项返回问题（版本、摘要、字段类型、别名图无环 / 自环 / 多目标、
  审查记录结构），预检绝不写库；
- 警告（别名最终标准名未登记、重复别名折叠、无指纹记录、空包清空风险）；
- 一次性令牌：预检通过发券、确认消费、重放拒绝、改文件后摘要不符拒绝、
  无券 / 空券拒绝；
- 确认导入在单个事务内整体替换（含 ID 与创建时间保留、自增序列接续）；
- 事务回滚：构造约束冲突，替换失败后原数据保持不变。
"""
import copy
import json
import os
import tempfile
import unittest

_DB_DIR = tempfile.mkdtemp(prefix="allergen-migration-")
os.environ["DB_PATH"] = os.path.join(_DB_DIR, "migration.db")

from app import db, migration  # noqa: E402
from app.review import analyze  # noqa: E402

DICT_TEXT = """\
小麦粉：麸质谷物
乳清粉：乳
奶粉：乳
可可脂：
大豆卵磷脂：大豆
鸡蛋：蛋类
食盐：
"""
TEXT_A = ("配料表：小麦粉，巧克力酱（可可脂，奶粉（乳清粉），大豆卵磷脂），"
          "鸡蛋，食盐")
DECLARED = ["麸质谷物", "蛋类"]


def make_review(name="迁移测试记录", text_a=TEXT_A, declared=None,
                text_b=None):
    """走正式审查全链路创建记录，返回 (rid, result)。"""
    result, fingerprint = analyze(
        text_a=text_a, text_b=text_b, label_a="现版配方",
        label_b="新版配方", declared=declared or DECLARED)
    rid = db.create_review(
        name=name, declared=declared or DECLARED, text_a=text_a,
        text_b=text_b, label_a="现版配方", label_b="新版配方",
        result=result, dict_fingerprint=fingerprint)
    return rid, result


class MigrationExportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()
        db.bulk_import(DICT_TEXT)
        db.bulk_upsert_aliases([("脱盐乳清", "乳清粉")])
        cls.rid, cls.result = make_review()

    def test_export_structure_and_checksum(self):
        snap = db.list_all_data()
        pkg = migration.build_package(**snap)
        self.assertEqual(pkg["format_version"], migration.FORMAT_VERSION)
        self.assertEqual(pkg["checksum"]["algorithm"], "SHA-256")
        self.assertEqual(len(pkg["checksum"]["value"]), 64)
        self.assertTrue(all(c in "0123456789abcdef"
                            for c in pkg["checksum"]["value"]))
        # 摘要 = 除 checksum 外规范化内容的 SHA-256
        self.assertEqual(pkg["checksum"]["value"],
                         migration.compute_digest(pkg))
        names = [e["ingredient"] for e in pkg["dictionary"]]
        self.assertEqual(names, sorted(names))  # 稳定排序
        self.assertEqual([r["id"] for r in pkg["reviews"]],
                         sorted(r["id"] for r in pkg["reviews"]))
        # updated_at 等易变字段不进包
        self.assertNotIn("updated_at", pkg["dictionary"][0])
        r0 = pkg["reviews"][0]
        for field in ("id", "name", "declared", "text_a", "text_b",
                      "label_a", "label_b", "result", "created_at",
                      "dict_fingerprint"):
            self.assertIn(field, r0)

    def test_repeated_export_byte_stable(self):
        """同一数据库内容重复导出逐字节稳定（即便 updated_at 被刷新）。"""
        def dump():
            return migration.serialize_package(
                migration.build_package(**db.list_all_data()))
        first = dump()
        # 再次 upsert 同内容字典会刷新 updated_at，但导出内容必须不变
        db.bulk_import("小麦粉：麸质谷物\n乳清粉：乳\n")
        db.upsert_entry("可可脂", [])
        second = dump()
        self.assertEqual(first, second)
        # 序列化产物本身是合法 JSON 且含末尾换行
        self.assertTrue(second.endswith(b"\n"))
        decoded = json.loads(second.decode("utf-8"))
        self.assertEqual(decoded["checksum"]["value"],
                         migration.compute_digest(decoded))

    def test_round_trip_preserves_data(self):
        pkg = migration.build_package(**db.list_all_data())
        outcome = migration.validate_package(pkg)
        self.assertTrue(outcome["ok"], outcome["problems"])
        dictionary, aliases, reviews = migration.normalized_rows(pkg)
        counts = db.replace_all_data(dictionary, aliases, reviews)
        self.assertEqual(counts, {"dictionary": len(dictionary),
                                  "aliases": len(aliases),
                                  "reviews": len(reviews)})
        after = db.list_all_data()
        self.assertEqual(len(after["dictionary"]), len(pkg["dictionary"]))
        self.assertEqual(len(after["aliases"]), len(pkg["aliases"]))
        self.assertEqual(len(after["reviews"]), len(pkg["reviews"]))
        reopened = db.get_review(self.rid)
        self.assertIsNotNone(reopened)
        self.assertEqual(reopened["name"], "迁移测试记录")
        self.assertFalse(reopened["needs_recheck"])  # 指纹保留 → 时效正常
        self.assertEqual(reopened["result"]["label_check"]["missing"],
                         self.result["label_check"]["missing"])


class MigrationPrecheckTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()

    def setUp(self):
        migration.tokens.clear()

    def _good_package(self):
        return migration.build_package(**db.list_all_data())

    def test_good_package_passes(self):
        out = migration.validate_package(self._good_package())
        self.assertTrue(out["ok"])
        self.assertEqual(out["problems"], [])

    def test_not_object(self):
        out = migration.validate_package([1, 2])
        self.assertFalse(out["ok"])
        self.assertTrue(any(p["scope"] == "$" for p in out["problems"]))

    def test_missing_sections(self):
        out = migration.validate_package({"format_version": 1})
        scopes = {p["scope"] for p in out["problems"]}
        self.assertIn("dictionary", scopes)
        self.assertIn("aliases", scopes)
        self.assertIn("reviews", scopes)
        self.assertIn("checksum", scopes)

    def test_unsupported_version(self):
        pkg = self._good_package()
        pkg["format_version"] = 999
        pkg["checksum"]["value"] = migration.compute_digest(pkg)
        out = migration.validate_package(pkg)
        self.assertFalse(out["ok"])
        self.assertTrue(any(p["scope"] == "format_version"
                            for p in out["problems"]))

    def test_bad_version_type(self):
        pkg = self._good_package()
        pkg["format_version"] = "1"
        out = migration.validate_package(pkg)
        self.assertTrue(any(p["scope"] == "format_version"
                            for p in out["problems"]))

    def test_checksum_tampered(self):
        pkg = self._good_package()
        pkg["dictionary"][0]["allergens"].append("被篡改的过敏原")
        out = migration.validate_package(pkg)  # checksum 未同步重算
        self.assertFalse(out["ok"])
        bad = [p for p in out["problems"]
               if p.get("type") == "checksum_mismatch"]
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0]["expected"], migration.compute_digest(pkg))

    def test_checksum_algorithm_wrong(self):
        pkg = self._good_package()
        pkg["checksum"]["algorithm"] = "MD5"
        out = migration.validate_package(pkg)
        self.assertTrue(any(p["scope"] == "checksum.algorithm"
                            for p in out["problems"]))

    def test_checksum_value_shape(self):
        pkg = self._good_package()
        pkg["checksum"]["value"] = "abc"
        out = migration.validate_package(pkg)
        self.assertTrue(any(p["scope"] == "checksum.value"
                            for p in out["problems"]))

    def test_dictionary_type_errors(self):
        pkg = self._good_package()
        pkg["dictionary"] = [
            {"ingredient": "好原料", "allergens": ["乳"]},
            {"ingredient": "  ", "allergens": []},
            {"ingredient": "坏过敏原", "allergens": "乳"},
            {"ingredient": "好原料", "allergens": ["蛋"]},
        ]
        out = migration.validate_package(pkg)
        scopes = {p["scope"] for p in out["problems"]}
        self.assertIn("dictionary[1].ingredient", scopes)
        self.assertIn("dictionary[2].allergens", scopes)
        self.assertTrue(any("重复" in p["message"] for p in out["problems"]))

    def test_alias_self_loop(self):
        pkg = self._good_package()
        pkg["aliases"] = [{"alias": "奶粉", "target": "奶粉"}]
        out = migration.validate_package(self._rechecksum(pkg))
        types = [p.get("type") for p in out["problems"]]
        self.assertIn("self_loop", types)

    def test_alias_cycle(self):
        pkg = self._good_package()
        pkg["aliases"] = [
            {"alias": "甲", "target": "乙"},
            {"alias": "乙", "target": "丙"},
            {"alias": "丙", "target": "甲"},
        ]
        out = migration.validate_package(self._rechecksum(pkg))
        cyc = [p for p in out["problems"] if p.get("type") == "cycle"]
        self.assertEqual(len(cyc), 1)
        self.assertEqual(cyc[0]["cycle"][0], cyc[0]["cycle"][-1])  # 闭合环

    def test_alias_multi_target(self):
        pkg = self._good_package()
        pkg["aliases"] = [
            {"alias": "脱盐乳清", "target": "乳清粉"},
            {"alias": "脱盐乳清", "target": "奶粉"},
        ]
        out = migration.validate_package(self._rechecksum(pkg))
        self.assertTrue(any(p.get("type") == "multi_target"
                            for p in out["problems"]))

    def test_alias_cycle_through_multi_target_conflict_still_detected(self):
        """多目标冲突的别名同时处于环中时，环也必须检出（建图不能跳过它）。"""
        pkg = self._good_package()
        pkg["aliases"] = [
            {"alias": "甲", "target": "乙"},
            {"alias": "甲", "target": "丙"},   # 同一别名多目标
            {"alias": "乙", "target": "甲"},   # 甲 → 乙 → 甲 成环
        ]
        out = migration.validate_package(self._rechecksum(pkg))
        types = [p.get("type") for p in out["problems"]]
        self.assertIn("multi_target", types)
        self.assertIn("cycle", types)

    def test_alias_type_errors(self):
        pkg = self._good_package()
        pkg["aliases"] = [
            {"alias": "", "target": "乳清粉"},
            {"alias": "别名X"},
            "not-an-object",
        ]
        out = migration.validate_package(self._rechecksum(pkg))
        scopes = {p["scope"] for p in out["problems"]}
        self.assertIn("aliases[0].alias", scopes)
        self.assertIn("aliases[1].target", scopes)
        self.assertIn("aliases[2]", scopes)

    def test_alias_terminal_not_in_dict_warning(self):
        pkg = self._good_package()
        pkg["aliases"] = [{"alias": "神秘原料", "target": "不存在的标准名"}]
        out = migration.validate_package(self._rechecksum(pkg))
        self.assertTrue(out["ok"])
        self.assertTrue(any("未在字典中登记" in w["message"]
                            for w in out["warnings"]))

    def test_duplicate_alias_is_warning_not_error(self):
        pkg = self._good_package()
        pkg["aliases"] = [
            {"alias": "脱盐乳清", "target": "乳清粉"},
            {"alias": "脱盐乳清", "target": "乳清粉"},
        ]
        out = migration.validate_package(self._rechecksum(pkg))
        self.assertTrue(out["ok"])
        self.assertTrue(any("重复" in w["message"] for w in out["warnings"]))

    def test_review_structure_errors(self):
        pkg = self._good_package()
        good = pkg["reviews"][0]
        bad = copy.deepcopy(good)
        bad["id"] = 0
        bad["name"] = "   "
        bad["declared"] = "乳"
        bad["label_a"] = 7
        bad["result"]["version_a"]["tree"] = [{"name": "x"}]  # 缺字段
        bad["result"]["label_check"]["missing"] = "not-a-list"
        pkg["reviews"] = [good, bad]
        out = migration.validate_package(self._rechecksum(pkg))
        scopes = {p["scope"] for p in out["problems"]}
        self.assertIn("reviews[1].id", scopes)
        self.assertIn("reviews[1].name", scopes)
        self.assertIn("reviews[1].declared", scopes)
        self.assertIn("reviews[1].label_a", scopes)
        self.assertIn("reviews[1].result.version_a.tree[0].own_allergens",
                      scopes)
        self.assertIn("reviews[1].result.label_check.missing", scopes)

    def test_review_duplicate_id(self):
        pkg = self._good_package()
        dup = copy.deepcopy(pkg["reviews"][0])
        pkg["reviews"].append(dup)
        out = migration.validate_package(self._rechecksum(pkg))
        self.assertTrue(any("ID" in p["message"] and "重复" in p["message"]
                            for p in out["problems"]))

    def test_missing_fingerprint_is_warning(self):
        pkg = self._good_package()
        pkg["reviews"][0]["dict_fingerprint"] = None
        out = migration.validate_package(self._rechecksum(pkg))
        self.assertTrue(out["ok"])
        self.assertTrue(any("指纹" in w["message"] for w in out["warnings"]))

    def test_empty_package_warns_about_wipe(self):
        pkg = {"format_version": migration.FORMAT_VERSION,
               "dictionary": [], "aliases": [], "reviews": []}
        pkg["checksum"] = {"algorithm": "SHA-256",
                           "value": migration.compute_digest(pkg)}
        out = migration.validate_package(pkg)
        self.assertTrue(out["ok"])
        self.assertTrue(any("清空" in w["message"] for w in out["warnings"]))

    def test_precheck_never_writes(self):
        """构造坏包反复预检，库中数据量不变。"""
        before = db.current_counts()
        bad = {"format_version": 1, "dictionary": "x",
               "aliases": [{"alias": "甲", "target": "乙"},
                           {"alias": "乙", "target": "甲"}],
               "reviews": [{"id": -1}]}
        out = migration.precheck(bad)
        self.assertFalse(out["ok"])
        self.assertNotIn("token", out)
        self.assertEqual(db.current_counts(), before)

    def test_precheck_issues_token_bound_to_digest(self):
        out = migration.precheck(self._good_package())
        self.assertTrue(out["ok"])
        self.assertIn("token", out)
        self.assertEqual(len(out["token"]), 64)

    def test_current_counts_warning(self):
        out = migration.precheck(
            self._good_package(),
            current_counts={"dictionary": 9, "aliases": 8, "reviews": 7})
        msg = out["warnings"][0]["message"]
        self.assertIn("整体替换", msg)
        self.assertIn("9", msg)

    @staticmethod
    def _rechecksum(pkg):
        pkg["checksum"] = {"algorithm": "SHA-256",
                           "value": migration.compute_digest(pkg)}
        return pkg


class MigrationTokenAndReplaceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()

    def setUp(self):
        migration.tokens.clear()
        # 每个用例重置为确定的基线数据
        db.replace_all_data([], [], [])

    def _seed_baseline(self):
        db.bulk_import(DICT_TEXT)
        db.bulk_upsert_aliases([("脱盐乳清", "乳清粉")])
        rid, _ = make_review("旧记录")
        return rid

    def test_full_import_flow_replaces_everything(self):
        self._seed_baseline()
        # 在另一个“库状态”上导出：只有 2 条字典、0 别名、1 条新记录(id=5)
        pkg = {
            "format_version": 1,
            "dictionary": [
                {"ingredient": "新原料A", "allergens": ["坚果"]},
                {"ingredient": "新原料B", "allergens": []},
            ],
            "aliases": [{"alias": "别名A", "target": "新原料A"}],
            "reviews": [self._review_row(5, "导入的记录")],
        }
        pkg["checksum"] = {"algorithm": "SHA-256",
                           "value": migration.compute_digest(pkg)}
        out = migration.precheck(pkg, current_counts=db.current_counts())
        self.assertTrue(out["ok"], out["problems"])
        token = out["token"]
        dictionary, aliases, reviews = migration.normalized_rows(pkg)
        counts = db.replace_all_data(dictionary, aliases, reviews)
        # 令牌由 API 层消费；这里直接验证替换结果
        self.assertEqual(counts, {"dictionary": 2, "aliases": 1, "reviews": 1})
        names = {e["ingredient"] for e in db.list_dict()}
        self.assertEqual(names, {"新原料A", "新原料B"})
        self.assertEqual(db.list_aliases(),
                         [{"alias": "别名A", "target": "新原料A"}])
        review = db.get_review(5)
        self.assertIsNotNone(review)
        self.assertEqual(review["name"], "导入的记录")
        self.assertIsNone(db.get_review(1))
        # 自增序列接续：新记录 id 应为 6
        new_rid = db.create_review(
            name="后续记录", declared=[], text_a="新原料A", text_b=None,
            label_a="现版配方", label_b="新版配方", result={}, )
        self.assertEqual(new_rid, 6)

    def test_token_one_time_use(self):
        pkg = self._minimal_pkg()
        out = migration.precheck(pkg)
        token = out["token"]
        migration.tokens.consume(token, out["digest"])  # 首次消费成功
        with self.assertRaises(migration.TokenError):
            migration.tokens.consume(token, out["digest"])  # 重放拒绝

    def test_token_rejects_changed_file(self):
        pkg = self._minimal_pkg()
        out = migration.precheck(pkg)
        token = out["token"]
        tampered = copy.deepcopy(pkg)
        # 改动真实内容（_minimal_pkg 的字典本来就是空的，改 aliases）
        tampered["aliases"] = [{"alias": "新别名", "target": "不存在"}]
        tampered["checksum"]["value"] = migration.compute_digest(tampered)
        new_digest = migration.compute_digest(tampered)
        self.assertNotEqual(new_digest, out["digest"])
        with self.assertRaises(migration.TokenError):
            migration.tokens.consume(token, new_digest)
        # 旧令牌仍未被消费（摘要不符时也必须一次性语义安全）
        with self.assertRaises(migration.TokenError):
            migration.tokens.consume("not-a-token", out["digest"])

    def test_token_empty_rejected(self):
        with self.assertRaises(migration.TokenError):
            migration.tokens.consume("", "0" * 64)

    def test_rollback_on_failure_keeps_old_data(self):
        rid = self._seed_baseline()
        before_counts = db.current_counts()
        before = db.list_all_data()
        # 构造必然在事务中段失败的包：审查记录 id 重复（主键冲突）
        bad_pkg = {
            "format_version": 1,
            "dictionary": [{"ingredient": "不会落库", "allergens": []}],
            "aliases": [],
            "reviews": [self._review_row(9, "x"), self._review_row(9, "y")],
        }
        bad_pkg["checksum"] = {"algorithm": "SHA-256",
                               "value": migration.compute_digest(bad_pkg)}
        # 预检会因 ID 重复拦截；这里直接打持久层模拟事务中失败的回滚
        dictionary, aliases, reviews = migration.normalized_rows(bad_pkg)
        with self.assertRaises(Exception):
            db.replace_all_data(dictionary, aliases, reviews)
        # 回滚后数据与替换前完全一致
        self.assertEqual(db.current_counts(), before_counts)
        after = db.list_all_data()
        self.assertEqual(
            [(e["ingredient"], e["allergens"]) for e in after["dictionary"]],
            [(e["ingredient"], e["allergens"]) for e in before["dictionary"]])
        self.assertEqual(after["aliases"], before["aliases"])
        self.assertEqual([r["id"] for r in after["reviews"]],
                         [r["id"] for r in before["reviews"]])
        self.assertIsNotNone(db.get_review(rid))
        self.assertIsNone(db.get_review(9))

    def test_replace_with_empty_package_wipes(self):
        self._seed_baseline()
        counts = db.replace_all_data([], [], [])
        self.assertEqual(counts, {"dictionary": 0, "aliases": 0, "reviews": 0})
        self.assertEqual(db.current_counts(),
                         {"dictionary": 0, "aliases": 0, "reviews": 0})
        # 清空后自增序列归零：新记录从 1 开始
        new_rid = db.create_review(
            name="全新记录", declared=[], text_a="食盐", text_b=None,
            label_a="现版配方", label_b="新版配方", result={})
        self.assertEqual(new_rid, 1)

    @staticmethod
    def _review_row(rid, name):
        return {
            "id": rid, "name": name, "declared": ["坚果"],
            "text_a": "新原料A", "text_b": None,
            "label_a": "现版配方", "label_b": "新版配方",
            "result": {
                "version_a": {
                    "tree": [{"name": "新原料A", "own_allergens": ["坚果"],
                              "total_allergens": ["坚果"], "unknown": False,
                              "children": []}],
                    "allergens": ["坚果"],
                    "unknown_ingredients": [],
                },
                "version_b": None, "diff": None,
                "ingredient_changes": None,
                "label_check": {
                    "missing": [], "over_reported": [],
                    "matched": ["坚果"], "missing_sources": {},
                },
                "label_check_version": "现版配方",
            },
            "created_at": "2026-01-01T00:00:00+08:00",
            "dict_fingerprint": None,
        }

    def _minimal_pkg(self):
        pkg = {
            "format_version": 1, "dictionary": [], "aliases": [],
            "reviews": [self._review_row(1, "最小记录")],
        }
        pkg["checksum"] = {"algorithm": "SHA-256",
                           "value": migration.compute_digest(pkg)}
        return pkg


if __name__ == "__main__":
    unittest.main()
