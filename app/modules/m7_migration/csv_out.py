"""CSV 导出：字段账、引用账、运行清单。

字段身份按原样字符串写出，大整数不进 float，前后导零不丢。
"""

from __future__ import annotations

import csv
import io
import json

FIELD_HEADER = [
    "field_id", "table", "old_name", "new_name", "old_type", "new_type",
    "change", "type_detail", "required_change", "reader", "writer", "verdict",
    "primary_reason", "bucket", "ref_count", "line_old", "line_new",
]

REF_HEADER = ["consumer", "kind", "ref", "table", "column", "via", "field_id", "reason"]

PLAN_HEADER = [
    "position", "field_id", "table", "column", "primary", "change", "tier",
    "tasks", "ref_count", "layer", "action", "suggestion", "deps_released",
    "deps", "wave", "slot", "switchable", "switch_status", "state_code",
    "condition", "basis",
]


def _buffer() -> tuple[io.StringIO, "csv.writer"]:
    buffer = io.StringIO()
    return buffer, csv.writer(buffer, lineterminator="\n")


def fields_csv(rows: list[dict]) -> str:
    buffer, writer = _buffer()
    writer.writerow(FIELD_HEADER)
    for row in rows:
        writer.writerow([
            str(row.get("field_id", "")),
            row.get("table") or "",
            row.get("old_name") or "",
            row.get("new_name") or "",
            row.get("old_type") or "",
            row.get("new_type") or "",
            row.get("change") or "",
            row.get("type_detail") or "",
            row.get("required_change") or "",
            "Y" if row.get("reader_ok") else "N",
            "Y" if row.get("writer_ok") else "N",
            row.get("verdict") or "",
            row.get("primary") or "",
            row.get("bucket") or "",
            str(row.get("ref_count", 0)),
            str(row.get("line_old") or ""),
            str(row.get("line_new") or ""),
        ])
    return buffer.getvalue()


def refs_csv(run: dict) -> str:
    resolution = run.get("resolution") or {}
    buffer, writer = _buffer()
    writer.writerow(REF_HEADER)
    for item in resolution.get("resolved") or []:
        writer.writerow([item["consumer"], item["kind"], item["ref"], item["table"],
                         item["column"], item["via"], str(item["field_id"] or ""), ""])
    for item in resolution.get("broken") or []:
        writer.writerow([item["consumer"], item["kind"], item["ref"], item["table"],
                         item["column"], item.get("via") or "broken", "", "REF_BROKEN"])
    return buffer.getvalue()


def plan_csv(plan: dict) -> str:
    """修复排程导出：页面、迁移单与这份 CSV 读同一份 plan。"""
    buffer, writer = _buffer()
    writer.writerow(PLAN_HEADER)
    for item in plan.get("items") or []:
        writer.writerow([
            str(item.get("position", "")),
            str(item.get("field_id", "")),
            item.get("table") or "",
            item.get("column") or "",
            item.get("primary") or "",
            item.get("change") or "",
            "关键" if item.get("tier") == 0 else "普通",
            str(item.get("tasks", 0)),
            str(item.get("ref_count", 0)),
            (item.get("fix") or {}).get("layer") or "",
            (item.get("fix") or {}).get("kind") or "",
            (item.get("fix") or {}).get("suggestion") or "",
            "Y" if item.get("deps_released") else "N",
            "；".join(dep.get("column", "") for dep in item.get("deps") or []),
            item.get("wave") or "",
            str(item.get("slot") or ""),
            "Y" if item.get("effective_switchable") else "N",
            item.get("switch_status") or "available",
            item.get("state_code") or "",
            item.get("condition") or "",
            "；".join(item.get("basis") or []),
        ])
    return buffer.getvalue()


def ledger_csv(ledger: dict) -> str:
    buffer, writer = _buffer()
    writer.writerow(["item", "value"])
    buckets = ledger.get("buckets") or {}
    for name in ("old_total", "unchanged", "renamed", "retyped", "required",
                 "conflict", "deleted", "added", "new_total"):
        value = buckets.get(name, ledger.get("fields", {}).get(name, 0))
        writer.writerow([name, value])
    refs = ledger.get("refs") or {}
    for name in ("refs_total", "reachable", "broken", "aliased"):
        writer.writerow([name, refs.get(name, 0)])
    return buffer.getvalue()


def manifest_json(manifest: dict) -> str:
    return json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
