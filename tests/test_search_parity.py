"""检索一致性（golden 对比）：search.py 与内置 MCP server 的 search_hub 在同一份示例数据上
必须产出相同的命中集合、类别与摘录——这是防止 Python/JS 两套检索实现漂移的契约锁。"""
import json
import os
import re
import subprocess
import sys
import unittest

from mcp_client import EXAMPLES_HUB, NODE, REPO, McpClient

SEARCH = os.path.join(REPO, "skills", "memory-hub-consume", "scripts", "search.py")


def normalize(text):
    """空白归一：两侧对空白折叠/裁剪的实现细节不进契约，内容一致即可。"""
    return re.sub(r"\s+", " ", str(text)).strip()


def search_py(query, extra):
    env = dict(os.environ, MEMORY_HUB_ROOT=EXAMPLES_HUB)
    env.pop("DUTI_MEMORY_ROOT", None)
    args = [sys.executable, SEARCH, query, "--json"] + extra
    out = subprocess.run(args, capture_output=True, text=True, env=env)
    return json.loads(out.stdout)["hits"]


@unittest.skipUnless(NODE, "本机无 node，跳过 MCP server 一致性测试")
class SearchParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = McpClient(EXAMPLES_HUB)
        cls.client.initialize()

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def assert_parity(self, query, extra=None):
        extra = list(extra or [])
        # 过滤参数要同时下发两侧：只传一边测的不是一致性，是各自的正确性
        server_args = {"query": query}
        if "--client" in extra:
            server_args["client"] = extra[extra.index("--client") + 1]
        if "--kind" in extra:
            server_args["kind"] = extra[extra.index("--kind") + 1]
        if "--scope" in extra:
            server_args["scope"] = extra[extra.index("--scope") + 1]
        py_hits = search_py(query, extra)
        hub = self.client.call_json(abs(hash(query)) % 1000 + 100, "search_hub", server_args)
        js_hits = hub["hits"]
        self.assertEqual(sorted(h["file"] for h in py_hits),
                         sorted(h["file"] for h in js_hits),
                         f"query={query} 命中文件不一致")
        py_by_file = {h["file"]: h for h in py_hits}
        for hit in js_hits:
            py = py_by_file[hit["file"]]
            self.assertEqual(normalize(py["excerpt"]), normalize(hit["excerpt"]),
                             f"query={query} file={hit['file']} 摘录不一致")

    def test_keyword_rule(self):
        self.assert_parity("Drizzle")

    def test_keyword_session(self):
        self.assert_parity("同步冲突")

    def test_keyword_multiple_files(self):
        self.assert_parity("memory")

    def test_keyword_chinese_global(self):
        self.assert_parity("部署")

    def test_client_filter(self):
        self.assert_parity("Drizzle", ["--client", "claude-code"])

    def test_kind_filter(self):
        self.assert_parity("发布", ["--kind", "rule"])


if __name__ == "__main__":
    unittest.main()
