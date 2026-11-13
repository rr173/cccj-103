"""定位随仓库携带的 duckdb 依赖（.vendor/duckdb_pkg）。

运行环境可能没有系统级 duckdb/pip；仓库在 .vendor 下解包了与解释器
版本/架构匹配的 wheel。任何需要 duckdb 的模块先 import 本模块。
"""
from __future__ import annotations

import sys
from pathlib import Path


def ensure_vendor_duckdb() -> None:
    try:
        import duckdb  # noqa: F401

        return
    except ImportError:
        pass
    root = Path(__file__).resolve().parents[2]
    candidate = root / ".vendor" / "duckdb_pkg"
    if candidate.is_dir() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))
    import duckdb  # noqa: F401
