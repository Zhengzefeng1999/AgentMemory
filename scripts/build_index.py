#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AgentMemory 索引重建工具：扫描 bank/ 全部 md，重建 SQLite 索引"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from memory_tool import rebuild_index, _utf8

if __name__ == "__main__":
    _utf8("")
    rebuild_index(verbose=True)
