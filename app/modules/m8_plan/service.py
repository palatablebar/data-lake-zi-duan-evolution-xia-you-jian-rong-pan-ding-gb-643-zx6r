"""修复排程编排：批次结果 → 工单 → 切换状态，页面与导出共用同一份。"""

from __future__ import annotations

from app import errors, store
from app.modules.m8_plan import lifecycle, model


def plan_of(run: dict) -> dict:
    """纯计算，不落库；同一输入必得同一结果。"""
    return model.build(run)


def current_plan(run: dict | None = None) -> dict:
    """带切换状态的排程视图。工单在第一次读取时补齐，重试不新增行。"""
    run = run if run is not None else store.latest_run()
    if not run:
        return {"summary": {}, "policy": {}, "items": [], "journal": []}
    plan = plan_of(run)
    lifecycle.ensure_orders(run["batch_key"], plan["items"])
    return lifecycle.annotate(plan, run["batch_key"])


def switch_item(item_id: str, base_version_seq=None) -> dict:
    run = _require_run()
    if base_version_seq is not None:
        from app.modules.m6_batches import guard
        guard.check_stale(base_version_seq, store.current_version_seq())
    view = current_plan(run)
    result = lifecycle.switch(view, run["batch_key"], item_id)
    result["view"] = lifecycle.annotate(plan_of(run), run["batch_key"])
    return result


def rollback_item(item_id: str) -> dict:
    run = _require_run()
    view = current_plan(run)
    result = lifecycle.rollback(view, run["batch_key"], item_id)
    result["view"] = lifecycle.annotate(plan_of(run), run["batch_key"])
    return result


def _require_run() -> dict:
    run = store.latest_run()
    if not run:
        raise errors.NotFound("还没有批次，先在版本历史页跑一次")
    return run


def run_id() -> str | None:
    run = store.latest_run()
    return run["run_id"] if run else None
