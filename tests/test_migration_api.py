"""审查数据迁移包的 FastAPI 端到端测试（TestClient 走真实 HTTP 路由）。

验证：导出端点、预检端点（坏包逐项返回且不写库、好包发令牌）、
确认端点（一次性令牌、重放 / 改文件 / 坏令牌拒绝、成功后整体替换
并保留 ID）。需要 httpx（开发依赖 requirements-dev.txt）。
"""
import json
import os
import tempfile
import unittest

_DB_DIR = tempfile.mkdtemp(prefix="allergen-migration-api-")
os.environ["DB_PATH"] = os.path.join(_DB_DIR, "migration_api.db")

try:
    from fastapi.testclient import TestClient
    _HAS_HTTPX = True
except ImportError:  # 运行环境未安装开发依赖 httpx
    _HAS_HTTPX = False

from app import db, migration  # noqa: E402
from app.main import app  # noqa: E402
from app.review import analyze  # noqa: E402


def _make_review(name):
    result, fp = analyze(
        text_a="小麦粉，奶粉", text_b=None, label_a="现版配方",
        label_b="新版配方", declared=["乳"])
    rid = db.create_review(
        name=name, declared=["乳"], text_a="小麦粉，奶粉", text_b=None,
        label_a="现版配方", label_b="新版配方", result=result,
        dict_fingerprint=fp)
    return rid


