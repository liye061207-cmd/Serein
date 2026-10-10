"""Bounded pure routing/unit rules adapted from the verified Bridge baseline.
See docs/public-feature-contracts.md for provenance and host differences.
"""
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)


def _loads_loose(text):
    import json
    s = str(text or "").strip()
    while s.startswith("`"):
        s = s[1:]
    while s.endswith("`"):
        s = s[:-1]
    s = s.strip()
    if s[:4].lower() == "json":
        s = s[4:].strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    i = s.find("{")
    if i < 0:
        raise ValueError("模型返回中没有 JSON 对象")
    depth = 0
    in_str = False
    esc = False
    for j in range(i, len(s)):
        ch = s[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(s[i:j + 1])
    raise ValueError("模型返回的 JSON 不完整，疑似被截断")


TRACK_ROUTING_ROLES = {"origin", "primary_activity", "landing", "bridge", "routine"}
TRACK_EVENT_POLICIES = {"default", "rolling_engineering"}


def flushable_dialogue_units(messages, *, now):
    """Completed exchanges up to a real silence boundary, separately per session."""
    sessions = {}
    for item in sorted(messages, key=lambda item: item['id']):
        sessions.setdefault(item['session_id'], []).append(item)
    ready = []
    silence = timedelta(minutes=20)
    for rows in sessions.values():
        units = dialogue_units(rows)
        last = -1
        for index, unit in enumerate(units):
            times = [datetime.fromisoformat(m['created_at'].replace('Z', '+00:00')) for m in unit]
            end = max(times)
            if index + 1 < len(units):
                following = min(datetime.fromisoformat(m['created_at'].replace('Z', '+00:00'))
                                for m in units[index + 1])
                paused = following - end >= silence
            else:
                paused = end <= now - silence
            if paused:
                last = index
        ready.extend(unit for unit in units[:last + 1] if dialogue_unit_is_complete(unit))
    return sorted(ready, key=lambda unit: unit[0]['id'])
def dialogue_units(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    units: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_has_assistant = False
    current_starts_proactive = False
    current_has_user = False
    for message in messages:
        role = message["role"]
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        is_proactive = role == "assistant" and bool(
            metadata.get("proactive")
            or metadata.get("autonomy")
            or metadata.get("memory_event_source")
        )
        # Multiple proactive messages remain one pending exchange until the
        # user's first reply, regardless of silence between them.
        if is_proactive and current and not (current_starts_proactive and not current_has_user):
            units.append(current)
            current = []
            current_has_assistant = False
            current_starts_proactive = False
            current_has_user = False
        if role == "user" and current and current_has_assistant and not (
            current_starts_proactive and not current_has_user
        ):
            units.append(current)
            current = []
            current_has_assistant = False
            current_starts_proactive = False
            current_has_user = False
        if not current:
            current_starts_proactive = is_proactive
        current.append(message)
        if role == "user":
            current_has_user = True
        if role == "assistant":
            current_has_assistant = True
    if current:
        units.append(current)
    return units

def dialogue_unit_is_complete(unit: list[dict[str, Any]]) -> bool:
    if not unit:
        return False
    first = unit[0]
    first_metadata = first.get("metadata") if isinstance(first.get("metadata"), dict) else {}
    starts_proactive = first.get("role") == "assistant" and bool(
        first_metadata.get("proactive")
        or first_metadata.get("autonomy")
        or first_metadata.get("memory_event_source")
    )
    if starts_proactive:
        # A proactive/free-activity message is not independently complete.  The
        # user's first reply completes the pending interaction; a later assistant
        # landing joins when present, but is not required before routing.
        return any(item.get("role") == "user" for item in unit[1:])
    if first.get("role") == "assistant":
        # This is a late answer/landing whose initiating messages may already be
        # in the durable ledger.  Buffering it would block every later message
        # in the session; route it with the existing Track cards as context.
        return True
    if first.get("role") != "user":
        return False
    return any(item.get("role") == "assistant" for item in unit[1:])

def normalize_event_track_message_output(
    output: dict[str, Any],
    messages: list[dict[str, Any]],
    active_tracks: list[dict[str, Any]],
    *,
    session_id: int,
    next_track_ordinal: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    payload_keys = set(output).difference(
        {"_splitter_provider", "_splitter_model", "_splitter_provider_index", "_codex_job"}
    )
    expected_keys = {"message_assignments", "track_updates"}
    if not expected_keys.issubset(payload_keys):
        raise ValueError(
            "Track Router top-level fields missing: "
            f"{sorted(expected_keys - payload_keys)}"
        )
    raw_assignments = output.get("message_assignments")
    raw_updates = output.get("track_updates")
    if not isinstance(raw_assignments, list) or not isinstance(raw_updates, list):
        raise ValueError("Track Router must return message_assignments and track_updates")
    expected_ids = [int(item["id"]) for item in messages]
    existing = {
        str(track.get("track_id") or ""): track
        for track in active_tracks
        if str(track.get("track_id") or "")
    }
    try:
        _payload = {
            "assignments": raw_assignments,
            "updates": raw_updates,
            "expected_ids": expected_ids,
        }
        with open("/tmp/track_router_raw.json", "w", encoding="utf-8") as _f:
            _f.write(json.dumps(_payload, ensure_ascii=False, indent=2, default=str))
    except Exception as _exc:
        try:
            with open("/tmp/track_router_raw.err", "w", encoding="utf-8") as _f2:
                _f2.write(repr(_exc) + "\n\n=== assignments ===\n")
                _f2.write(repr(raw_assignments)[:8000])
                _f2.write("\n\n=== updates ===\n")
                _f2.write(repr(raw_updates)[:8000])
        except Exception:
            pass
    assignments: list[dict[str, Any]] = []
    used_refs: list[str] = []
    required_fields = {
        "source_message_id",
        "primary_track_ref",
        "context_track_refs",
        "routing_role",
    }
    n = len(expected_ids)
    pos = {mid: i for i, mid in enumerate(expected_ids)}
    raw_ids = [raw.get("source_message_id") if isinstance(raw, dict) else None
               for raw in raw_assignments]

    # 模式判断：全是 1..n 的小整数 = 模型在数序号；否则当 ID 处理
    ordinal_mode = (
        bool(raw_ids)
        and all(type(v) is int for v in raw_ids)
        and set(raw_ids) <= set(range(1, n + 1))
    )

    parsed = []
    for index, raw in enumerate(raw_assignments):
        if not isinstance(raw, dict):
            raise ValueError(f"Track Router message assignment #{index + 1} must be an object")
        if not required_fields.issubset(raw):
            raise ValueError(
                f"Track Router message assignment #{index + 1} fields missing: "
                f"{sorted(required_fields - set(raw))}"
            )
        primary_ref = str(raw.get("primary_track_ref") or "").strip()
        raw_context_refs = raw.get("context_track_refs")
        routing_role = str(raw.get("routing_role") or "").strip()
        last_primary = assignments[-1]["primary_track_ref"] if assignments else None
        if primary_ref not in existing and not re.fullmatch(r"new:[1-9][0-9]*", primary_ref):
            logger.warning(
                "Track Router 第 %s 条 primary=%r 未知，沿用上一条", index + 1, primary_ref
            )
            primary_ref = last_primary or "new:1"
        if not isinstance(raw_context_refs, list):
            logger.warning("Track Router 第 %s 条 context_track_refs 不是 list，已清空", index + 1)
            raw_context_refs = []
        context_refs: list[str] = []
        for value in raw_context_refs:
            ref = str(value or "").strip()
            if not ref or ref == primary_ref or ref in context_refs:
                continue
            if ref not in existing and not re.fullmatch(r"new:[1-9][0-9]*", ref):
                logger.warning("Track Router 第 %s 条 context=%r 未知，已丢弃", index + 1, ref)
                continue
            context_refs.append(ref)
        if routing_role not in TRACK_ROUTING_ROLES:
            logger.warning(
                "Track Router 第 %s 条返回非法 routing_role=%r，已降为 primary_activity",
                index + 1, routing_role,
            )
            routing_role = "primary_activity"
        if routing_role == "bridge" and not context_refs:
            logger.warning(
                "Track Router 第 %s 条标了 bridge 却没给 context Track，已降为 default",
                index + 1,
            )
            routing_role = "default"
        elif routing_role != "bridge" and context_refs:
            logger.warning(
                "Track Router 第 %s 条给了 context Track 却没标 bridge，已升为 bridge",
                index + 1,
            )
            routing_role = "bridge"

        if ordinal_mode:
            position = index
        else:
            v = raw.get("source_message_id")
            if type(v) is int and v in pos:
                position = pos[v]
            else:
                logger.warning(
                    "Track Router 第 %s 条返回 source_message_id=%r，不在本批中，按位置对齐",
                    index + 1, v,
                )
                position = index
        parsed.append((position, {
            "primary_track_ref": primary_ref,
            "context_track_refs": context_refs,
            "routing_role": routing_role,
        }))


    filled: dict[int, dict[str, Any]] = {}
    for position, item in parsed:
        filled[position] = item

    last_item = None
    for p in range(n):
        if p not in filled:
            if last_item is None:
                logger.warning("Track Router 第一条消息未归线，已分配默认 new:1")
                last_item = {"primary_track_ref": "new:1", "context_track_refs": [], "routing_role": "default"}
                filled[0] = last_item
                continue
            item = dict(last_item)
            item["routing_role"] = "default"
            item["context_track_refs"] = []
            logger.warning(
                "Track Router 漏了 source_message_id=%s，已并入上一条 Track",
                expected_ids[p],
            )
            filled[p] = item
        last_item = filled[p]

    for p in range(n):
        item = dict(filled[p])
        item["source_message_id"] = expected_ids[p]
        assignments.append(item)
        used_refs.extend([item["primary_track_ref"], *item["context_track_refs"]])

    update_by_ref: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(raw_updates):
        if not isinstance(raw, dict):
            raise ValueError(f"Track Router update #{index + 1} must be an object")
        required_update_fields = {"track_ref", "subject", "throughline", "status"}
        if not required_update_fields.issubset(raw):
            raise ValueError(
                f"Track Router update #{index + 1} fields missing: "
                f"{sorted(required_update_fields - set(raw))}"
            )
        track_ref = str(raw.get("track_ref") or "").strip()
        subject = " ".join(str(raw.get("subject") or "").split())
        throughline = " ".join(str(raw.get("throughline") or "").split())
        event_policy = str(raw.get("event_policy") or "").strip()
        status = str(raw.get("status") or "").strip()
        if track_ref in update_by_ref:
            logger.warning("Track Router 重复更新了 Track %s，已取第一条", track_ref)
            continue
        if track_ref not in existing and not re.fullmatch(r"new:[1-9][0-9]*", track_ref):
            raise ValueError("Track Router updated an invalid or repeated Track")
        if not subject or len(subject) > 160 or not throughline or len(throughline) > 600:
            raise ValueError("Track Router update needs bounded subject and throughline")
        if status not in {"active", "parked"}:
            logger.warning("Track Router 更新 Track %s 的 status=%r 无效，已修正为 active", track_ref, status)
            status = "active"
        existing_policy = str((existing.get(track_ref) or {}).get("event_policy") or "default")
        if not event_policy:
            event_policy = existing_policy
        if event_policy not in TRACK_EVENT_POLICIES:
            raise ValueError("Track Router update has invalid event_policy")
        if existing_policy == "rolling_engineering":
            event_policy = existing_policy
        update_by_ref[track_ref] = {
            "subject": subject,
            "throughline": throughline,
            "event_policy": event_policy,
            "status": status,
        }
    used_ref_set = set(used_refs)

    # new: 序号错位纠正：模型用了 new 引用，但 update 里的序号对不上
    new_used = [r for r in used_ref_set if r.startswith("new:")]
    new_updated = [r for r in update_by_ref if r.startswith("new:")]
    new_missing = [r for r in new_used if r not in update_by_ref]
    if new_missing and new_updated:
        mapping = {}
        for i, ref in enumerate(new_missing):
            mapping[ref] = new_updated[i % len(new_updated)]
        for item in assignments:
            item["primary_track_ref"] = mapping.get(item["primary_track_ref"], item["primary_track_ref"])
            item["context_track_refs"] = [mapping.get(r, r) for r in item["context_track_refs"]]
        used_ref_set = set()
        for item in assignments:
            used_ref_set.add(item["primary_track_ref"])
            used_ref_set.update(item["context_track_refs"])
        logger.warning("Track Router new 序号错位，已重映射：%s", mapping)

    # 补齐：assignments 用到但 updates 没写的 Track，沿用 existing 原值
    for ref in sorted(used_ref_set - set(update_by_ref)):
        if ref.startswith("new:"):
            raise ValueError(f"Track Router 新建了 Track {ref}，但没有给出 update")
        origin = existing.get(ref) or {}
        subject = " ".join(str(origin.get("subject") or "").split())
        throughline = " ".join(str(origin.get("throughline") or "").split())
        if not subject:
            raise ValueError(f"Track Router 未更新 Track {ref}，existing 中也没有可用 subject")
        origin_policy = str(origin.get("event_policy") or "default")
        update_by_ref[ref] = {
            "subject": subject[:160],
            "throughline": throughline[:600],
            "event_policy": origin_policy,
            "status": str(origin.get("status") or "active"),
        }
        logger.warning("Track Router 未更新 Track %s，已沿用原值", ref)

    # updates 里多出来的：保留，只记一笔
    extra = sorted(set(update_by_ref) - used_ref_set)
    if extra:
        logger.warning("Track Router 更新了本批未使用的 Track %s，已保留", extra)

    new_refs = sorted(
        {ref for ref in used_refs if ref not in existing},
        key=lambda value: int(value.split(":", 1)[1]),
    )
    ref_to_track_id = {ref: ref for ref in existing}
    ordinal = next_track_ordinal
    for ref in new_refs:
        ref_to_track_id[ref] = f"session_{session_id}_track_{ordinal:04d}"
        ordinal += 1
    return (
        [
            {
                "source_message_id": item["source_message_id"],
                "primary_track_id": ref_to_track_id[item["primary_track_ref"]],
                "context_track_ids": [ref_to_track_id[ref] for ref in item["context_track_refs"]],
                "routing_role": item["routing_role"],
            }
            for item in assignments
        ],
        [
            {"track_id": ref_to_track_id[ref], **update_by_ref[ref]}
            for ref in dict.fromkeys(used_refs)
        ],
        ordinal,
    )
