"""memory_hub_hook.py 的测试：转录解析、摘要生成、会话→cwd 映射、emit_session 文件通道端到端、import-all 清点。"""
import importlib.util
import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "skills", "memory-hub-export", "scripts", "memory_hub_hook.py")

_SPEC = importlib.util.spec_from_file_location("mh_hook", SCRIPT)
hook = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(hook)


class BuildTurnsFromTranscriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.jsonl")
        lines = [
            '{"type":"system","content":"环境信息"}',
            '{"message":{"role":"user","content":[{"type":"text","text":"怎么配置数据库"}]}}',
            '{"message":{"role":"assistant","content":[{"type":"text","text":"先看配置文件"},{"type":"tool_use","name":"Read","input":{"file_path":"/etc/app.conf"}}]}}',
            '{"message":{"role":"assistant","content":"数据库地址在环境变量里"}}',
        ]
        open(self.path, "w", encoding="utf-8").write("\n".join(lines))

    def tearDown(self):
        self.tmp.cleanup()

    def test_parses_turns_with_tools_and_merges_assistant_messages(self):
        turns = hook.build_turns_from_transcript(self.path)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["user"], "怎么配置数据库")
        self.assertIn("先看配置文件", turns[0]["assistant"])
        self.assertIn("数据库地址在环境变量里", turns[0]["assistant"])  # 连续 assistant 消息合并进同一轮
        self.assertIn("Read", turns[0]["tools"])

    def test_missing_file_returns_empty(self):
        self.assertEqual(hook.build_turns_from_transcript("/nonexistent.jsonl"), [])

    def test_summarize_from_transcript(self):
        summary = hook.summarize_from_transcript(self.path)
        self.assertIsNotNone(summary)
        self.assertIn("诉求", summary)
        self.assertIn("结论", summary)
        self.assertIn("Read", summary)

    def test_turns_summary(self):
        turns = [{"n": 1, "user": "问个问题", "assistant": "这是回答", "tools": []}]
        summary = hook.turns_summary(turns)
        self.assertIn("诉求：问个问题", summary)
        self.assertIn("结论：这是回答", summary)
        self.assertIn("轮次：1", summary)


class SessionCwdMapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        hook.ROOT = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_put_get_roundtrip(self):
        hook.session_cwd_put("clientA", "s1", "/tmp/proj")
        self.assertEqual(hook.session_cwd_get("clientA", "s1"), "/tmp/proj")
        self.assertEqual(hook.session_cwd_get("clientA", "unknown"), "")

    def test_missing_map_returns_empty(self):
        self.assertEqual(hook.session_cwd_get("nobody", "s1"), "")


class EmitSessionFileChannelTest(unittest.TestCase):
    """端到端：无管理端（.state/app.json 缺失）时，emit_session 经文件通道写出会话并完成脱敏。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        hook.ROOT = self.tmp.name
        hook._HTTP_DISABLED = False
        # emit_session 的文件通道以子进程运行导出脚本，数据根取自环境变量：
        # 必须同步设置，否则测试数据会写进真实 ~/MemoryHub
        self._saved_env = os.environ.get("MEMORY_HUB_ROOT")
        os.environ["MEMORY_HUB_ROOT"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()
        if self._saved_env is None:
            os.environ.pop("MEMORY_HUB_ROOT", None)
        else:
            os.environ["MEMORY_HUB_ROOT"] = self._saved_env

    def test_writes_redacted_session_file(self):
        turns = [
            {"n": 1, "time": "10:00", "user": "连接串 api_key=hunter2secretvalue 怎么配",
             "assistant": "按文档配置即可", "tools": ["Edit"]},
        ]
        ok = hook.emit_session("testclient", "sess-1", os.path.join(self.tmp.name, "proj"),
                               turns, title="演示会话")
        self.assertTrue(ok)
        sessions_dir = os.path.join(self.tmp.name, "projects", self.slug_of("proj"),
                                    "clients", "testclient", "sessions")
        files = os.listdir(sessions_dir)
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0], r"^\d{4}-\d{2}-\d{2}-session-sess-1\.md$")
        text = open(os.path.join(sessions_dir, files[0]), encoding="utf-8").read()
        self.assertIn("[REDACTED]", text)          # 键值对凭据已被脱敏
        self.assertNotIn("hunter2secretvalue", text)
        self.assertIn("title: 演示会话", text)
        self.assertIn("format: 2", text)
        self.assertIn("## 轮 1", text)

    def slug_of(self, name):
        from project_id import project_slug  # hook 模块已把 _shared 加入 sys.path
        return project_slug(os.path.join(self.tmp.name, name))


class ImportAllTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        hook.ROOT = os.path.join(self.tmp.name, "hub")
        os.makedirs(hook.ROOT, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_dry_run_counts_jsonl_sessions(self):
        sessions = os.path.join(self.tmp.name, "sessions")
        os.makedirs(sessions)
        open(os.path.join(sessions, "a.jsonl"), "w", encoding="utf-8").write(
            '{"message":{"role":"user","content":"问题"}}\n{"message":{"role":"assistant","content":"回答"}}\n')
        open(os.path.join(sessions, "b.jsonl"), "w", encoding="utf-8").write(
            '{"message":{"role":"user","content":"另一问"}}\n')
        rc = hook.import_all("testclient", sessions_dir=sessions, dry=True)
        self.assertEqual(rc, 0)

    def test_no_sources_returns_two(self):
        rc = hook.import_all("testclient", sessions_dir=os.path.join(self.tmp.name, "empty"), dry=True)
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
