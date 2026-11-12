"""MCP server 子进程客户端（测试共用）：按 stdio JSON-RPC 逐行收发。"""
import json
import os
import shutil
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(REPO, "mcp", "memory-hub-mcp.mjs")
EXAMPLES_HUB = os.path.join(REPO, "examples", "hub")

NODE = shutil.which("node")


class McpClient:
    def __init__(self, root):
        env = dict(os.environ, MEMORY_HUB_ROOT=root)
        env.pop("DUTI_MEMORY_ROOT", None)
        self.proc = subprocess.Popen(
            [NODE, SERVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, env=env, bufsize=1)

    def request(self, req_id, method, params=None):
        req = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            req["params"] = params
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise AssertionError("MCP server 提前退出")
            msg = json.loads(line)
            if msg.get("id") == req_id:
                return msg

    def initialize(self):
        return self.request(0, "initialize", {"clientInfo": {"name": "test"}})

    def list_tools(self):
        return self.request(1, "tools/list")["result"]["tools"]

    def call(self, req_id, name, arguments=None):
        return self.request(req_id, "tools/call", {"name": name, "arguments": arguments or {}})

    def call_json(self, req_id, name, arguments=None):
        """调用工具并把 text 结果解析为 JSON；isError 时抛出消息。"""
        resp = self.call(req_id, name, arguments)
        result = resp["result"]
        text = result["content"][0]["text"]
        if result.get("isError"):
            raise RuntimeError(text)
        return json.loads(text)

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
