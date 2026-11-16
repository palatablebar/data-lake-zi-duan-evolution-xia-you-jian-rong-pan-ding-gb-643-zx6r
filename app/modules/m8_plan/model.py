"""修复工单模型：把破坏项排成批次，逐项给出处理位置、依赖与可切换条件。

排序只看三件事，且都写死在 ruleset 里：
1. 关键链路（收款 / 结算）先于公共维度；
2. 同一档内影响任务多的先做；
3. 有依赖的排在依赖之后，同档同影响按表名、字段身份定序。

可切换不是「修得动」的同义词：只要有一项依赖没解除，工单就不能进入可切换状态。
维护窗口只给可切换工单分配槽位，阻塞项不占窗口。
"""

from __future__ import annotations

from app import config, reasons
from app.modules.m2_types import coerce, range as value_range
from app.modules.m5_impact import closure

LAYER_UPSTREAM = "上游 DDL"
LAYER_MAPPING = "映射视图"
LAYER_CONSUMER = "下游消费端"

WAVE_THIS = "本批"
WAVE_NEXT = "下批"
WAVE_BLOCKED = "阻塞"
WAVE_HOLD = "中断"


def item_id_of(field_id: str) -> str:
    return f"fid:{field_id}"


def _tier(item: dict) -> int:
    if item["table"].lower() in config.CRITICAL_TABLES:
        return 0
    if set(item["consumers"]) & config.CRITICAL_CONSUMERS:
        return 0
    return 1


def _widen_suggestion(old_raw: str, new_raw: str) -> str | None:
    """给出能无损容纳旧类型的写法；证不出来就返回 None，不许猜。"""
    old = coerce.parse_type(old_raw)
    new = coerce.parse_type(new_raw)
    if not old.get("known") or not new.get("known"):
        return None
    if old["super"] != new["super"]:
        return None
    if old["super"] == "numeric":
        if "decimal" in (old["base"], new["base"]):
            if old["base"] != "decimal" or new["base"] != "decimal":
                # 整数/浮点与 decimal 混用时，取能容纳对方的那个写法
                if value_range.capacity(new, old)["fits"]:
                    return coerce.type_text(old)
                if value_range.capacity(old, new)["fits"]:
                    return coerce.type_text(new)
                return None
            old_scale = old["scale"] or 0
            new_scale = new["scale"] or 0
            old_int = (old["precision"] or 38) - old_scale
            new_int = (new["precision"] or 38) - new_scale
            scale = max(old_scale, new_scale)
            return f"decimal({max(old_int, new_int) + scale},{scale})"
        if value_range.capacity(new, old)["fits"]:
            return coerce.type_text(old)
        if value_range.capacity(old, new)["fits"]:
            return coerce.type_text(new)
        return None
    if old["super"] == "text":
        old_len = value_range.text_length(old)
        new_len = value_range.text_length(new)
        if old_len is None or new_len is None:
            return "string"
        if old_len == new_len:
            return f"varchar({old_len})" if old["base"] == new["base"] else None
        return f"varchar({max(old_len, new_len)})"
    if old["super"] == "temporal":
        rank = {"date": 0, "timestamp": 1}
        wider = old["base"] if rank.get(old["base"], -1) >= rank.get(new["base"], -1) \
            else new["base"]
        return wider
    return None


def _base_row(row: dict) -> dict:
    old_name = row.get("old_name")
    new_name = row.get("new_name")
    return {
        "field_id": row["field_id"],
        "item_id": item_id_of(row["field_id"]),
        "table": row.get("table") or "",
        "old_name": old_name,
        "new_name": new_name,
        "column": old_name or new_name or "",
        "bucket": row.get("bucket") or "",
        "change": row.get("change") or "same",
        "type_detail": row.get("type_detail"),
        "primary": row.get("primary") or "",
        "reasons": list(row.get("reasons") or []),
        "ref_count": int(row.get("ref_count") or 0),
        "consumers": [],
        "tasks": 0,
        "tier": 1,
        "fix": {},
        "deps": [],
        "position": 0,
        "wave": WAVE_BLOCKED,
        "slot": None,
        "deps_released": False,
        "dep_cycle": False,
        "condition": "",
        "basis": [],
    }


