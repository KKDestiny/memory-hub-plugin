"""redact 的规则测试：令牌、键值对、长随机串与逐轮会话的脱敏。"""
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "skills", "_shared"))
from redact import redact, redact_turns  # noqa: E402


class RedactTextTest(unittest.TestCase):
    def test_prefixed_token_is_replaced(self):
        out, hits = redact("connect sk-abcdef0123456789abcdef now")
        self.assertEqual(out, "connect [REDACTED] now")
        self.assertEqual(hits, 1)

    def test_key_value_pair_is_replaced(self):
        out, hits = redact("api_key = 'supersecretvalue123'")
        self.assertEqual(out, "api_key = [REDACTED]")
        self.assertEqual(hits, 1)

    def test_long_random_blob_is_replaced(self):
        out, hits = redact("data: " + "A" * 48)
        self.assertEqual(out, "data: [REDACTED]")
        self.assertEqual(hits, 1)

    def test_plain_text_is_untouched(self):
        out, hits = redact("普通文本 without secrets")
        self.assertEqual(out, "普通文本 without secrets")
        self.assertEqual(hits, 0)


class RedactTurnsTest(unittest.TestCase):
    def test_turn_fields_are_redacted_and_structure_preserved(self):
        turns = [{"n": 1, "time": "10:00",
                  "user": "token=ghp_abcdef0123456789abcdef",
                  "assistant": "好的，[REDACTED] 已处理",
                  "tools": ["git push --token=xxxxxxxxxxxxxxxxxxxx"]}]
        out, hits = redact_turns(turns)
        self.assertEqual(out[0]["n"], 1)
        self.assertEqual(out[0]["time"], "10:00")
        self.assertEqual(out[0]["user"], "token=[REDACTED]")
        self.assertEqual(out[0]["assistant"], "好的，[REDACTED] 已处理")
        self.assertEqual(out[0]["tools"], ["git push --token=[REDACTED]"])
        self.assertEqual(hits, 2)

    def test_input_is_not_mutated(self):
        turns = [{"n": 1, "user": "pwd=hunter222222", "assistant": "", "tools": []}]
        redact_turns(turns)
        self.assertEqual(turns[0]["user"], "pwd=hunter222222")

    def test_non_dict_elements_are_preserved(self):
        out, hits = redact_turns(["keep", {"n": 1, "user": "x" * 60, "assistant": "", "tools": []}])
        self.assertEqual(out[0], "keep")
        self.assertEqual(hits, 1)


if __name__ == "__main__":
    unittest.main()
