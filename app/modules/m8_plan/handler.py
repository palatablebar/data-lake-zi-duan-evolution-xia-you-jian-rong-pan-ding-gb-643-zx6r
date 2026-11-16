"""M8 入口：修复排程页、排程接口、切换与回滚。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import store, web
from app.modules.m8_plan import service

router = APIRouter()


@router.get("/plan", response_class=HTMLResponse)
def plan_page(request: Request):
    run = store.latest_run()
    payload = service.current_plan(run)
    return web.page_or_404(
        request,
        "plan.html",
        "plan",
        run,
        plan=payload,
        summary=payload.get("summary") or {},
        items=payload.get("items") or [],
        journal=payload.get("journal") or [],
    )


@router.get("/api/plan")
def plan_api():
    run = store.latest_run()
    if not run:
        return {"run_id": None, "summary": {}, "items": [], "journal": []}
    payload = service.current_plan(run)
    return {"run_id": run["run_id"], **payload}


@router.post("/api/plan/switch")
def plan_switch(payload: dict | None = None):
    body = payload or {}
    item_id = str(body.get("item_id") or "").strip()
    if not item_id:
        from app import errors

        raise errors.BadInput("需要给出 item_id", detail={"field": "item_id"})
    result = service.switch_item(item_id, body.get("base_version_seq"))
    view = result.pop("view")
    return {"run_id": service.run_id(), **result,
            "summary": view["summary"], "items": view["items"]}


@router.post("/api/plan/rollback")
def plan_rollback(payload: dict | None = None):
    body = payload or {}
    item_id = str(body.get("item_id") or "").strip()
    if not item_id:
        from app import errors

        raise errors.BadInput("需要给出 item_id", detail={"field": "item_id"})
    result = service.rollback_item(item_id)
    view = result.pop("view")
    return {"run_id": service.run_id(), **result,
            "summary": view["summary"], "items": view["items"]}