def _classify(item: dict) -> dict:
    """判定工单的修复动作与处理位置；ready=False 表示自身就修不动。"""
    codes = set(item["reasons"])
    table = item["table"]
    column = item["column"]
    old_type = item.get("old_type") or "—"
    new_type = item.get("new_type") or "—"

    if "FIELD_DELETED_REFERENCED" in codes:
        return {
            "kind": "migrate_consumers",
            "layer": LAYER_CONSUMER,
            "object": "、".join(item["consumers"]) or "下游登记",
            "ready": False,
            "blocking_reason": "DEP_UNRELEASED",
            "suggestion": "先把下游引用迁到新身份，再下线旧字段",
            "note": f"{item['ref_count']} 处引用仍指向旧身份 {item['field_id']}",
        }
    if item["bucket"] == "added" and "NAME_DUP_DIFF_ID" in codes:
        return {
            "kind": "rebind_name",
            "layer": f"{LAYER_MAPPING} + {LAYER_CONSUMER}",
            "object": f"{table}.{item['new_name']}",
            "ready": False,
            "blocking_reason": "DEP_UNRELEASED",
            "suggestion": "旧身份下线后，同步把下游绑定迁到新身份",
            "note": "同名不同身份，不能直接按名字绑到新列",
        }
    if "TYPE_UNKNOWN" in codes or "TYPE_CROSS_FAMILY" in codes:
        return {
            "kind": "declare_type",
            "layer": LAYER_UPSTREAM,
            "object": f"{table}.{column}",
            "ready": False,
            "blocking_reason": "DEP_UNRELEASED",
            "suggestion": f"先声明 {old_type} → {new_type} 的等价或保留原类型",
            "note": "类型不在类型格内，不能证明兼容",
        }
    if "MAPPING_CONFLICT" in codes or "ALIAS_CYCLE" in codes:
        return {
            "kind": "resolve_mapping",
            "layer": LAYER_MAPPING,
            "object": f"{table}.{column}",
            "ready": False,
            "blocking_reason": "DEP_UNRELEASED",
            "suggestion": "先消解多个旧名落到同一新名的冲突",
            "note": "映射冲突未消解",
        }

    suggestion = _widen_suggestion(item.get("old_type") or "", item.get("new_type") or "")
    if "RENAME_AND_RETYPE" in codes:
        if suggestion is None:
            return {
                "kind": "widen_type+alias",
                "layer": f"{LAYER_UPSTREAM} → {LAYER_MAPPING}",
                "object": f"{table}.{column}",
                "ready": False,
                "blocking_reason": "DEP_UNRELEASED",
                "suggestion": "找不到能容纳旧类型的写法，需人工定类型",
                "note": "改名同时换类型，别名不安全",
            }
        return {
            "kind": "widen_type+alias",
            "layer": f"{LAYER_UPSTREAM} → {LAYER_MAPPING}",
            "object": f"{table}.{column}",
            "ready": True,
            "blocking_reason": None,
            "suggestion": suggestion,
            "note": f"类型放宽到 {suggestion} 后才建 {item['new_name']} as {item['old_name']} 别名",
        }
    if item["change"] in ("narrow",):
        if suggestion is None:
            return {
                "kind": "widen_type",
                "layer": LAYER_UPSTREAM,
                "object": f"{table}.{column}",
                "ready": False,
                "blocking_reason": "DEP_UNRELEASED",
                "suggestion": "找不到能容纳旧类型的写法，需人工定类型",
                "note": "类型收窄",
            }
        return {
            "kind": "widen_type",
            "layer": LAYER_UPSTREAM,
            "object": f"{table}.{column}",
            "ready": True,
            "blocking_reason": None,
            "suggestion": f"放宽到 {suggestion}",
            "note": f"{old_type} → {new_type}，"
                    f"{reasons.narrow_label(item.get('type_detail'))}",
        }
    if "REQUIRED_TIGHTENED" in codes:
        return {
            "kind": "add_default",
            "layer": f"{LAYER_UPSTREAM} + 回填",
            "object": f"{table}.{column}",
            "ready": True,
            "blocking_reason": None,
            "suggestion": "回填非空值并补默认值后再加必填",
            "note": "可空改必填，写者会被新约束挡住",
        }
    return {
        "kind": "manual",
        "layer": LAYER_UPSTREAM,
        "object": f"{table}.{column}",
        "ready": False,
        "blocking_reason": "DEP_UNRELEASED",
        "suggestion": "需人工给出修复方案",
        "note": reasons.label(item["primary"]),
    }


