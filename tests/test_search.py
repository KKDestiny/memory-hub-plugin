"""search.py 的测试：kind 判定、会话分节摘录、CLI 检索（含 --json 与过滤参数）。"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "skills", "memory-hub-consume", "scripts", "search.py")

_SPEC = importlib.util.spec_from_file_location("mh_search", SCRIPT)
ms = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ms)


class KindOfTest(unittest.TestCase):
    def test_frontmatter_kind_takes_priority(self):
        self.assertEqual(ms.kind_of("x.md", "---\nkind: rule\n---\n"), "rule")

    def test_session_and_hash_names(self):
        self.assertEqual(ms.kind_of("2026-01-01-session-abc.md", ""), "session")
        self.assertEqual(ms.kind_of("memory-global-deadbeef.md", ""), "memory")

    def test_fixed_legacy_names(self):
        self.assertEqual(ms.kind_of("rules.md", ""), "rule")
        self.assertEqual(ms.kind_of("project-memory.md", ""), "memory")

    def test_unknown_returns_empty(self):
        self.assertEqual(ms.kind_of("notes.md", ""), "")


class SessionTurnsTextTest(unittest.TestCase):
    def test_returns_hit_section_and_next(self):
        text = "## 轮 1\n用户问了 Drizzle\n\n## 轮 2\n助手给出方案\n\n## 轮 3\n无关内容"
        out = ms.session_turns_text(text, "drizzle")
        self.assertIn("轮 1", out)
        self.assertIn("轮 2", out)  # 命中轮的下一轮一并给出
        self.assertNotIn("无关内容", out)  # 未命中的后续轮不进入摘录

    def test_non_turn_file_falls_back_to_context_window(self):
        out = ms.session_turns_text("前文 关键词 后文", "关键词")
        self.assertIn("关键词", out)


class CliSearchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        g = os.path.join(self.root, "global", "clients", "claude-code")
        os.makedirs(g)
        open(os.path.join(g, "memory-global-deadbeef.md"), "w", encoding="utf-8").write(
            "---\nkind: memory\n---\n本项目 ORM 统一使用 Drizzle，禁用其他 ORM")
        p = os.path.join(self.root, "projects", "demo-3f9a1c", "clients", "zcode", "sessions")
        os.makedirs(p)
        open(os.path.join(p, "2026-10-20-session-abc.md"), "w", encoding="utf-8").write(
            "---\nkind: session\n---\n\n## 轮 1\n**用户**：同步冲突怎么解\n\n## 轮 2\n**助手**：改用队列串行写入")
        open(os.path.join(self.root, "projects", "demo-3f9a1c", "clients", "zcode", "rule-project-cafe1234.md"), "w", encoding="utf-8").write(
            "---\nkind: rule\n---\n部署规则：先跑测试再发布")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        env = dict(os.environ, MEMORY_HUB_ROOT=self.root)
        env.pop("DUTI_MEMORY_ROOT", None)
        return subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True, env=env)

    def test_search_hits_across_partitions(self):
        r = self.run_cli("Drizzle")
        self.assertEqual(r.returncode, 0)
        self.assertIn("memory-global-deadbeef.md", r.stdout)

    def test_json_output_is_parseable(self):
        r = self.run_cli("同步冲突", "--json")
        data = json.loads(r.stdout)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["query"], "同步冲突")
        self.assertTrue(data["hits"][0]["file"].endswith("2026-10-20-session-abc.md"))
        self.assertIn("队列串行写入", data["hits"][0]["excerpt"])  # 会话命中按轮分节给原文

    def test_client_filter_excludes_other_clients(self):
        r = self.run_cli("Drizzle", "--client", "zcode", "--json")
        self.assertEqual(json.loads(r.stdout)["count"], 0)

    def test_kind_filter(self):
        r = self.run_cli("发布", "--kind", "rule", "--json")
        self.assertEqual(json.loads(r.stdout)["count"], 1)

    def test_no_hit_reports_empty(self):
        r = self.run_cli("不存在的关键词xyz", "--json")
        self.assertEqual(json.loads(r.stdout)["count"], 0)
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
