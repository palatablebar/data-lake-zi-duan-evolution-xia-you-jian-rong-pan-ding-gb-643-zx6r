"""切换状态机：两阶段切换 + 中断恢复 + 幂等工单。

一次切换先写日志（prepare），再落状态（commit）。日志先落盘，进程在两步之间
被杀掉也能从日志看出「上次切换没走完」，回滚按日志里的旧状态恢复旧版。
工单本身按 (批次键, item_id) 唯一，同一批重试只补缺、不新增、不挪位。
"""

from __future__ import annotations

from app import config, errors, store


def state_key(batch_key: str) -> str:
    return f"plan_switch:{batch_key}"


def load_state(batch_key: str) -> dict:
    state = store.get_kv(state_key(batch_key)) or {}
    state.setdefault("items", {})
    state.setdefault("journal", [])
    return state


def _save_state(batch_key: str, state: dict) -> None:
    store.set_kv(state_key(batch_key), state)


def _now() -> str:
    return config.now_utc().isoformat()


def _entry(state: dict, item_id: str) -> dict:
    return state["items"].get(item_id) or {"status": "available", "previous": None,
                                           "updated_at": None, "note": None}


def _log(state: dict, action: str, item_id: str, before: str, after: str,
         note: str | None = None) -> None:
    state["journal"].append({
        "action": action,
        "item_id": item_id,
        "from": before,
        "to": after,
        "at": _now(),
        "note": note,
    })


def require_item(plan: dict, item_id: str) -> dict:
    for item in plan.get("items") or []:
        if item["item_id"] == item_id:
            return item
    raise errors.NotFound("排程里没有这项工单", detail={"item_id": item_id})


def begin_switch(plan: dict, batch_key: str, item_id: str) -> dict:
    """prepare 阶段：先确认可切换，再把「正在切」写进日志。

    plan 必须是带切换状态的视图（service.current_plan），闸门只认这一处结论。
    """
    item = require_item(plan, item_id)
    state = load_state(batch_key)
    current = _entry(state, item_id)
    if current["status"] == "switching":
        raise errors.SwitchInterrupted(
            "上一轮切换没走完，先回滚再重试",
            detail={"item_id": item_id, "reason": "SWITCH_INTERRUPTED"},
        )
    if current["status"] == "switched":
        return {"item": item, "state": state, "already": True}
    if not item.get("effective_switchable"):
        raise errors.DependencyUnreleased(
            "依赖未解除的字段不能进入可切换状态",
            detail={
                "item_id": item_id,
                "column": item["column"],
                "wave": item["wave"],
                "switch_status": current["status"],
                "condition": item["condition"],
                "deps": item["deps"],
                "reason": "DEP_UNRELEASED",
            },
        )
    state["items"][item_id] = {
        "status": "switching",
        "previous": current["status"],
        "updated_at": _now(),
        "note": "prepare 已写日志，commit 未完成",
    }
    _log(state, "prepare", item_id, current["status"], "switching", item["condition"])
    _save_state(batch_key, state)
    return {"item": item, "state": state, "already": False}


def commit_switch(batch_key: str, item_id: str) -> dict:
    """commit 阶段：把状态从 switching 推到 switched。"""
    state = load_state(batch_key)
    current = _entry(state, item_id)
    if current["status"] != "switching":
        raise errors.SwitchInterrupted(
            "没有可提交的切换",
            detail={"item_id": item_id, "status": current["status"],
                    "reason": "SWITCH_INTERRUPTED"},
        )
    state["items"][item_id] = {
        "status": "switched",
        "previous": current["previous"],
        "updated_at": _now(),
        "note": None,
    }
    _log(state, "commit", item_id, "switching", "switched")
    _save_state(batch_key, state)
    return current


def switch(plan: dict, batch_key: str, item_id: str) -> dict:
    begun = begin_switch(plan, batch_key, item_id)
    if begun["already"]:
        return {"item_id": item_id, "status": "switched", "already": True,
                "code": "PLAN_SWITCHED", "journal": begun["state"]["journal"]}
    commit_switch(batch_key, item_id)
    state = load_state(batch_key)
    return {"item_id": item_id, "status": "switched", "already": False,
            "code": "PLAN_SWITCHED", "journal": state["journal"]}


def rollback(plan: dict, batch_key: str, item_id: str) -> dict:
    """回滚到切换前的状态；中断的切换同样按日志里的旧状态恢复。

    依赖项先切换、后回滚，所以回滚要逆着依赖链走：有已切换的下游依赖时先拒，
    否则会出现「前置项回退、下游还按新版跑」的两种结论。
    """
    item = require_item(plan, item_id)
    state = load_state(batch_key)
    current = _entry(state, item_id)
    if current["status"] not in ("switching", "switched"):
        raise errors.BadInput(
            "没有可回滚的切换",
            detail={"item_id": item_id, "status": current["status"],
                    "reason": "NOTHING_TO_ROLLBACK"},
        )
    dependents = [
        other for other in plan.get("items") or []
        if other["item_id"] != item_id
        and any(dep["item_id"] == item_id for dep in other.get("deps") or [])
        and _entry(state, other["item_id"])["status"] == "switched"
    ]
    if current["status"] == "switched" and dependents:
        raise errors.Conflict(
            "有依赖项已切换，先回滚依赖项",
            detail={
                "item_id": item_id,
                "dependents": [other["item_id"] for other in dependents],
                "reason": "DEPENDENT_SWITCHED",
            },
        )
    restore = current["previous"] or "available"
    state["items"][item_id] = {
        "status": "rolled_back",
        "previous": restore,
        "updated_at": _now(),
        "note": f"恢复旧版：{restore}" if current["status"] == "switching"
                else "已切回切换前状态",
    }
    _log(state, "rollback", item_id, current["status"], "rolled_back",
         f"恢复 {restore}")
    _save_state(batch_key, state)
    return {
        "item_id": item_id,
        "status": "rolled_back",
        "restored": restore,
        "interrupted": current["status"] == "switching",
        "code": "PLAN_ROLLED_BACK",
        "column": item["column"],
        "journal": state["journal"],
    }