def _impact_of(item: dict, consumers: list[dict], resolution: dict) -> dict:
    """复用影响面闭包，按单项算影响任务数，口径与 /impact 页一致。"""
    field_id = item["field_id"]
    by_field = {}
    direct = (resolution.get("by_field") or {}).get(field_id) or []
    if direct:
        by_field[field_id] = sorted(direct)

    names = {n.lower() for n in (item["old_name"], item["new_name"]) if n}
    broken_seeds: dict[str, set[str]] = {}
    broken_consumers: set[str] = set()
    for entry in resolution.get("broken") or []:
        if entry["table"].lower() != item["table"].lower():
            continue
        if entry["column"].lower() not in names:
            continue
        broken_seeds.setdefault(entry["consumer"], set()).add(f"断链 {entry['ref']}")
        broken_consumers.add(entry["consumer"])

    seed_field = {
        "field_id": field_id,
        "table": item["table"],
        "old_name": item["column"],
    }
    graph = closure.impact([seed_field], by_field, consumers,
                           broken_seeds=broken_seeds)
    return {
        "tasks": graph["total"],
        "task_names": [row["name"] for row in graph["rows"]],
        "consumers": sorted(set(direct) | broken_consumers),
    }


def _build_deps(items: list[dict]) -> None:
    """依赖只有两类，都能从登记本身推出来，不写死样例。

    D1 同名不同身份：新增字段要等旧身份的下游迁移完成，才能把名字绑过去。
    D2 别名依赖类型：同一批里共享下游节点的「放宽类型」是「建别名」的前置，
       否则别名视图建好了、下游照样被同一节点上的类型收窄挡住。
    """
    by_name = {}
    for item in items:
        for name in {item["old_name"], item["new_name"]}:
            if name:
                by_name.setdefault((item["table"].lower(), name.lower()), []).append(item)

    for item in items:
        if item["fix"]["kind"] == "rebind_name":
            for other in by_name.get((item["table"].lower(),
                                      (item["new_name"] or "").lower()), []):
                if other is item or other["field_id"] == item["field_id"]:
                    continue
                if other["bucket"] == "deleted":
                    item["deps"].append({
                        "item_id": other["item_id"],
                        "table": other["table"],
                        "column": other["column"],
                        "reason": "NAME_DUP_DIFF_ID",
                        "detail": f"旧身份 {other['field_id']} 需先下线并迁走下游",
                    })
                    break

    type_items = [item for item in items if item["fix"]["kind"] == "widen_type"]
    for item in items:
        if item["fix"]["kind"] != "widen_type+alias":
            continue
        shared = [
            other for other in type_items
            if other is not item
            and other["table"].lower() == item["table"].lower()
            and set(other["consumers"]) & set(item["consumers"])
        ]
        if shared:
            shared.sort(key=_order_key)
            target = shared[0]
            item["deps"].append({
                "item_id": target["item_id"],
                "table": target["table"],
                "column": target["column"],
                "reason": "ALIAS_AFTER_TYPE",
                "detail": "同节点类型收窄先放宽，别名才安全",
            })
        item["deps"] = sorted(item["deps"], key=lambda dep: dep["item_id"])


