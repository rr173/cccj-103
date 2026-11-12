"""JSONL 装载辅助：从磁盘读两类输入文件（事项 / 路由）。

每行一个 JSON 对象；空行忽略。读取时不做解释执行，只 ``json.loads``。
"""
from __future__ import annotations

import json
from pathlib import Path


def read_jsonl(path: str | Path) -> list[dict]:
    rows: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{lineno} 不是合法 JSON: {e}") from e
            if not isinstance(obj, dict):
                raise ValueError(f"{path}:{lineno} 必须是一个 JSON 对象")
            rows.append(obj)
    return rows


def write_jsonl(path: str | Path, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for obj in rows:
            f.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
