"""生产批次成本分摊（POST/GET /devices/{id}/loss-allocations）端到端测试。

覆盖：按窗口交集分配分钟与最大余数法分账（各去向金额之和=原片段金额）、
待定价只分分钟、窗口外归未分配、同单号同内容（列表顺序无关）幂等、
异内容冲突、未知/跨设备账、同班次多账、批次窗口重叠、非法窗口整单拒绝
不留部分结果、结果固化（后续归因修订/费率发布不改历史分摊）。

运行方式：docker compose exec app pytest tests/test_loss_allocation.py
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import engine
from app.main import app

TABLES = (
    "loss_allocation_pieces",
    "loss_allocation_lines",
    "loss_allocation_accountings",
    "loss_allocation_batches",
    "loss_allocations",
    "loss_accounting_pieces",
    "loss_accountings",
    "rate_entries",
    "rate_versions",
    "manual_adjudications",
    "shift_result_segments",
    "shift_results",
    "event_versions",
    "event_sources",
    "shifts",
    "devices",
    "change_counter",
)


@pytest.fixture()
def client():
    with TestClient(app) as c:  # 触发 lifespan：建库建表与迁移
        with engine.begin() as conn:
            for t in TABLES:
                conn.execute(text(f"TRUNCATE {t} CASCADE"))
        yield c


# ---------------------------------------------------------------- 构造数据


def _device(client, device="CNC-01"):
    r = client.post("/devices", json={"id": device, "name": f"设备{device}"})
    assert r.status_code in (200, 201), r.text


def _shift(client, device, start, end):
    r = client.post(
        "/shifts", json={"device_id": device, "start": start, "end": end}
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _event(client, device, source, version, start, end, reason):
    r = client.post(
        "/events",
        json={
            "device_id": device,
            "source_id": source,
            "version": version,
            "start": start,
            "end": end,
            "reason": reason,
        },
    )
    assert r.status_code == 201, r.text


def _settle(client, shift_id):
    r = client.post(f"/shifts/{shift_id}/settle")
    assert r.status_code == 200, r.text


def _publish_rates(client, device, entries, note=""):
    r = client.post(
        f"/devices/{device}/rate-versions",
        json={"entries": entries, "note": note},
    )
    assert r.status_code == 201, r.text
    return r.json()["version"]


def _account(client, shift_id, rate_version, revision=None, expect=201):
    body = {"rate_version": rate_version}
    if revision is not None:
        body["revision"] = revision
    r = client.post(f"/shifts/{shift_id}/loss-accountings", json=body)
    assert r.status_code == expect, r.text
    return r.json()


def _entry(reason, sh, eh, amount, day="2026-10-01"):
    return {
        "reason": reason,
        "start": f"{day}T{sh}+08:00",
        "end": f"{day}T{eh}+08:00",
        "amount_per_minute": amount,
    }


def _base_setup(client):
    """班次1（10/1 08:00-16:00）：故障 10:00-12:00 + 保养 11:00-11:30（已结算）；
    班次2（10/1 16:00-24:00）：待判 18:00-19:00（已结算）。
    费率版本1：故障 08:00-11:00 @1 元、11:00-24:00 @0.5 元；保养全天 @2 元（无待判费率）。
    账1 = 班次1 修订1 × 费率版本1；账2 = 班次2 修订1 × 费率版本1（待判 60 分钟待定价）。
    """
    _device(client)
    s1 = _shift(client, "CNC-01", "2026-10-01T08:00:00+08:00", "2026-10-01T16:00:00+08:00")
    _event(client, "CNC-01", "PLC-9", 1, "2026-10-01T10:00:00+08:00", "2026-10-01T12:00:00+08:00", "failure")
    _event(client, "CNC-01", "MES-1", 1, "2026-10-01T11:00:00+08:00", "2026-10-01T11:30:00+08:00", "maintenance")
    _settle(client, s1)
    s2 = _shift(client, "CNC-01", "2026-10-01T16:00:00+08:00", "2026-10-02T00:00:00+08:00")
    _event(client, "CNC-01", "PLC-9", 2, "2026-10-01T18:00:00+08:00", "2026-10-01T19:00:00+08:00", "pending")
    _settle(client, s2)
    rv1 = _publish_rates(
        client,
        "CNC-01",
        [
            _entry("failure", "08:00:00", "11:00:00", "1"),
            _entry("failure", "11:00:00", "23:59:00", "0.5"),
            _entry("maintenance", "08:00:00", "23:59:00", "2"),
        ],
    )
    acc1 = _account(client, s1, rv1)  # 定价 120 分钟，合计 135.00
    acc2 = _account(client, s2, rv1)  # 待判 60 分钟全部待定价
    return s1, s2, rv1, acc1, acc2


def _allocate(client, body, device="CNC-01", expect=None):
    r = client.post(f"/devices/{device}/loss-allocations", json=body)
    if expect is not None:
        assert r.status_code == expect, r.text
    return r


# ---------------------------------------------------------------- 正常分摊与守恒


def test_allocate_happy_path_and_conservation(client):
    _s1, _s2, _rv1, acc1, acc2 = _base_setup(client)
    body = {
        "allocation_no": "ALLOC-001",
        "accounting_ids": [acc1["id"], acc2["id"]],
        "batches": [
            {"batch_no": "B-001", "start": "2026-10-01T09:30:00+08:00", "end": "2026-10-01T10:30:00+08:00"},
            {"batch_no": "B-002", "start": "2026-10-01T10:30:00+08:00", "end": "2026-10-01T11:30:00+08:00"},
            {"batch_no": "B-003", "start": "2026-10-01T17:00:00+08:00", "end": "2026-10-01T19:00:00+08:00"},
        ],
    }
    r = _allocate(client, body, expect=201)
    out = r.json()
    assert out["device_id"] == "CNC-01"
    assert out["allocation_no"] == "ALLOC-001"
    assert out["idempotent"] is False
    assert [b["batch_no"] for b in out["batches"]] == ["B-001", "B-002", "B-003"]
    assert [a["accounting_id"] for a in out["accountings"]] == [acc1["id"], acc2["id"]]

    a1 = out["accountings"][0]
    assert (a1["shift_id"], a1["revision"], a1["rate_version"]) == (
        acc1["shift_id"], 1, 1,
    )
    # 片段守恒：各去向分钟之和=片段分钟；定价片段各去向金额之和=片段金额
    for p in a1["pieces"]:
        assert sum(s["minutes"] for s in p["shares"]) == p["minutes"]
        if p["priced"]:
            assert sum(s["amount_cents"] for s in p["shares"]) == p["amount_cents"]
        else:
            assert all(s["amount_cents"] is None for s in p["shares"])
    # 去向按批次号升序、未分配最后
    dest_no = [d["batch_no"] for d in a1["destinations"]]
    assert dest_no == ["B-001", "B-002", None]
    by_no = {d["batch_no"]: d for d in a1["destinations"]}
    # B-001：故障 10:00-10:30 → 30 分钟 30.00
    d = by_no["B-001"]
    assert d["totals"]["priced_minutes"] == 30
    assert d["totals"]["amount_cents"] == 3000
    reason_map = {r["reason"]: r for r in d["by_reason"]}
    assert reason_map["failure"]["priced_minutes"] == 30
    assert reason_map["failure"]["amount_cents"] == 3000
    assert reason_map["maintenance"]["priced_minutes"] == 0  # 三类原因恒列出
    # B-002：故障 10:30-11:00（30 分钟 30.00）+ 保养 11:00-11:30（30 分钟 60.00）
    d = by_no["B-002"]
    assert d["totals"]["priced_minutes"] == 60
    assert d["totals"]["amount_cents"] == 9000
    reason_map = {r["reason"]: r for r in d["by_reason"]}
    assert reason_map["maintenance"]["amount_cents"] == 6000
    # 未分配：故障 11:30-12:00（30 分钟 15.00，窗口外）
    d = by_no[None]
    assert d["totals"]["priced_minutes"] == 30
    assert d["totals"]["amount_cents"] == 1500
    # 各去向金额之和 = 账合计
    assert sum(d["totals"]["amount_cents"] for d in a1["destinations"]) == 13500

    # 账2：待判 60 分钟全部待定价，分批次 B-003，不产生金额
    a2 = out["accountings"][1]
    assert len(a2["destinations"]) == 1
    d = a2["destinations"][0]
    assert d["batch_no"] == "B-003"
    assert d["totals"]["unpriced_minutes"] == 60
    assert d["totals"]["priced_minutes"] == 0
    assert d["totals"]["amount_cents"] == 0
    reason_map = {r["reason"]: r for r in d["by_reason"]}
    assert reason_map["pending"]["unpriced_minutes"] == 60
    p = a2["pieces"][0]
    assert p["priced"] is False
    assert p["shares"] == [{"batch_no": "B-003", "minutes": 60, "amount_cents": None}]


def test_allocate_largest_remainder_end_to_end(client):
    """100 分按 1/1/1 分钟分三份：34/33/33（同余数批次号升序、未分配最后）。"""
    _device(client)
    s = _shift(client, "CNC-01", "2026-10-02T08:00:00+08:00", "2026-10-02T09:00:00+08:00")
    _event(client, "CNC-01", "PLC-9", 1, "2026-10-02T08:00:00+08:00", "2026-10-02T08:03:00+08:00", "failure")
    _settle(client, s)
    rv = _publish_rates(
        client,
        "CNC-01",
        [_entry("failure", "00:00:00", "23:59:00", "0.333333", day="2026-10-02")],
    )
    acc = _account(client, s, rv)
    # 3 分钟 × 0.333333 元 = 0.999999 元 → 逐段取整为 100 分
    assert acc["totals"]["amount_cents"] == 100

    r = _allocate(
        client,
        {
            "allocation_no": "ALLOC-REM",
            "accounting_ids": [acc["id"]],
            "batches": [
                {"batch_no": "B-200", "start": "2026-10-02T08:01:00+08:00", "end": "2026-10-02T08:02:00+08:00"},
                {"batch_no": "B-100", "start": "2026-10-02T08:00:00+08:00", "end": "2026-10-02T08:01:00+08:00"},
            ],
        },
        expect=201,
    )
    piece = r.json()["accountings"][0]["pieces"][0]
    shares = {s["batch_no"]: s for s in piece["shares"]}
    assert shares["B-100"]["amount_cents"] == 34  # 同余数按批次号升序先补
    assert shares["B-200"]["amount_cents"] == 33
    assert shares[None]["amount_cents"] == 33  # 未分配最后
    assert sum(s["amount_cents"] for s in piece["shares"]) == 100


# ---------------------------------------------------------------- 幂等与冲突


def test_idempotent_retry_order_independent(client):
    _s1, _s2, _rv1, acc1, acc2 = _base_setup(client)
    body = {
        "allocation_no": "ALLOC-001",
        "accounting_ids": [acc1["id"], acc2["id"]],
        "batches": [
            {"batch_no": "B-001", "start": "2026-10-01T09:30:00+08:00", "end": "2026-10-01T10:30:00+08:00"},
            {"batch_no": "B-002", "start": "2026-10-01T10:30:00+08:00", "end": "2026-10-01T11:30:00+08:00"},
        ],
    }
    first = _allocate(client, body, expect=201).json()
    # 列表顺序不影响同内容判定：账ID与批次均乱序重试 → 幂等返回原结果
    retry = dict(body)
    retry["accounting_ids"] = [acc2["id"], acc1["id"]]
    retry["batches"] = list(reversed(body["batches"]))
    r = _allocate(client, retry, expect=200)
    assert r.json()["idempotent"] is True
    assert r.json() == {**first, "idempotent": True}  # 原结果逐字节一致


def test_conflict_same_no_different_content(client):
    _s1, _s2, _rv1, acc1, acc2 = _base_setup(client)
    body = {
        "allocation_no": "ALLOC-001",
        "accounting_ids": [acc1["id"]],
        "batches": [
            {"batch_no": "B-001", "start": "2026-10-01T09:30:00+08:00", "end": "2026-10-01T10:30:00+08:00"}
        ],
    }
    _allocate(client, body, expect=201)
    changed = dict(body)
    changed["accounting_ids"] = [acc2["id"]]  # 同单号异内容
    r = _allocate(client, changed, expect=409)
    assert r.json()["detail"]["code"] == "ALLOCATION_CONFLICT"
    changed2 = dict(body)
    changed2["batches"] = [
        {"batch_no": "B-001", "start": "2026-10-01T09:30:00+08:00", "end": "2026-10-01T10:31:00+08:00"}
    ]
    r = _allocate(client, changed2, expect=409)
    assert r.json()["detail"]["code"] == "ALLOCATION_CONFLICT"


# ---------------------------------------------------------------- 整单拒绝（不留部分结果）


def test_reject_unknown_accounting_and_leave_nothing(client):
    _s1, _s2, _rv1, acc1, _acc2 = _base_setup(client)
    body = {
        "allocation_no": "ALLOC-BAD",
        "accounting_ids": [acc1["id"], 999999],
        "batches": [
            {"batch_no": "B-001", "start": "2026-10-01T09:30:00+08:00", "end": "2026-10-01T10:30:00+08:00"}
        ],
    }
    r = _allocate(client, body, expect=404)
    assert r.json()["detail"]["code"] == "ACCOUNTING_NOT_FOUND"
    # 不留部分结果：单号未固化，之后可用同单号正常提交
    r = client.get("/devices/CNC-01/loss-allocations/ALLOC-BAD")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "ALLOCATION_NOT_FOUND"
    body["accounting_ids"] = [acc1["id"]]
    _allocate(client, body, expect=201)


def test_reject_cross_device_accounting(client):
    _s1, _s2, _rv1, acc1, _acc2 = _base_setup(client)
    # 另一台设备的账
    _device(client, "CNC-02")
    s = _shift(client, "CNC-02", "2026-10-01T08:00:00+08:00", "2026-10-01T16:00:00+08:00")
    _event(client, "CNC-02", "PLC-1", 1, "2026-10-01T10:00:00+08:00", "2026-10-01T11:00:00+08:00", "failure")
    _settle(client, s)
    rv = _publish_rates(client, "CNC-02", [_entry("failure", "08:00:00", "16:00:00", "1")])
    other_acc = _account(client, s, rv)

    r = _allocate(
        client,
        {
            "allocation_no": "ALLOC-X",
            "accounting_ids": [acc1["id"], other_acc["id"]],
            "batches": [
                {"batch_no": "B-001", "start": "2026-10-01T09:30:00+08:00", "end": "2026-10-01T10:30:00+08:00"}
            ],
        },
        expect=422,
    )
    assert r.json()["detail"]["code"] == "ACCOUNTING_DEVICE_MISMATCH"
    assert client.get("/devices/CNC-01/loss-allocations/ALLOC-X").status_code == 404


def test_reject_duplicate_shift_accounting(client):
    _s1, _s2, rv1, acc1, _acc2 = _base_setup(client)
    # 同班次第二份账（修订1 × 新费率版本2）
    rv2 = _publish_rates(client, "CNC-01", [_entry("failure", "08:00:00", "23:59:00", "3")])
    acc1b = _account(client, _s1, rv2)
    r = _allocate(
        client,
        {
            "allocation_no": "ALLOC-DUP",
            "accounting_ids": [acc1["id"], acc1b["id"]],
            "batches": [
                {"batch_no": "B-001", "start": "2026-10-01T09:30:00+08:00", "end": "2026-10-01T10:30:00+08:00"}
            ],
        },
        expect=422,
    )
    assert r.json()["detail"]["code"] == "DUPLICATE_SHIFT_ACCOUNTING"
    assert client.get("/devices/CNC-01/loss-allocations/ALLOC-DUP").status_code == 404


def test_reject_overlapping_batch_windows(client):
    _s1, _s2, _rv1, acc1, _acc2 = _base_setup(client)
    r = _allocate(
        client,
        {
            "allocation_no": "ALLOC-OVL",
            "accounting_ids": [acc1["id"]],
            "batches": [
                {"batch_no": "B-001", "start": "2026-10-01T09:30:00+08:00", "end": "2026-10-01T10:30:00+08:00"},
                {"batch_no": "B-002", "start": "2026-10-01T10:00:00+08:00", "end": "2026-10-01T11:00:00+08:00"},
            ],
        },
        expect=422,
    )
    assert r.json()["detail"]["code"] == "BATCH_WINDOW_OVERLAP"
    assert client.get("/devices/CNC-01/loss-allocations/ALLOC-OVL").status_code == 404


def test_reject_invalid_requests(client):
    _s1, _s2, _rv1, acc1, _acc2 = _base_setup(client)
    base = {
        "allocation_no": "ALLOC-V",
        "accounting_ids": [acc1["id"]],
        "batches": [
            {"batch_no": "B-001", "start": "2026-10-01T09:30:00+08:00", "end": "2026-10-01T10:30:00+08:00"}
        ],
    }
    # 缺时区 / 未对齐整分钟 / start≥end → 422 VALIDATION_ERROR
    bad_windows = [
        [{"batch_no": "B-001", "start": "2026-10-01T09:30:00", "end": "2026-10-01T10:30:00+08:00"}],
        [{"batch_no": "B-001", "start": "2026-10-01T09:30:30+08:00", "end": "2026-10-01T10:30:00+08:00"}],
        [{"batch_no": "B-001", "start": "2026-10-01T10:30:00+08:00", "end": "2026-10-01T09:30:00+08:00"}],
    ]
    for w in bad_windows:
        r = _allocate(client, {**base, "batches": w}, expect=422)
        assert r.json()["detail"]["code"] == "VALIDATION_ERROR"
    # 重复批次号 / 重复账ID / 空列表 → 422
    r = _allocate(
        client,
        {**base, "batches": base["batches"] + [dict(base["batches"][0])]},
        expect=422,
    )
    assert r.json()["detail"]["code"] == "VALIDATION_ERROR"
    r = _allocate(
        client,
        {**base, "accounting_ids": [acc1["id"], acc1["id"]]},
        expect=422,
    )
    assert r.json()["detail"]["code"] == "VALIDATION_ERROR"
    _allocate(client, {**base, "accounting_ids": []}, expect=422)
    _allocate(client, {**base, "batches": []}, expect=422)
    # 均未留下任何分摊单
    assert client.get("/devices/CNC-01/loss-allocations/ALLOC-V").status_code == 404


def test_unknown_device(client):
    r = _allocate(
        client,
        {
            "allocation_no": "ALLOC-1",
            "accounting_ids": [1],
            "batches": [
                {"batch_no": "B-001", "start": "2026-10-01T09:30:00+08:00", "end": "2026-10-01T10:30:00+08:00"}
            ],
        },
        device="NOPE",
        expect=404,
    )
    assert r.json()["detail"]["code"] == "DEVICE_NOT_FOUND"
    r = client.get("/devices/NOPE/loss-allocations/ALLOC-1")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "DEVICE_NOT_FOUND"


# ---------------------------------------------------------------- 读取与固化


def test_get_allocation_readback(client):
    _s1, _s2, _rv1, acc1, _acc2 = _base_setup(client)
    body = {
        "allocation_no": "ALLOC-001",
        "accounting_ids": [acc1["id"]],
        "batches": [
            {"batch_no": "B-001", "start": "2026-10-01T09:30:00+08:00", "end": "2026-10-01T10:30:00+08:00"}
        ],
    }
    created = _allocate(client, body, expect=201).json()
    r = client.get("/devices/CNC-01/loss-allocations/ALLOC-001")
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["idempotent"] is True
    assert got == {**created, "idempotent": True}
    r = client.get("/devices/CNC-01/loss-allocations/NOPE")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "ALLOCATION_NOT_FOUND"


def test_frozen_unaffected_by_later_revisions_and_rates(client):
    """后续归因修订、费率发布与新账不改历史分摊；同内容重试仍返回原结果。"""
    s1, _s2, rv1, acc1, _acc2 = _base_setup(client)
    body = {
        "allocation_no": "ALLOC-FROZEN",
        "accounting_ids": [acc1["id"]],
        "batches": [
            {"batch_no": "B-001", "start": "2026-10-01T09:30:00+08:00", "end": "2026-10-01T10:30:00+08:00"}
        ],
    }
    created = _allocate(client, body, expect=201).json()

    # 归因修订：故障缩短为 10:00-11:30 → 班次1 产生修订2
    _event(client, "CNC-01", "PLC-9", 3, "2026-10-01T10:00:00+08:00", "2026-10-01T11:30:00+08:00", "failure")
    # 新费率版本与新组合的账
    rv2 = _publish_rates(client, "CNC-01", [_entry("failure", "08:00:00", "23:59:00", "9")])
    _account(client, s1, rv2)  # 修订2 × 费率版本2 → 新账

    r = client.get("/devices/CNC-01/loss-allocations/ALLOC-FROZEN")
    assert r.status_code == 200
    assert r.json() == {**created, "idempotent": True}  # 历史分摊不变
    retry = _allocate(client, body, expect=200)
    assert retry.json() == {**created, "idempotent": True}  # 同内容重试仍返回原结果