def _order_key(item: dict) -> tuple:
    return (item["tier"], -item["tasks"], item["table"].lower(), item["field_id"])


def _order(items: list[dict]) -> None:
    """拓扑 + 优先级：依赖先落位，其余按关键链路与影响面取。"""
    remaining = {item["item_id"]: item for item in items}
    placed: set[str] = set()
    position = 0
    while remaining:
        ready = [
            item for item in remaining.values()
            if all(dep["item_id"] in placed for dep in item["deps"])
        ]
        if not ready:
            # 登记成环时不能无限等：按优先级落位并把环标进依据
            ready = list(remaining.values())
            for item in ready:
                item["dep_cycle"] = True
        chosen = min(ready, key=_order_key)
        position += 1
        chosen["position"] = position
        placed.add(chosen["item_id"])
        remaining.pop(chosen["item_id"])


def _plan_static(items: list[dict], capacity: int) -> None:
    """静态排程：标出自身修不动的项、依赖成环的项，以及被阻塞依赖拖住的项。

    这里只回答「理论上能不能修」；真正的可切换闸门还要看切换状态，见
    lifecycle.annotate。这样页面、排程 CSV、迁移单永远读同一份判定。
    """
    by_id = {item["item_id"]: item for item in items}

    def reaches(start: str, goal: str, seen: set[str]) -> bool:
        for dep in by_id[start]["deps"]:
            dep_id = dep["item_id"]
            if dep_id not in by_id or dep_id in seen:
                continue
            if dep_id == goal:
                return True
            seen.add(dep_id)
            if reaches(dep_id, goal, seen):
                return True
        return False

    def blocked(item: dict, seen: set[str] | None = None) -> bool:
        seen = set() if seen is None else seen
        if item["item_id"] in seen:
            return False  # 环单独标记，不靠递归判阻塞
        seen.add(item["item_id"])
        if not item["fix"]["ready"]:
            return True
        return any(blocked(by_id[dep["item_id"]], seen)
                   for dep in item["deps"] if dep["item_id"] in by_id)

    slots = 0
    for item in items:
        in_cycle = reaches(item["item_id"], item["item_id"], set())
        item["dep_cycle"] = in_cycle
        item["self_blocked"] = not item["fix"]["ready"]
        item["dep_blocked"] = in_cycle or any(
            blocked(by_id[dep["item_id"]])
            for dep in item["deps"] if dep["item_id"] in by_id
        )
        if item["self_blocked"] or item["dep_blocked"]:
            item["wave"] = WAVE_BLOCKED
            item["slot"] = None
        elif item["deps"]:
            # 依赖尚未切换前先排下批，依赖一解除就能补进本批窗口
            item["wave"] = WAVE_NEXT
            item["slot"] = None
        elif slots < capacity:
            slots += 1
            item["wave"] = WAVE_THIS
            item["slot"] = slots
        else:
            item["wave"] = WAVE_NEXT
            item["slot"] = None


