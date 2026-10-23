"""setup.py 的钩子合并与 TOML 补齐测试：幂等安装、共用文件归属判定、标识校正、用户设置保留。"""
import importlib.util
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SPEC = importlib.util.spec_from_file_location(
    "memory_hub_setup",
    os.path.join(REPO, "skills", "memory-hub-setup", "scripts", "setup.py"))
setup = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(setup)

FLAT = {"hooks": {"shape": "flat", "events": ["Stop"], "timeoutPlacement": "handler-seconds", "timeout": 30}}
ZCODE = {"hooks": {"shape": "zcode", "events": ["Stop"], "timeoutPlacement": "root-ms", "timeout": 30000}}
IDENTITY = lambda x: x  # noqa: E731


class MergeHooksFlatTest(unittest.TestCase):
    def test_install_then_idempotent(self):
        cfg = {}
        cfg, written, skipped, conflicts, fixed = setup.merge_hooks(cfg, FLAT, "trae-cn", resolve=IDENTITY)
        self.assertEqual(written, ["Stop"])
        self.assertEqual(skipped, [])
        self.assertEqual(conflicts, [])
        self.assertFalse(fixed)
        handler = cfg["hooks"]["Stop"][0]["hooks"][0]
        self.assertIn("--client trae-cn", handler["command"])
        self.assertEqual(handler["timeout"], 30)
        cfg, written, skipped, conflicts, fixed = setup.merge_hooks(cfg, FLAT, "trae-cn", resolve=IDENTITY)
        self.assertEqual(written, [])
        self.assertEqual(skipped, ["Stop"])
        self.assertEqual(conflicts, [])
        self.assertEqual(len(cfg["hooks"]["Stop"]), 1)

    def test_foreign_hook_is_reported_not_rewritten(self):
        command = "python3 memory_hub_hook.py --client trae-work --event Stop"
        cfg = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": command}]}]}}
        cfg, written, skipped, conflicts, fixed = setup.merge_hooks(cfg, FLAT, "trae-cn", resolve=IDENTITY)
        self.assertEqual(written, [])
        self.assertEqual(skipped, [])
        self.assertEqual(len(conflicts), 1)
        self.assertIn("trae-work", conflicts[0])
        self.assertEqual(cfg["hooks"]["Stop"][0]["hooks"][0]["command"], command)
        self.assertFalse(fixed)

    def test_stale_id_of_own_hook_is_corrected(self):
        command = "python3 memory_hub_hook.py --client trae-cn --event Stop"
        cfg = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": command}]}]}}
        # resolve 模拟中台登记：trae-cn 的规范标识已改为 mytrae
        resolve = lambda x: {"trae-cn": "mytrae"}.get(x, x)  # noqa: E731
        cfg, written, skipped, conflicts, fixed = setup.merge_hooks(cfg, FLAT, "mytrae", resolve=resolve)
        self.assertEqual(written, [])
        self.assertEqual(skipped, ["Stop"])
        self.assertEqual(conflicts, [])
        self.assertTrue(fixed)
        self.assertIn("--client mytrae", cfg["hooks"]["Stop"][0]["hooks"][0]["command"])
        self.assertNotIn("--client trae-cn", cfg["hooks"]["Stop"][0]["hooks"][0]["command"])


class MergeHooksZcodeTest(unittest.TestCase):
    def test_existing_user_settings_are_preserved(self):
        cfg = {"hooks": {"enabled": False, "timeoutMs": 1}}
        cfg, written, skipped, conflicts, fixed = setup.merge_hooks(cfg, ZCODE, "zcode", resolve=IDENTITY)
        self.assertEqual(written, ["Stop"])
        self.assertEqual(cfg["hooks"]["enabled"], False)
        self.assertEqual(cfg["hooks"]["timeoutMs"], 1)
        self.assertIn("Stop", cfg["hooks"]["events"])

    def test_defaults_written_when_absent(self):
        cfg = {}
        cfg, written, skipped, conflicts, fixed = setup.merge_hooks(cfg, ZCODE, "zcode", resolve=IDENTITY)
        self.assertEqual(written, ["Stop"])
        self.assertTrue(cfg["hooks"]["enabled"])
        self.assertEqual(cfg["hooks"]["timeoutMs"], 30000)
        handler = cfg["hooks"]["events"]["Stop"][0]["hooks"][0]
        self.assertIn("--client zcode", handler["command"])
        self.assertTrue(handler["enabled"])


class PatchTomlClientTest(unittest.TestCase):
    def test_args_with_comma_in_value_and_env_value_are_corrected(self):
        text = "\n".join([
            "# --- memory-hub (added by memory-hub-setup) ---",
            "[mcp_servers.memory-hub]",
            'command = "memory-hub"',
            'args = ["node", "/opt/tools,dir/server.js", "--client", "old-id"]',
            "",
            "[mcp_servers.memory-hub.env]",
            'MEMORY_HUB_CLIENT_ID = "old-id"',
            "",
        ])
        new_text, changed = setup.patch_toml_client(text, "new-id", "mcp_servers")
        self.assertTrue(changed)
        self.assertIn('"--client", "new-id"', new_text)
        self.assertIn('"/opt/tools,dir/server.js"', new_text)
        self.assertIn('MEMORY_HUB_CLIENT_ID = "new-id"', new_text)

    def test_missing_client_argument_is_appended(self):
        text = "\n".join([
            "# --- memory-hub (added by memory-hub-setup) ---",
            "[mcp_servers.memory-hub]",
            'command = "memory-hub"',
            'args = ["memory-hub"]',
            "",
        ])
        new_text, changed = setup.patch_toml_client(text, "zcode", "mcp_servers")
        self.assertTrue(changed)
        self.assertIn('"--client", "zcode"', new_text)


if __name__ == "__main__":
    unittest.main()