@unittest.skipUnless(_HAS_HTTPX, "未安装 httpx（开发依赖），跳过 API 端到端测试")
class MigrationApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()
        cls.client = TestClient(app)

    def _seed_baseline(self):
        """重置为确定基线：2 条字典、0 别名、1 条审查记录。"""
        db.replace_all_data([], [], [])
        db.bulk_import("小麦粉：麸质谷物\n奶粉：乳\n")
        self.rid = _make_review("API 迁移记录")

    def setUp(self):
        migration.tokens.clear()
        self._seed_baseline()

    def _export(self):
        resp = self.client.get("/api/migration/export")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("attachment", resp.headers["content-disposition"])
        self.assertIn("allergen_review_migration.json",
                      resp.headers["content-disposition"])
        return json.loads(resp.content.decode("utf-8"))

    def test_counts_endpoint(self):
        resp = self.client.get("/api/migration/counts")
        self.assertEqual(resp.status_code, 200)
        counts = resp.json()["counts"]
        self.assertEqual(counts["dictionary"], 2)
        self.assertEqual(counts["reviews"], 1)

    def test_export_is_stable_and_self_checksumming(self):
        first = self.client.get("/api/migration/export").content
        second = self.client.get("/api/migration/export").content
        self.assertEqual(first, second)
        pkg = json.loads(first)
        self.assertEqual(pkg["format_version"], 1)
        self.assertEqual(pkg["checksum"]["value"],
                         migration.compute_digest(pkg))

    def test_precheck_valid_package_returns_token(self):
        pkg = self._export()
        resp = self.client.post("/api/migration/precheck", json={"package": pkg})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["problems"], [])
        self.assertEqual(len(body["token"]), 64)
        self.assertEqual(body["counts"]["dictionary"], 2)
        # 预检不改变数据量
        self.assertEqual(db.current_counts()["dictionary"], 2)
        # 警告中含整体替换风险提示
        self.assertTrue(any("整体替换" in w["message"]
                            for w in body["warnings"]))

    def test_precheck_bad_package_lists_problems_and_no_token(self):
        bad = {
            "format_version": 1,
            "dictionary": [{"ingredient": "小麦粉", "allergens": ["麸质谷物"]}],
            "aliases": [{"alias": "甲", "target": "乙"},
                        {"alias": "乙", "target": "甲"}],
            "reviews": [{"id": "bad-id"}],
        }
        bad["checksum"] = {"algorithm": "SHA-256",
                           "value": migration.compute_digest(bad)}
        resp = self.client.post("/api/migration/precheck",
                                json={"package": bad})
        self.assertEqual(resp.status_code, 200)  # 预检结论在响应体内
        body = resp.json()
        self.assertFalse(body["ok"])
        self.assertNotIn("token", body)
        kinds = " ".join(p["scope"] + p.get("type", "")
                         for p in body["problems"])
        self.assertIn("cycle", kinds)
        self.assertIn("reviews[0].id", kinds)
        # 预检绝不写库
        self.assertEqual(db.current_counts()["reviews"], 1)

    def test_precheck_tampered_checksum_rejected(self):
        pkg = self._export()
        pkg["dictionary"][0]["allergens"].append("坚果")  # 未重算摘要
        resp = self.client.post("/api/migration/precheck", json={"package": pkg})
        body = resp.json()
        self.assertFalse(body["ok"])
        self.assertTrue(any(p.get("type") == "checksum_mismatch"
                            for p in body["problems"]))

    def test_import_without_token_rejected(self):
        pkg = self._export()
        resp = self.client.post("/api/migration/import",
                                json={"package": pkg, "token": ""})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("令牌", resp.json()["detail"]["message"])
        self.assertEqual(db.current_counts()["dictionary"], 2)

    def test_import_rejects_replayed_token(self):
        pkg = self._export()
        pre = self.client.post("/api/migration/precheck",
                               json={"package": pkg}).json()
        self.assertTrue(pre["ok"])
        r1 = self.client.post("/api/migration/import",
                              json={"package": pkg, "token": pre["token"]})
        self.assertEqual(r1.status_code, 200)
        r2 = self.client.post("/api/migration/import",
                              json={"package": pkg, "token": pre["token"]})
        self.assertEqual(r2.status_code, 400)
        self.assertIn("一次性", r2.json()["detail"]["message"])

    def test_import_rejects_token_bound_to_other_file(self):
        pkg_a = self._export()
        token_a = self.client.post(
            "/api/migration/precheck", json={"package": pkg_a}).json()["token"]
        pkg_b = self._export()
        pkg_b["dictionary"].append({"ingredient": "外来原料", "allergens": []})
        pkg_b["checksum"]["value"] = migration.compute_digest(pkg_b)
        # pkg_b 自身预检通过，但用 pkg_a 的令牌确认 → 拒绝
        resp = self.client.post("/api/migration/import",
                                json={"package": pkg_b, "token": token_a})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("摘要", resp.json()["detail"]["message"])
        # 外来数据没有落库
        names = {e["ingredient"] for e in db.list_dict()}
        self.assertNotIn("外来原料", names)

    def test_full_import_replaces_and_preserves_ids(self):
        new_pkg = {
            "format_version": 1,
            "dictionary": [{"ingredient": "全新原料", "allergens": ["鱼类"]}],
            "aliases": [],
            "reviews": [{
                "id": 42, "name": "外来审查", "declared": ["鱼类"],
                "text_a": "全新原料", "text_b": None,
                "label_a": "现版配方", "label_b": "新版配方",
                "result": {
                    "version_a": {"tree": [
                        {"name": "全新原料", "own_allergens": ["鱼类"],
                         "total_allergens": ["鱼类"], "unknown": False,
                         "children": []}],
                        "allergens": ["鱼类"], "unknown_ingredients": []},
                    "version_b": None, "diff": None,
                    "ingredient_changes": None,
                    "label_check": {"missing": [], "over_reported": [],
                                    "matched": ["鱼类"], "missing_sources": {}},
                    "label_check_version": "现版配方"},
                "created_at": "2025-05-05T05:05:05+08:00",
                "dict_fingerprint": None,
            }],
        }
        new_pkg["checksum"] = {"algorithm": "SHA-256",
                               "value": migration.compute_digest(new_pkg)}
        pre = self.client.post("/api/migration/precheck",
                               json={"package": new_pkg}).json()
        self.assertTrue(pre["ok"], pre.get("problems"))
        resp = self.client.post("/api/migration/import",
                                json={"package": new_pkg,
                                      "token": pre["token"]})
        self.assertEqual(resp.status_code, 200, resp.text)
        counts = resp.json()["counts"]
        self.assertEqual(counts, {"dictionary": 1, "aliases": 0, "reviews": 1})
        self.assertEqual(db.current_counts(), counts)
        self.assertIsNone(db.get_review(self.rid))  # 旧记录被整体替换
        r42 = db.get_review(42)
        self.assertIsNotNone(r42)
        self.assertEqual(r42["name"], "外来审查")
        self.assertEqual(r42["created_at"], "2025-05-05T05:05:05+08:00")
        self.assertTrue(r42["needs_recheck"])  # 无指纹 → 导入后需复核
        # 自增序列接续 max(id)=42
        rid = db.create_review(
            name="后续", declared=[], text_a="全新原料", text_b=None,
            label_a="现版配方", label_b="新版配方", result={})
        self.assertEqual(rid, 43)


if __name__ == "__main__":
    unittest.main()