def condition_text(item: dict, by_id: dict[str, dict], switched: set[str],
                   capacity: int, used: int) -> str:
    """可切换条件：自身能修、依赖已切换、窗口还有槽，三条缺一不可。"""
    fix = item["fix"]
    parts: list[str] = []
    if item.get("dep_cycle"):
        parts.append("依赖成环：切换前需人工拆环")
        parts.append(f"处理方案：{fix['suggestion']}")
        return "；".join(parts)
    if item["self_blocked"]:
        parts.append(f"依赖未解除：{fix['note']}")
        parts.append(f"处理方案：{fix['suggestion']}")
        return "；".join(parts)
    if item["dep_blocked"]:
        blocked_cols = [
            by_id[dep["item_id"]]["column"] for dep in item["deps"]
            if dep["item_id"] in by_id
            and (by_id[dep["item_id"]].get("dep_blocked")
                 or by_id[dep["item_id"]].get("self_blocked"))
        ]
        parts.append(f"依赖未解除：{'、'.join(blocked_cols) or '前置项'} 不可能在本批解除")
        parts.append(f"处理方案：{fix['suggestion']}")
        return "；".join(parts)

    parts.append(f"{fix['suggestion']} 后可切换")
    pending = [
        by_id[dep["item_id"]] for dep in item["deps"]
        if dep["item_id"] in by_id and dep["item_id"] not in switched
    ]
    if pending:
        names = "、".join(f"{dep['column']}(第 {dep['position']} 项)" for dep in pending)
        parts.append(f"依赖 {names} 先切换")
    if item.get("effective_switchable") and item.get("slot"):
        parts.append(f"本批窗口第 {item['slot']} 项")
    elif not item.get("deps_released"):
        parts.append("依赖切换后补进本批窗口")
    elif used >= capacity:
        parts.append(f"本批窗口已满（{used}/{capacity}），排下批")
    else:
        parts.append("可补进本批窗口")
    return "；".join(parts)


def _basis(item: dict) -> list[str]:
    basis = [
        f"主原因 {item['primary']}（{reasons.label(item['primary'])}）",
        f"影响任务 {item['tasks']} 个"
        + (f"（{ '、'.join(item['task_names']) }）" if item["task_names"] else ""),
        f"下游引用 {item['ref_count']} 处",
    ]
    if item["type_detail"]:
        basis.append(f"类型细分 {item['type_detail']}（{reasons.narrow_label(item['type_detail'])}）")
    for dep in item["deps"]:
        basis.append(f"依赖 {dep['column']}：{dep['detail']}")
    if item.get("dep_cycle"):
        basis.append("依赖成环，按优先级落位，切换前需人工拆环")
    if item["tier"] == 0:
        basis.append(f"关键链路：{'、'.join(item['consumers']) or item['table']}")
    return basis


def build(run_like: dict) -> dict:
    """从一次判定的结果生成修复排程；同一输入必得同一顺序与同一组工单。"""
    rows = [row for row in (run_like.get("diff") or [])
            if row.get("verdict") == "BREAKING"]
    consumers = (run_like.get("refs") or {}).get("consumers") or []
    resolution = run_like.get("resolution") or {}

    items: list[dict] = []
    for row in rows:
        item = _base_row(row)
        item["old_type"] = row.get("old_type")
        item["new_type"] = row.get("new_type")
        impact = _impact_of(item, consumers, resolution)
        item["tasks"] = impact["tasks"]
        item["task_names"] = impact["task_names"]
        item["consumers"] = impact["consumers"]
        item["tier"] = _tier(item)
        item["fix"] = _classify(item)
        items.append(item)

    items.sort(key=_order_key)
    _build_deps(items)
    _order(items)
    _plan_static(items, config.PLAN_WINDOW_CAPACITY)
    for item in items:
        item["basis"] = _basis(item)

    ordered = sorted(items, key=lambda item: item["position"])
    summary = {
        "total": len(ordered),
        "breaking": len(ordered),
        "blocked": sum(1 for item in ordered if item["wave"] == WAVE_BLOCKED),
        "deferred": sum(1 for item in ordered if item["wave"] == WAVE_NEXT),
        "critical": sum(1 for item in ordered if item["tier"] == 0),
        "capacity": config.PLAN_WINDOW_CAPACITY,
        "critical_tables": sorted(config.CRITICAL_TABLES),
        "order_rule": "关键链路优先 → 影响任务多优先 → 依赖先于被依赖",
        "window_rule": "阻塞项不占窗口；本批只排窗口容量内的可切换项",
    }
    return {
        "summary": summary,
        "policy": {
            "critical_tables": sorted(config.CRITICAL_TABLES),
            "critical_consumers": sorted(config.CRITICAL_CONSUMERS),
            "capacity": config.PLAN_WINDOW_CAPACITY,
        },
        "items": ordered,
    }
