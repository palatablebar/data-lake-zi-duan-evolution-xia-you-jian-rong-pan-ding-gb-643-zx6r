"""M8 修复排程：排序、依赖闸门、切换中断恢复、同批重试幂等、出口一致。"""

from __future__ import annotations

import csv
import io

import pytest

from app import errors, store
from app.modules.m6_batches import service as batch_service
from app.modules.m8_plan import lifecycle, model, service


def _item(view: dict, column: str) -> dict:
    return next(item for item in view["items"] if item["column"] == column)


def test_plan_orders_critical_chain_first(run):
    """关键结算链先于公共维度字段，且同档内影响任务多的先排。"""
    view = service.current_plan(run)
    positions = {item["column"]: item["position"] for item in view["items"]}
    assert positions["pay_amt"] == 1
    assert positions["channel"] == 2
    assert positions["paid_at"] == 3
    # 公共维度的破坏项全部排在关键链之后
    first_dim = min(item["position"] for item in view["items"]
                    if item["table"] == "dim_order")
    assert first_dim > positions["paid_at"]
    tiers = [item["tier"] for item in view["items"]]
    assert tiers == sorted(tiers)


def test_dependency_blocks_switchable_state(run):
    """依赖未解除的字段不能进入可切换状态，接口必须拒绝切换。"""
    view = service.current_plan(run)
    for column in ("channel", "status", "ext_json"):
        item = _item(view, column)
        assert item["effective_switchable"] is False
        assert item["condition"]
    assert _item(view, "pay_amt")["effective_switchable"] is True

    with pytest.raises(errors.DependencyUnreleased) as caught:
        service.switch_item(_item(view, "channel")["item_id"])
    assert caught.value.status == 409
    assert caught.value.detail["reason"] == "DEP_UNRELEASED"


def test_dependency_release_unlocks_dependent(run):
    """前置项切换后，被依赖项才可切换；回滚前置项后被依赖项重新挡住。"""
    view = service.current_plan(run)
    pay = _item(view, "pay_amt")
    channel = _item(view, "channel")
    assert channel["deps"][0]["item_id"] == pay["item_id"]

    service.switch_item(pay["item_id"])
    view = service.current_plan(run)
    assert _item(view, "channel")["deps_released"] is True
    assert _item(view, "channel")["effective_switchable"] is True

    service.switch_item(_item(view, "channel")["item_id"])
    # 有已切换的依赖项时，不能先回滚前置项
    with pytest.raises(errors.Conflict) as caught:
        service.rollback_item(pay["item_id"])
    assert caught.value.detail["reason"] == "DEPENDENT_SWITCHED"

    service.rollback_item(_item(view, "channel")["item_id"])
    rolled = service.rollback_item(pay["item_id"])
    assert rolled["status"] == "rolled_back"
    view = service.current_plan(run)
    assert _item(view, "channel")["deps_released"] is False
    assert _item(view, "channel")["effective_switchable"] is False


def test_interrupted_switch_recovers_old_version(run):
    """切换在 prepare 与 commit 之间中断：先回滚恢复旧版，再重试。"""
    view = service.current_plan(run)
    target = _item(view, "paid_at")
    lifecycle.begin_switch(view, run["batch_key"], target["item_id"])

    view = service.current_plan(run)
    interrupted = _item(view, "paid_at")
    assert interrupted["interrupted"] is True
    assert interrupted["effective_switchable"] is False

    with pytest.raises(errors.SwitchInterrupted) as caught:
        service.switch_item(target["item_id"])
    assert caught.value.status == 409

    rolled = service.rollback_item(target["item_id"])
    assert rolled["interrupted"] is True
    assert rolled["restored"] == "available"
    view = service.current_plan(run)
    assert _item(view, "paid_at")["switch_status"] == "rolled_back"
    assert _item(view, "paid_at")["effective_switchable"] is True
    assert service.switch_item(target["item_id"])["status"] == "switched"


def test_retry_same_batch_does_not_add_orders(client):
    """同一批重试（重复读取、重跑批次）不新增工单。"""
    run = store.latest_run()
    first = store.count_plan_orders(run["batch_key"])
    assert first == 6
    for _ in range(3):
        service.current_plan(run)
    batch_service.execute()
    again = batch_service.execute()
    assert again["run_id"] == run["run_id"]
    assert store.count_runs() == 1
    assert store.count_plan_orders(run["batch_key"]) == first


