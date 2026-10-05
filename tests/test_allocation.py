"""生产批次成本分摊算法单元测试（仅依赖标准库）。

可用 pytest 运行，也可直接：python3 tests/test_allocation.py
"""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.allocation import (  # noqa: E402
    BatchWindow,
    BatchWindowError,
    PieceView,
    allocate_accounting,
    allocate_piece,
    distribute_cents,
    validate_batch_windows,
)

UTC = timezone.utc
BASE = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


def t(minutes: float = 0) -> datetime:
    return BASE + timedelta(minutes=minutes)


def batch(no: str, start_m: float, end_m: float) -> BatchWindow:
    return BatchWindow(batch_no=no, start=t(start_m), end=t(end_m))


def piece(
    no: int,
    start_m: float,
    end_m: float,
    reason: str = "failure",
    amount_cents: int | None = 100,
) -> PieceView:
    priced = amount_cents is not None
    return PieceView(
        piece_no=no,
        start=t(start_m),
        end=t(end_m),
        reason=reason,
        minutes=int(end_m - start_m),
        priced=priced,
        rate_per_minute_micro=10_000 if priced else None,
        amount_cents=amount_cents,
    )


# ---------------------------------------------------------------- 批次窗口校验


def test_validate_batch_windows_ok_touching_allowed():
    validate_batch_windows(
        [batch("B-2", 60, 120), batch("B-1", 0, 60)]  # 端点相接 + 乱序
    )


def test_validate_batch_windows_rejects_duplicate_batch_no():
    try:
        validate_batch_windows([batch("B-1", 0, 60), batch("B-1", 60, 120)])
    except BatchWindowError:
        pass
    else:
        raise AssertionError("expected BatchWindowError for duplicate batch_no")


def test_validate_batch_windows_rejects_overlap():
    try:
        validate_batch_windows([batch("B-1", 0, 90), batch("B-2", 60, 120)])
    except BatchWindowError:
        pass
    else:
        raise AssertionError("expected BatchWindowError for overlapping windows")


# ---------------------------------------------------------------- 最大余数法分账


def test_distribute_cents_exact_split_no_remainder():
    out = distribute_cents(90, [("A", 30), ("B", 30), (None, 30)], 90)
    assert out == {"A": 30, "B": 30, None: 30}
    assert sum(out.values()) == 90


def test_distribute_cents_remainder_descending_then_batch_no_asc():
    # 100 分按 1/1/1 分钟分三份：余数相同，按批次号升序补给 B-1
    out = distribute_cents(100, [("B-2", 1), ("B-1", 1), (None, 1)], 3)
    assert out == {"B-1": 34, "B-2": 33, None: 33}
    assert sum(out.values()) == 100


def test_distribute_cents_unallocated_last_on_tie():
    # 余分只有 1 分，未分配与批次同余数时未分配最后
    out = distribute_cents(51, [("B-1", 1), (None, 1)], 2)
    assert out == {"B-1": 26, None: 25}
    assert sum(out.values()) == 51


def test_distribute_cents_remainder_descending_order():
    # 10 分按 6/3/1 分钟：raw = 6.0 / 3.0 / 1.0 → 无余数
    assert distribute_cents(10, [("A", 6), ("B", 3), ("C", 1)], 10) == {
        "A": 6,
        "B": 3,
        "C": 1,
    }
    # 10 分按 5/3/2 分钟：raw = 5.0 / 3.0 / 2.0 → 无余数
    assert distribute_cents(10, [("A", 5), ("B", 3), ("C", 2)], 10) == {
        "A": 5,
        "B": 3,
        "C": 2,
    }
    # 7 分按 1/1/1 分钟：raw = 2.333…，余数相同，批次号升序补 1 分
    out = distribute_cents(7, [("C", 1), ("A", 1), ("B", 1)], 3)
    assert out == {"A": 3, "B": 2, "C": 2}
    assert sum(out.values()) == 7


def test_distribute_cents_zero_amount():
    out = distribute_cents(0, [("A", 10), (None, 5)], 15)
    assert out == {"A": 0, None: 0}


def test_distribute_cents_single_destination_gets_all():
    out = distribute_cents(12345, [(None, 60)], 60)
    assert out == {None: 12345}


# ---------------------------------------------------------------- 片段分摊


def test_allocate_piece_intersection_and_unallocated():
    # 片段 0-90（90 分钟，90 分）；批次 B-1 0-30、B-2 60-90；30-60 未分配
    p = piece(1, 0, 90, amount_cents=90)
    alloc = allocate_piece(p, [batch("B-1", 0, 30), batch("B-2", 60, 90)])
    shares = {s.batch_no: s for s in alloc.shares}
    assert [s.batch_no for s in alloc.shares] == ["B-1", "B-2", None]
    assert shares["B-1"].minutes == 30
    assert shares["B-2"].minutes == 30
    assert shares[None].minutes == 30
    assert sum(s.minutes for s in alloc.shares) == 90
    assert sum(s.amount_cents for s in alloc.shares) == 90  # 金额守恒


