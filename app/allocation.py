"""生产批次停机成本分摊：把已固化损失账的切分明细按批次生产窗口分配（纯标准库）。

规则
- 输入为一份损失账的切分明细（定价段金额已按分取整；待定价段只有分钟、
  无金额）与一批「批次号 + 带时区整分钟半开生产窗口」（一批次一窗口，
  窗口互不重叠，端点相接允许）；
- 每个切分片段按与各批次窗口的交集分钟分配：窗口之外的分钟归「未分配」；
- 定价片段的整数分按交集分钟占该片段分钟的比例分配（最大余数法）：
  先向下取整，余分按小数余数降序逐个补足，同余数按批次号升序、
  未分配最后；各去向金额之和恒等于原片段金额；
- 待定价片段只分分钟，不产生金额；
- 全程整数运算，结果确定：与批次/片段的提交顺序无关。

本模块只依赖标准库，可脱离 Web/数据库独立测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


class BatchWindowError(ValueError):
    """批次窗口非法：批次号重复（一批次一窗口）或窗口重叠。"""


@dataclass(frozen=True)
class BatchWindow:
    """一个生产批次的整分钟半开生产窗口 [start, end)（一批次一窗口）。"""

    batch_no: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class PieceView:
    """损失账的一个切分片段（定价或待定价）。

    定价片段（priced=True）携带每分钟费率（微元）与已按分取整的片段金额
    （分）；待定价片段（priced=False）只有分钟，两个金额字段均为 None。
    """

    piece_no: int
    start: datetime
    end: datetime
    reason: str
    minutes: int
    priced: bool
    rate_per_minute_micro: int | None
    amount_cents: int | None


@dataclass(frozen=True)
class Share:
    """一个片段分到一个去向（批次号，或 None=未分配）的分钟与金额。"""

    batch_no: str | None
    minutes: int
    amount_cents: int | None  # 待定价片段为 None（只分分钟）


@dataclass(frozen=True)
class PieceAllocation:
    """一个切分片段的完整分摊：各去向分钟之和 = 片段分钟；

    定价片段各去向金额之和 = 片段金额。
    """

    piece_no: int
    shares: tuple[Share, ...]


@dataclass(frozen=True)
class ReasonShare:
    """某去向下某原因的汇总：定价分钟 / 待定价分钟 / 金额（分）。"""

    reason: str
    priced_minutes: int = 0
    unpriced_minutes: int = 0
    amount_cents: int = 0


@dataclass(frozen=True)
class AccountingAllocation:
    """一份账的完整分摊结果。

    - pieces：逐片段的去向明细（来源明细）；
    - destinations：去向（None=未分配）→ 原因 → 汇总，只含有分钟的去向与原因。
    """

    pieces: tuple[PieceAllocation, ...]
    destinations: dict[str | None, dict[str, ReasonShare]] = field(
        default_factory=dict
    )


def _minutes(start: datetime, end: datetime) -> int:
    return int((end - start).total_seconds()) // 60


def validate_batch_windows(batches: list[BatchWindow]) -> None:
    """校验批次窗口：批次号唯一（一批次一窗口）、窗口互不重叠（端点相接允许）。"""
    seen: set[str] = set()
    for b in batches:
        if b.batch_no in seen:
            raise BatchWindowError(
                f"duplicate batch_no {b.batch_no!r}: one window per batch"
            )
        seen.add(b.batch_no)
    prev: BatchWindow | None = None
    for b in sorted(batches, key=lambda x: (x.start, x.end, x.batch_no)):
        if prev is not None and b.start < prev.end:
            raise BatchWindowError(
                f"overlapping batch windows: {prev.batch_no!r} "
                f"[{prev.start.isoformat()}, {prev.end.isoformat()}) and "
                f"{b.batch_no!r} [{b.start.isoformat()}, {b.end.isoformat()})"
            )
        prev = b


def distribute_cents(
    amount_cents: int, parts: list[tuple[str | None, int]], total_minutes: int
) -> dict[str | None, int]:
    """最大余数法：把整数分按各去向分钟占总分钟的比例分配。

    parts 为 [(去向, 交集分钟)]（去向 None 表示未分配，仅含分钟 > 0 的去向），
    各去向分钟之和须等于 total_minutes。先取 floor(amount × minutes / total)，
    余分按小数余数降序逐个补足；同余数按批次号升序、未分配最后。
    返回 {去向: 金额(分)}，总和恒等于 amount_cents。
    """
    floors: dict[str | None, int] = {}
    remainders: dict[str | None, int] = {}
    for key, minutes in parts:
        raw = amount_cents * minutes
        floors[key] = raw // total_minutes
        remainders[key] = raw % total_minutes
    leftover = amount_cents - sum(floors.values())
    # 余数降序；同余数批次号升序、未分配最后（leftover < 去向数，每去向至多补 1 分）
    order = sorted(
        parts,
        key=lambda kv: (-remainders[kv[0]], kv[0] is None, kv[0] or ""),
    )
    out = dict(floors)
    for key, _m in order[:leftover]:
        out[key] += 1
    return out


def allocate_piece(
    piece: PieceView, batches: list[BatchWindow]
) -> PieceAllocation:
    """把一个切分片段按批次窗口交集分配分钟（定价片段同时分配金额）。

    去向按批次号升序排列、未分配最后；窗口外分钟归未分配。
    """
    parts: list[tuple[str | None, int]] = []
    for b in sorted(batches, key=lambda x: x.batch_no):
        lo, hi = max(piece.start, b.start), min(piece.end, b.end)
        if lo < hi:
            parts.append((b.batch_no, _minutes(lo, hi)))
    rest = piece.minutes - sum(m for _, m in parts)
    if rest < 0:  # 只有窗口重叠才会发生；validate_batch_windows 已先行排除
        raise BatchWindowError(
            "batch windows overlap: allocated minutes exceed piece minutes"
        )
    if rest > 0:
        parts.append((None, rest))  # 窗口外分钟归「未分配」
    if piece.priced:
        if piece.amount_cents is None:
            raise ValueError("priced piece must carry amount_cents")
        amounts = distribute_cents(piece.amount_cents, parts, piece.minutes)
        shares = tuple(
            Share(batch_no=key, minutes=m, amount_cents=amounts[key])
            for key, m in parts
        )
    else:
        shares = tuple(
            Share(batch_no=key, minutes=m, amount_cents=None) for key, m in parts
        )
    return PieceAllocation(piece_no=piece.piece_no, shares=shares)


def allocate_accounting(
    pieces: list[PieceView], batches: list[BatchWindow]
) -> AccountingAllocation:
    """分摊一份账的全部切分片段，并按（去向, 原因）汇总分钟与金额。

    片段按 piece_no 顺序处理；批次窗口先经 validate_batch_windows 校验。
    """
    validate_batch_windows(batches)
    by_no = {p.piece_no: p for p in pieces}
    allocations = tuple(
        allocate_piece(p, batches) for p in sorted(pieces, key=lambda x: x.piece_no)
    )
    destinations: dict[str | None, dict[str, ReasonShare]] = {}
    for alloc in allocations:
        piece = by_no[alloc.piece_no]
        for share in alloc.shares:
            by_reason = destinations.setdefault(share.batch_no, {})
            cur = by_reason.get(piece.reason, ReasonShare(reason=piece.reason))
            if piece.priced:
                cur = ReasonShare(
                    reason=piece.reason,
                    priced_minutes=cur.priced_minutes + share.minutes,
                    unpriced_minutes=cur.unpriced_minutes,
                    amount_cents=cur.amount_cents + (share.amount_cents or 0),
                )
            else:
                cur = ReasonShare(
                    reason=piece.reason,
                    priced_minutes=cur.priced_minutes,
                    unpriced_minutes=cur.unpriced_minutes + share.minutes,
                    amount_cents=cur.amount_cents,
                )
            by_reason[piece.reason] = cur
    return AccountingAllocation(pieces=allocations, destinations=destinations)
