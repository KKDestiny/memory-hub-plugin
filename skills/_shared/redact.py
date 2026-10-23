#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导出内容的脱敏规则：将已知形态的凭据统一替换为 [REDACTED]。

覆盖三类形态：
  * 已知前缀的访问令牌（sk-、pk-、ghp_、xox… 等）
  * 键值对形式的凭据（api_key=、token:、password= 等）
  * 长随机串（48 位及以上的 Base64 形态字符串）

这是辅助措施而非完备的机密识别：无法覆盖全部凭据形态（例如中文键名的凭据），
不应作为敏感信息的唯一防线。文件通道与 HTTP 通道在发送前都必须经过本模块处理。
"""
import re

REDACT_TOKEN = re.compile(r"\b(?:sk|pk|ghp|gho|ghs|xox[baprs])[-_][A-Za-z0-9_-]{16,}\b")
# 值字符集排除 "["：使 "[REDACTED]" 不会被再次匹配，保证重复脱敏幂等且计数准确
REDACT_KV = re.compile(r"((?:api[_-]?key|token|secret|password|passwd|pwd)\s*[:=]\s*)[\"']?[^\s\"',;\[]{6,}[\"']?", re.I)
REDACT_BLOB = re.compile(r"[A-Za-z0-9+/]{48,}={0,2}")


def redact(text):
    """对文本执行脱敏。返回 (脱敏后的文本, 替换次数)。"""
    hits = [0]

    def sub(_match):  # 替换回调必须接收 match 参数，否则 re.sub 调用时抛出 TypeError
        hits[0] += 1
        return "[REDACTED]"

    def kv_sub(match):
        hits[0] += 1
        return match.group(1) + "[REDACTED]"

    out = REDACT_TOKEN.sub(sub, text)
    out = REDACT_KV.sub(kv_sub, out)
    out = REDACT_BLOB.sub(sub, out)
    return out, hits[0]


def redact_turns(turns):
    """对逐轮会话（[{n, time, user, assistant, tools[]}]）的用户与助手文本、工具标注执行脱敏。

    返回 (新的轮次列表, 替换次数)；输入不做原地修改，非字典元素原样保留。
    """
    hits = 0
    out = []
    for turn in turns or []:
        if not isinstance(turn, dict):
            out.append(turn)
            continue
        safe = dict(turn)
        for key in ("user", "assistant"):
            if isinstance(safe.get(key), str):
                safe[key], n = redact(safe[key])
                hits += n
        if isinstance(safe.get("tools"), list):
            tools = []
            for item in safe["tools"]:
                if isinstance(item, str):
                    item, n = redact(item)
                    hits += n
                tools.append(item)
            safe["tools"] = tools
        out.append(safe)
    return out, hits