def annotate(plan: dict, batch_key: str) -> dict:
    """把切换状态合并进排程视图，并在这里做唯一的可切换闸门判定。

    闸门三条同时成立才算可切换：自身能修、依赖已切换、本批窗口还有槽。
    页面、排程 CSV、迁移单都读这一份，所以三处结论必然一致。
    """
    from app.modules.m8_plan import model as plan_model

    state = load_state(batch_key)
    capacity = int((plan.get("summary") or {}).get("capacity") or 0)
    items = [dict(item) for item in plan.get("items") or []]
    by_id = {item["item_id"]: item for item in items}
    status_of = {item["item_id"]: _entry(state, item["item_id"])["status"]
                 for item in items}
    switched = {item_id for item_id, status in status_of.items() if status == "switched"}

    # 窗口槽位：已切换项和切换中断项都占位；中断项回滚后才把槽位放出来
    used = sum(1 for status in status_of.values()
               if status in ("switched", "switching"))
    for item in items:
        status = status_of[item["item_id"]]
        item["switch_status"] = status
        item["interrupted"] = status == "switching"
        item["deps_released"] = _deps_released(item, by_id, switched)

        if status == "switched":
            item["effective_switchable"] = False
            item["process_state"] = "已切换"
            item["state_code"] = "PLAN_SWITCHED"
            item["wave"] = plan_model.WAVE_THIS
            item["slot"] = None
            item["condition"] = "已切换；可回滚恢复旧版"
            continue

        if status == "switching":
            item["effective_switchable"] = False
            item["process_state"] = "切换中断，可回滚"
            item["state_code"] = "SWITCH_INTERRUPTED"
            item["wave"] = plan_model.WAVE_HOLD
            item["slot"] = None
            item["condition"] = "上次切换未走完，先回滚再重试"
            continue

        if item["self_blocked"] or item["dep_blocked"]:
            item["effective_switchable"] = False
            item["process_state"] = "依赖未解除"
            item["state_code"] = "PLAN_BLOCKED"
            item["wave"] = plan_model.WAVE_BLOCKED
            item["slot"] = None
            item["condition"] = plan_model.condition_text(item, by_id, switched, capacity, used)
            continue

        if not item["deps_released"]:
            # 依赖能解除、只是还没切：排下批，等依赖切完自动补位
            item["effective_switchable"] = False
            item["process_state"] = "待依赖切换"
            item["state_code"] = "PLAN_WAITING"
            item["wave"] = plan_model.WAVE_NEXT
            item["slot"] = None
            item["condition"] = plan_model.condition_text(item, by_id, switched, capacity, used)
            continue

        if used < capacity:
            used += 1
            item["effective_switchable"] = True
            item["process_state"] = "可切换"
            item["state_code"] = "PLAN_READY"
            item["wave"] = plan_model.WAVE_THIS
            item["slot"] = used
        else:
            item["effective_switchable"] = False
            item["process_state"] = "本批窗口已满"
            item["state_code"] = "WINDOW_FULL"
            item["wave"] = plan_model.WAVE_NEXT
            item["slot"] = None
        item["condition"] = plan_model.condition_text(item, by_id, switched, capacity, used)

    summary = dict(plan.get("summary") or {})
    summary.update({
        "switchable": sum(1 for item in items if item["effective_switchable"]),
        "pending": sum(1 for item in items if item["effective_switchable"]),
        "blocked": sum(1 for item in items if item["wave"] == plan_model.WAVE_BLOCKED),
        "deferred": sum(1 for item in items if item["wave"] == plan_model.WAVE_NEXT),
        "switched": sum(1 for item in items if item["switch_status"] == "switched"),
        "interrupted": sum(1 for item in items if item["interrupted"]),
        "rolled_back": sum(1 for item in items if item["switch_status"] == "rolled_back"),
        "window_used": used,
        "window_held": used,
    })
    return {
        "summary": summary,
        "policy": plan.get("policy") or {},
        "items": items,
        "journal": state["journal"],
    }


def _deps_released(item: dict, by_id: dict[str, dict], switched: set[str]) -> bool:
    if item.get("self_blocked"):
        return False
    return all(dep["item_id"] in switched
               for dep in item.get("deps") or [] if dep["item_id"] in by_id)


def ensure_orders(batch_key: str, items: list[dict]) -> dict:
    """落工单：同批次重试只补缺，不新增行、不搬位置。"""
    before = store.count_plan_orders(batch_key)
    stored = {row["item_id"] for row in store.list_plan_orders(batch_key)}
    store.save_plan_orders(batch_key, items)
    after = store.count_plan_orders(batch_key)
    added = [item["item_id"] for item in items if item["item_id"] not in stored]
    return {"created": after - before, "total": after, "reused": before > 0,
            "added": added}
