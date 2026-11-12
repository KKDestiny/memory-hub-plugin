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


class MergeMcpJsonTest(unittest.TestCase):
    ADAPTER = {"mcp": {"keys": ["mcpServers"], "serverExtra": {"type": "stdio"}}}

    def setUp(self):
        # mcp_server 读取数据根与端点文件：隔离到临时目录，避免依赖本机真实中台
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = os.environ.get("MEMORY_HUB_ROOT")
        os.environ["MEMORY_HUB_ROOT"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()
        if self._saved is None:
            os.environ.pop("MEMORY_HUB_ROOT", None)
        else:
            os.environ["MEMORY_HUB_ROOT"] = self._saved

    def test_plugin_default_creates_entry_without_client_id(self):
        cfg, written = setup.merge_mcp_json({}, self.ADAPTER, "zcode", "plugin")
        self.assertEqual(written, "created")
        entry = cfg["mcpServers"]["memory-hub"]
        self.assertEqual(entry["command"], "node")
        self.assertEqual(entry["args"], [setup.PLUGIN_MCP])
        self.assertNotIn(setup.CLIENT_ARG, entry["args"])
        self.assertEqual(entry["env"], {"MEMORY_HUB_ROOT": self.tmp.name})
        self.assertNotIn("MEMORY_HUB_CLIENT_ID", entry["env"])
        self.assertEqual(entry["type"], "stdio")

    def test_plugin_mode_conflicts_with_existing_desktop_entry(self):
        desktop_entry = {"type": "stdio", "command": "node",
                         "args": ["/app/server.mjs", "--client", "zcode"],
                         "env": {"MEMORY_HUB_ROOT": "/x", "MEMORY_HUB_CLIENT_ID": "zcode"}}
        cfg = {"mcpServers": {"memory-hub": dict(desktop_entry)}}
        cfg2, written = setup.merge_mcp_json(cfg, self.ADAPTER, "zcode", "plugin")
        self.assertEqual(written, "conflict")
        self.assertEqual(cfg2["mcpServers"]["memory-hub"], desktop_entry)  # 不改写

    def test_plugin_entry_idempotent_and_root_corrected(self):
        cfg, written = setup.merge_mcp_json({}, self.ADAPTER, "zcode", "plugin")
        self.assertEqual(written, "created")
        cfg, written = setup.merge_mcp_json(cfg, self.ADAPTER, "zcode", "plugin")
        self.assertFalse(written)  # 完全一致：无改动
        cfg["mcpServers"]["memory-hub"]["env"]["MEMORY_HUB_ROOT"] = "/old/root"
        cfg, written = setup.merge_mcp_json(cfg, self.ADAPTER, "zcode", "plugin")
        self.assertEqual(written, "updated")  # 数据根漂移被校正
        self.assertEqual(cfg["mcpServers"]["memory-hub"]["env"]["MEMORY_HUB_ROOT"], self.tmp.name)

    def test_desktop_source_rewrites_plugin_entry(self):
        cfg = {"mcpServers": {"memory-hub": {"type": "stdio", "command": "node",
                                             "args": [setup.PLUGIN_MCP],
                                             "env": {"MEMORY_HUB_ROOT": self.tmp.name}}}}
        cfg2, written = setup.merge_mcp_json(cfg, self.ADAPTER, "myzcode", "desktop")
        self.assertEqual(written, "updated")
        entry = cfg2["mcpServers"]["memory-hub"]
        self.assertIn("--client", entry["args"])
        self.assertEqual(entry["env"]["MEMORY_HUB_CLIENT_ID"], "myzcode")
        self.assertNotIn(setup.PLUGIN_MCP, str(entry["args"]))

    def test_desktop_source_keeps_injecting_into_desktop_entry(self):
        cfg = {"mcpServers": {"memory-hub": {"type": "stdio", "command": "node",
                                             "args": ["/app/server.mjs", "--client", "old"],
                                             "env": {"MEMORY_HUB_ROOT": self.tmp.name,
                                                     "MEMORY_HUB_CLIENT_ID": "old"}}}}
        cfg2, written = setup.merge_mcp_json(cfg, self.ADAPTER, "zcode", "desktop")
        self.assertEqual(written, "updated")
        entry = cfg2["mcpServers"]["memory-hub"]
        self.assertEqual(entry["args"][entry["args"].index("--client") + 1], "zcode")
        self.assertEqual(entry["env"]["MEMORY_HUB_CLIENT_ID"], "zcode")


if __name__ == "__main__":
    unittest.main()
