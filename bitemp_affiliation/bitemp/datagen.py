"""数据生成器：随机但可复现（种子固定）的断言批与事实。

生成的情形覆盖：嵌套重叠、无穷尾区间、多时区 recorded_at（含等价时刻）、
晚到的替代链、revision 相同需要决胜、空档。另提供各类无效批生成器。
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone

ORGS = ["ORG-NORTH", "ORG-SOUTH", "ORG-EAST", "ORG-WEST", "ORG-CENTRAL"]
TZS = [
    timezone.utc,
    timezone(timedelta(hours=8)),    # Asia/Shanghai
    timezone(timedelta(hours=-5)),   # America/New_York (冬令)
    timezone(timedelta(hours=1)),    # Europe/Berlin (冬令)
    timezone(timedelta(hours=9)),    # Asia/Tokyo
]


class Generator:
    def __init__(self, seed: int = 42, n_researchers: int = 6):
        self.rng = random.Random(seed)
        self.researchers = [f"R{i:03d}" for i in range(1, n_researchers + 1)]
        self._seq = 0
        self._fact_seq = 0
        self.history: list[dict] = []  # 已生成的全部有效断言（原始 dict）
        self._superseded: set[str] = set()  # 已被替代的断言 id（替代链不允许分叉）

    def _aid(self) -> str:
        self._seq += 1
        return f"A{self._seq:05d}"

    def _rand_recorded_at(self) -> str:
        """随机时区的 ISO 时间串；不同时区可能表示同一时刻。"""
        base = datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(
            seconds=self.rng.randrange(0, 2 * 365 * 24 * 3600))
        return base.astimezone(self.rng.choice(TZS)).isoformat()

    def batch(self, n: int = 12, supersede_prob: float = 0.35,
              infinite_tail_prob: float = 0.25) -> list[dict]:
        rows = []
        for _ in range(n):
            rid = self.rng.choice(self.researchers)
            vf = date(2023, 1, 1) + timedelta(days=self.rng.randrange(0, 1000))
            vt = None if self.rng.random() < infinite_tail_prob else \
                vf + timedelta(days=self.rng.randrange(30, 400))
            sup = None
            cands = [a for a in self.history
                     if a["researcher_id"] == rid
                     and a["assertion_id"] not in self._superseded]
            if cands and self.rng.random() < supersede_prob:
                sup = self.rng.choice(cands)["assertion_id"]
                self._superseded.add(sup)
            row = dict(
                assertion_id=self._aid(),
                researcher_id=rid,
                valid_from=vf.isoformat(),
                valid_to=None if vt is None else vt.isoformat(),
                organization=self.rng.choice(ORGS),
                recorded_at=self._rand_recorded_at(),
                revision=self.rng.randrange(0, 4),  # 小范围，制造同分决胜
                supersedes=sup,
            )
            rows.append(row)
        self.history.extend(rows)
        return rows

    def facts(self, n: int = 40) -> list[dict]:
        rows = []
        for _ in range(n):
            self._fact_seq += 1
            rows.append(dict(
                fact_id=f"F{self._fact_seq:05d}",
                researcher_id=self.rng.choice(self.researchers),
                event_date=(date(2023, 1, 1)
                            + timedelta(days=self.rng.randrange(0, 1400))).isoformat(),
                metric=round(self.rng.uniform(0, 100), 2),
            ))
        return rows

    # -------------------------------------------------- 无效批（负例）

    def invalid_batch_cycle(self) -> list[dict]:
        """两条新断言互相 supersedes，构成环。"""
        a1, a2 = self._aid(), self._aid()
        rid = self.rng.choice(self.researchers)
        base = dict(researcher_id=rid, valid_from="2024-01-01", valid_to=None,
                    organization="ORG-NORTH", revision=1)
        return [
            dict(base, assertion_id=a1, recorded_at="2025-01-01T00:00:00+00:00",
                 supersedes=a2),
            dict(base, assertion_id=a2, recorded_at="2025-01-02T00:00:00+00:00",
                 supersedes=a1),
        ]

    def invalid_batch_dangling_supersedes(self) -> list[dict]:
        return [dict(assertion_id=self._aid(),
                     researcher_id=self.rng.choice(self.researchers),
                     valid_from="2024-01-01", valid_to=None,
                     organization="ORG-NORTH",
                     recorded_at="2025-01-01T00:00:00+00:00",
                     revision=1, supersedes="A99999")]

    def invalid_batch_cross_researcher(self) -> list[dict]:
        """supersedes 指向另一位研究者的断言。"""
        by_rid: dict[str, str] = {}
        for a in self.history:
            by_rid.setdefault(a["researcher_id"], a["assertion_id"])
        if len(by_rid) < 2:
            raise RuntimeError("历史断言不足，先处理几批有效数据")
        rids = sorted(by_rid)
        return [dict(assertion_id=self._aid(), researcher_id=rids[0],
                     valid_from="2024-01-01", valid_to=None,
                     organization="ORG-SOUTH",
                     recorded_at="2025-01-01T00:00:00+00:00",
                     revision=1, supersedes=by_rid[rids[1]])]

    def invalid_batch_bad_interval(self) -> list[dict]:
        return [dict(assertion_id=self._aid(),
                     researcher_id=self.rng.choice(self.researchers),
                     valid_from="2024-06-01", valid_to="2024-01-01",
                     organization="ORG-EAST",
                     recorded_at="2025-01-01T00:00:00+00:00",
                     revision=1, supersedes=None)]

    def invalid_batch_naive_timestamp(self) -> list[dict]:
        return [dict(assertion_id=self._aid(),
                     researcher_id=self.rng.choice(self.researchers),
                     valid_from="2024-01-01", valid_to=None,
                     organization="ORG-WEST",
                     recorded_at="2025-01-01 12:00:00",  # 无时区
                     revision=1, supersedes=None)]