def test_allocate_piece_amount_conservation_with_rounding():
    # 3 分钟 100 分：三个去向各 1 分钟 → 34/33/33（同余数批次号升序、未分配最后）
    p = piece(1, 0, 3, amount_cents=100)
    alloc = allocate_piece(p, [batch("B-2", 1, 2), batch("B-1", 0, 1)])
    shares = {s.batch_no: s for s in alloc.shares}
    assert shares["B-1"].amount_cents == 34
    assert shares["B-2"].amount_cents == 33
    assert shares[None].amount_cents == 33
    assert sum(s.amount_cents for s in alloc.shares) == 100
    assert sum(s.minutes for s in alloc.shares) == 3


def test_allocate_piece_outside_all_windows_fully_unallocated():
    p = piece(1, 0, 60, amount_cents=60)
    alloc = allocate_piece(p, [batch("B-1", 120, 180)])
    assert len(alloc.shares) == 1
    assert alloc.shares[0].batch_no is None
    assert alloc.shares[0].minutes == 60
    assert alloc.shares[0].amount_cents == 60


def test_allocate_piece_unpriced_only_minutes_no_amount():
    p = piece(1, 0, 60, amount_cents=None)  # 待定价片段
    alloc = allocate_piece(p, [batch("B-1", 0, 30)])
    shares = {s.batch_no: s for s in alloc.shares}
    assert shares["B-1"].minutes == 30
    assert shares["B-1"].amount_cents is None  # 只分分钟，不产生金额
    assert shares[None].minutes == 30
    assert shares[None].amount_cents is None


def test_allocate_piece_touching_window_edges_no_overlap_no_gap():
    # 窗口端点相接：0-30 与 30-60 覆盖整个片段，无未分配
    p = piece(1, 0, 60, amount_cents=60)
    alloc = allocate_piece(p, [batch("B-1", 0, 30), batch("B-2", 30, 60)])
    assert [s.batch_no for s in alloc.shares] == ["B-1", "B-2"]
    assert sum(s.minutes for s in alloc.shares) == 60


def test_allocate_piece_result_order_batch_asc_unallocated_last():
    p = piece(1, 0, 90, amount_cents=90)
    alloc = allocate_piece(
        p, [batch("B-9", 30, 60), batch("B-1", 0, 30)]  # 提交乱序
    )
    assert [s.batch_no for s in alloc.shares] == ["B-1", "B-9", None]


# ---------------------------------------------------------------- 整账分摊与汇总


def test_allocate_accounting_aggregates_by_destination_and_reason():
    pieces = [
        piece(1, 0, 60, "failure", amount_cents=60),  # 定价故障 60 分钟 60 分
        piece(2, 60, 90, "maintenance", amount_cents=None),  # 待定价保养 30 分钟
        piece(3, 90, 150, "failure", amount_cents=120),  # 定价故障 60 分钟 120 分
    ]
    batches = [batch("B-1", 0, 100), batch("B-2", 100, 200)]
    result = allocate_accounting(pieces, batches)

    # 片段顺序与守恒
    assert [pa.piece_no for pa in result.pieces] == [1, 2, 3]
    for pa, p in zip(result.pieces, pieces):
        assert sum(s.minutes for s in pa.shares) == p.minutes
        if p.priced:
            assert sum(s.amount_cents for s in pa.shares) == p.amount_cents

    dest = result.destinations
    # B-1：故障 0-60（60 分）+ 90-100（10 分钟，120×10/60=20 分）；保养待定价 60-90 中 60-100 → 30 分钟
    assert dest["B-1"]["failure"].priced_minutes == 70
    assert dest["B-1"]["failure"].amount_cents == 80
    assert dest["B-1"]["maintenance"].unpriced_minutes == 30
    assert dest["B-1"]["maintenance"].amount_cents == 0
    # B-2：故障 100-150（50 分钟，120×50/60=100 分）
    assert dest["B-2"]["failure"].priced_minutes == 50
    assert dest["B-2"]["failure"].amount_cents == 100
    assert "maintenance" not in dest["B-2"]
    # 无未分配（窗口覆盖全部片段）
    assert None not in dest


def test_allocate_accounting_unallocated_destination():
    pieces = [piece(1, 0, 60, "failure", amount_cents=60)]
    result = allocate_accounting(pieces, [batch("B-1", 0, 30)])
    dest = result.destinations
    assert dest["B-1"]["failure"].priced_minutes == 30
    assert dest["B-1"]["failure"].amount_cents == 30
    assert dest[None]["failure"].priced_minutes == 30
    assert dest[None]["failure"].amount_cents == 30


def test_allocate_accounting_order_independent():
    pieces = [
        piece(1, 0, 3, "failure", amount_cents=100),
        piece(2, 3, 6, "pending", amount_cents=None),
    ]
    b1 = [batch("B-2", 1, 2), batch("B-1", 0, 1)]
    b2 = [batch("B-1", 0, 1), batch("B-2", 1, 2)]
    r1 = allocate_accounting(pieces, b1)
    r2 = allocate_accounting(list(reversed(pieces)), b2)
    assert r1 == r2  # 列表顺序不影响结果


def test_allocate_accounting_rejects_overlapping_windows():
    try:
        allocate_accounting(
            [piece(1, 0, 60)], [batch("B-1", 0, 40), batch("B-2", 30, 60)]
        )
    except BatchWindowError:
        pass
    else:
        raise AssertionError("expected BatchWindowError")


if __name__ == "__main__":
    tests = [
        (name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_")
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