def test_plan_is_deterministic_and_order_independent():
    """同样的判定结果重排输入，工单顺序不变；顺序只看关键链路与影响面。"""
    run = {
        "diff": [
            {"field_id": "1", "table": "dim_x", "old_name": "a", "new_name": "a",
             "bucket": "retyped", "change": "narrow", "type_detail": "int_digits_drop",
             "primary": "TYPE_NARROW", "reasons": ["TYPE_NARROW"], "ref_count": 0,
             "verdict": "BREAKING", "old_type": "decimal(18,2)",
             "new_type": "decimal(18,4)"},
            {"field_id": "2", "table": "fct_pay", "old_name": "b", "new_name": "b",
             "bucket": "required", "change": "same", "type_detail": None,
             "primary": "REQUIRED_TIGHTENED", "reasons": ["REQUIRED_TIGHTENED"],
             "ref_count": 0, "verdict": "BREAKING", "old_type": "timestamp",
             "new_type": "timestamp"},
        ],
        "refs": {"consumers": []},
        "resolution": {},
    }
    forward = model.build(run)
    backward = model.build({**run, "diff": list(reversed(run["diff"]))})
    assert [item["field_id"] for item in forward["items"]] == \
           [item["field_id"] for item in backward["items"]]
    assert forward["items"][0]["field_id"] == "2"
    assert forward["items"][0]["tier"] == 0


def test_blocked_items_do_not_take_window_slots(run):
    """阻塞项不占窗口：可切换数不超过容量，阻塞项不计入。"""
    view = service.current_plan(run)
    summary = view["summary"]
    assert summary["capacity"] == 3
    assert summary["pending"] <= summary["capacity"]
    assert summary["blocked"] == 3
    assert all(item["slot"] is None for item in view["items"] if item["wave"] == "阻塞")


def test_dependency_cycle_blocks_instead_of_recursing():
    """依赖成环时不能爆栈，也不能放行切换：按阻塞处理并要求先拆环。"""
    def item(item_id: str, deps: list[str]) -> dict:
        return {
            "item_id": item_id, "field_id": item_id, "column": item_id,
            "fix": {"ready": True, "suggestion": "改类型", "note": ""},
            "deps": [{"item_id": dep, "column": dep} for dep in deps],
            "wave": "阻塞", "slot": None, "dep_cycle": False,
        }

    items = [item("a", ["b"]), item("b", ["a"])]
    model._plan_static(items, 3)
    assert all(row["dep_blocked"] for row in items)
    assert all(row["wave"] == "阻塞" for row in items)
    condition = model.condition_text(items[0], {row["item_id"]: row for row in items},
                                     set(), 3, 0)
    assert "依赖成环" in condition


def test_page_and_exports_share_one_conclusion(client):
    """/plan 页面、/api/plan、排程 CSV、迁移单对同一工单给出同一结论。"""
    api = client.get("/api/plan").json()
    rows = list(csv.DictReader(io.StringIO(client.get("/api/export/plan.csv").text)))
    spec = client.get("/api/export/spec.md").text

    by_id = {row["field_id"]: row for row in rows}
    for item in api["items"]:
        row = by_id[item["field_id"]]
        assert row["position"] == str(item["position"])
        assert row["switchable"] == ("Y" if item["effective_switchable"] else "N")
        assert row["condition"] == item["condition"]
        assert row["state_code"] == item["state_code"]
        assert item["state_code"] in spec
        assert item["process_state"] in spec
    assert len(rows) == len(api["items"])


def test_plan_page_shows_same_state_as_api(client):
    from app import reasons

    api = client.get("/api/plan").json()
    page = client.get("/plan").text
    for item in api["items"]:
        assert item["condition"] in page
        assert reasons.label(item["state_code"]) in page


def test_manifest_carries_plan(client):
    import json

    payload = json.loads(client.get("/api/export/manifest.json").text)
    assert payload["plan"]["summary"]["total"] == 6
    for item in payload["plan"]["items"]:
        assert item["state_code"]
        assert item["condition"]


def test_switch_rejects_stale_baseline(client):
    run = store.latest_run()
    view = service.current_plan(run)
    target = _item(view, "pay_amt")
    with pytest.raises(errors.StaleVersion):
        service.switch_item(target["item_id"], base_version_seq=0)


def test_reason_codes_registered():
    from app import reasons

    for code in ("PLAN_READY", "PLAN_WAITING", "PLAN_BLOCKED", "PLAN_SWITCHED",
                 "PLAN_ROLLED_BACK", "DEP_UNRELEASED", "SWITCH_INTERRUPTED",
                 "WINDOW_FULL", "NOTHING_TO_ROLLBACK", "DEPENDENT_SWITCHED"):
        assert code in reasons.known_codes()
    assert reasons.severity("DEP_UNRELEASED") == "break"


def test_every_plan_state_maps_to_a_registered_code(run):
    """工单状态码必须都能落到 reasons.py，页面不出现裸字符串。"""
    from app import reasons

    view = service.current_plan(run)
    for item in view["items"]:
        assert item["state_code"] in reasons.known_codes()
