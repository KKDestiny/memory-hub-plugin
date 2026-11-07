"""memory_hub_export.py 的测试：聚合过滤、命名与路由、增量跳过、脱敏落盘、会话摘要截断。"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "skills", "memory-hub-export", "scripts", "memory_hub_export.py")

_SPEC = importlib.util.spec_from_file_location("mh_export", SCRIPT)
mh = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mh)


class SplitFrontmatterTest(unittest.TestCase):
    def test_parses_attributes_and_body(self):
        attrs, body = mh.split_frontmatter("---\nkind: memory\nsummary: 摘要\n---\n\n正文")
        self.assertEqual(attrs["kind"], "memory")
        self.assertEqual(attrs["summary"], "摘要")
        self.assertEqual(body, "正文")

    def test_plain_text_returns_empty_attrs(self):
        attrs, body = mh.split_frontmatter("没有 frontmatter 的正文")
        self.assertEqual(attrs, {})
        self.assertEqual(body, "没有 frontmatter 的正文")


class CollectDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "mem")
        os.makedirs(self.dir)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, content):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
            f.write(content)

    def test_aggregates_same_kind_and_excludes_others(self):
        self.write("a.md", "---\nkind: memory\n---\n记忆甲")
        self.write("b.md", "---\nkind: memory\n---\n记忆乙")
        self.write("c.md", "---\nkind: rule\n---\n规则丙")  # 其他类别：排除
        self.write("2026-01-01-session-x.md", "---\nkind: memory\n---\n会话")  # 会话文件：排除
        self.write("done.md", "---\nkind: memory\naggregated: true\n---\n旧聚合")  # 已聚合：排除
        body, count, note, used = mh.collect_dir(self.dir, "memory", "", None)
        self.assertEqual(count, 2)
        self.assertIn("## a.md", body)
        self.assertIn("## b.md", body)
        self.assertNotIn("规则丙", body)
        self.assertNotIn("旧聚合", body)

    def test_fixed_name_fallback_without_frontmatter(self):
        self.write("project-memory.md", "裸文件记忆")
        body, count, _, used = mh.collect_dir(self.dir, "memory", "", None)
        self.assertEqual(count, 1)
        self.assertIn("裸文件记忆", body)

    def test_missing_directory_returns_none(self):
        body, count, note, used = mh.collect_dir(os.path.join(self.tmp.name, "nope"), "memory", "", None)
        self.assertIsNone(body)

    def test_glob_pattern_filters_files(self):
        self.write("memory-a.md", "---\nkind: memory\n---\n甲")
        self.write("memory-b.md", "---\nkind: memory\n---\n乙")
        body, count, _, _ = mh.collect_dir(self.dir, "memory", "", "memory-a")
        self.assertEqual(count, 1)
        self.assertNotIn("乙", body)


class CliExportTest(unittest.TestCase):
    """经子进程走完整 CLI：环境变量隔离数据根，验证 JSON 输出与落盘行为。"""

    def run_cli(self, *args, root):
        env = dict(os.environ, MEMORY_HUB_ROOT=root)
        # 清掉可能存在的旧名变量，保证测试只受 MEMORY_HUB_ROOT 控制
        env.pop("DUTI_MEMORY_ROOT", None)
        return subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True, env=env)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_memory_export_then_incremental_skip(self):
        src = os.path.join(self.root, "src.md")
        open(src, "w", encoding="utf-8").write("记忆内容")
        r1 = self.run_cli("--client", "t", "--kind", "memory", "--text-file", src, root=self.root)
        out1 = json.loads(r1.stdout)
        self.assertTrue(out1["ok"])
        self.assertTrue(out1["wrote"])
        self.assertTrue(os.path.exists(out1["file"]))
        self.assertRegex(os.path.basename(out1["file"]), r"^memory-global-[0-9a-f]{8}\.md$")
        r2 = self.run_cli("--client", "t", "--kind", "memory", "--text-file", src, root=self.root)
        out2 = json.loads(r2.stdout)
        self.assertTrue(out2["unchanged"])
        self.assertFalse(out2["wrote"])  # 内容未变化：不重复写盘

    def test_sensitive_content_is_redacted_on_disk(self):
        src = os.path.join(self.root, "secret.md")
        open(src, "w", encoding="utf-8").write("密钥 sk-abcdef0123456789abcdef 之外的内容")
        r = self.run_cli("--client", "t", "--kind", "memory", "--text-file", src, root=self.root)
        out = json.loads(r.stdout)
        self.assertEqual(out["redacted"], 1)
        text = open(out["file"], encoding="utf-8").read()
        self.assertIn("[REDACTED]", text)
        self.assertNotIn("sk-abcdef", text)

    def test_project_routing_creates_partition_and_meta(self):
        proj = os.path.join(self.root, "demo-app")
        os.makedirs(proj)
        src = os.path.join(self.root, "p.md")
        open(src, "w", encoding="utf-8").write("项目记忆")
        r = self.run_cli("--client", "t", "--kind", "memory", "--text-file", src, "--project", proj, root=self.root)
        out = json.loads(r.stdout)
        self.assertTrue(out["ok"])
        rel = os.path.relpath(out["file"], self.root).replace(os.sep, "/")
        self.assertRegex(rel, r"^projects/demo-app-[0-9a-f]{6}/clients/t/memory-project-[0-9a-f]{8}\.md$")
        meta = json.load(open(os.path.join(self.root, "projects", rel.split("/")[1], "project.json"), encoding="utf-8"))
        self.assertEqual(meta["name"], "demo-app")

    def test_session_summary_truncated_to_300(self):
        long_summary = "长" * 400
        r = self.run_cli("--client", "t", "--kind", "session", "--session", "s1", "--summary", long_summary, root=self.root)
        out = json.loads(r.stdout)
        self.assertTrue(out["ok"])
        text = open(out["file"], encoding="utf-8").read()
        self.assertIn("长" * 300, text)
        self.assertNotIn("长" * 301, text)

    def test_missing_text_file_reports_skip(self):
        r = self.run_cli("--client", "t", "--kind", "memory", root=self.root)
        out = json.loads(r.stdout)
        self.assertFalse(out["ok"])
        self.assertTrue(out["skip"])


if __name__ == "__main__":
    unittest.main()
