#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""项目标识（project slug）：同一目录在中台只对应一个分区，标识算法只允许有这一处实现。

本文件是管理端参考实现（normalizeProjectPath 与 projectSlug）的 Python 镜像，两者必须保持一致。

规则：
    abspath（相对路径按当前目录解析）
    → realpath 解析符号链接（不存在的尾部路径段原样保留）
    → 去除尾部路径分隔符 → Windows 盘符大写 → Unicode NFC 归一化
    → 目录名 + "-" + sha256(归一化路径的 UTF-8 编码) 前 6 位

不得转小写，也不得把非 ASCII 目录名替换为固定值：同一目录会因大小写或中文目录名被不同客户端
计算为两个分区，导致数据分裂与授权记录重复。
"""
import hashlib
import os
import re
import unicodedata


def normalize_project_path(project_path):
    """项目路径归一化（与管理端 normalizeProjectPath 规则一致）。

    空输入返回空串：os.path.abspath("") 会返回当前目录，若不先行拦截，
    空的项目路径会被静默归属到调用方工作目录对应的分区。
    """
    raw = str(project_path or "").strip()
    if not raw:
        return ""
    raw = os.path.abspath(raw)
    try:
        abs_path = os.path.realpath(raw)
    except OSError:
        abs_path = raw
    abs_path = re.sub(r"[\\/]+$", "", str(abs_path or ""))
    if os.name == "nt" and re.match(r"^[a-zA-Z]:", abs_path):
        abs_path = abs_path[0].upper() + abs_path[1:]
    return unicodedata.normalize("NFC", abs_path)


def project_slug(project_path):
    """中台分区名 = 目录名 + "-" + sha256(归一化路径)[:6]；路径不可用时返回空串。"""
    norm = normalize_project_path(project_path)
    if not norm:
        return ""
    base = os.path.basename(norm) or norm
    digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:6]
    return "%s-%s" % (base, digest)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        print(project_slug(sys.argv[1]))
        sys.exit(0)
    print("project_id module (pass a path to print its slug)", file=sys.stderr)
    sys.exit(1)
