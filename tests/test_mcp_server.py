"""内置 MCP server 的协议与工具测试（node 子进程，走完整 stdio JSON-RPC）。"""
import unittest

from mcp_client import EXAMPLES_HUB, NODE, McpClient

SESSION_FILE = "projects/demo-3f9a1c/clients/claude-code/sessions/2026-10-20-session-3f2b1a.md"


@unittest.skipUnless(NODE, "本机无 node，跳过 MCP server 测试")
class McpProtocolTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = McpClient(EXAMPLES_HUB)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def test_initialize(self):
        result = self.client.initialize()["result"]
        self.assertEqual(result["serverInfo"]["name"], "memory-hub")
        self.assertIn("tools", result["capabilities"])

    def test_tools_list_has_seven_tools(self):
        names = sorted(t["name"] for t in self.client.list_tools())
        self.assertEqual(names, sorted([
            "search_hub", "read_entry", "get_session",
            "list_partitions", "list_clients", "read_catalog", "hub_stats"]))

    def test_search_hub(self):
        data = self.client.call_json(10, "search_hub", {"query": "Drizzle"})
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["hits"][0]["file"],
                         "projects/demo-3f9a1c/clients/claude-code/rule-project-cafe1234.md")
        self.assertEqual(data["hits"][0]["kind"], "rule")

    def test_search_hub_client_filter(self):
        data = self.client.call_json(11, "search_hub", {"query": "Drizzle", "client": "zcode"})
        self.assertEqual(data["count"], 0)

    def test_search_hub_session_sections(self):
        data = self.client.call_json(12, "search_hub", {"query": "同步冲突"})
        self.assertEqual(data["count"], 1)
        self.assertIn("## 轮", data["hits"][0]["excerpt"])

    def test_read_entry(self):
        data = self.client.call_json(13, "read_entry", {"file": "global/clients/zcode/memory-global-c0ffee.md"})
        self.assertIn("部署惯例", data["content"])

    def test_read_entry_rejects_traversal(self):
        resp = self.client.call(14, "read_entry", {"file": "../../etc/hosts"})
        self.assertTrue(resp["result"].get("isError"))

    def test_get_session(self):
        data = self.client.call_json(15, "get_session", {"file": SESSION_FILE})
        self.assertEqual(data["meta"]["title"], "调通同步冲突的处理方案")
        self.assertEqual([t["n"] for t in data["turns"]], [1, 2])
        self.assertEqual(data["turns"][0]["time"], "10:02")
        self.assertIn("队列串行化", data["turns"][1]["text"])

    def test_get_session_rejects_non_session(self):
        resp = self.client.call(16, "get_session", {"file": "global/clients/zcode/memory-global-c0ffee.md"})
        self.assertTrue(resp["result"].get("isError"))

    def test_list_partitions(self):
        data = self.client.call_json(17, "list_partitions")
        self.assertEqual(len(data["partitions"]), 1)
        part = data["partitions"][0]
        self.assertEqual(part["dir"], "demo-3f9a1c")
        self.assertEqual(part["name"], "demo")
        self.assertEqual(part["entries"], 2)  # rule + session

    def test_list_clients_all_and_filtered(self):
        all_rows = self.client.call_json(18, "list_clients")["clients"]
        self.assertEqual([(r["client"], r["scope"], r["entries"]) for r in all_rows],
                         [("claude-code", "global", 1),
                          ("claude-code", "project", 2),
                          ("zcode", "global", 1)])
        rows = self.client.call_json(19, "list_clients", {"partition": "demo-3f9a1c"})["clients"]
        self.assertEqual([r["client"] for r in rows], ["claude-code"])
        self.assertEqual(rows[0]["entries"], 2)

    def test_read_catalog_missing_reports_error(self):
        resp = self.client.call(20, "read_catalog")
        self.assertTrue(resp["result"].get("isError"))
        self.assertIn("catalog", resp["result"]["content"][0]["text"])

    def test_hub_stats(self):
        data = self.client.call_json(21, "hub_stats")
        self.assertEqual(data["partitions"], 1)
        self.assertEqual(data["byKind"], {"memory": 2, "rule": 1, "session": 1})
        self.assertEqual(data["clients"], ["claude-code", "zcode"])


if __name__ == "__main__":
    unittest.main()
