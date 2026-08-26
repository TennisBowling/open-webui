"""Validation and transcript helpers for mid-generation tool selection changes.

The browser sends the complete desired selection, not a sequence of toggle
operations.  Keeping the operation as a replace makes rapid on/off changes
coalesce naturally: the agentic loop consumes the newest snapshot at its next
provider boundary and computes one net transcript event.
"""

from __future__ import annotations

from typing import Any


MAX_TOOL_SELECTION_IDS = 256
MAX_TOOL_SELECTION_LABEL_LENGTH = 160
MAX_DIRECT_TOOL_SERVERS = 32
MAX_TOOL_SELECTION_REVISION = 9_007_199_254_740_991
TOOL_FEATURE_KEYS = frozenset(
    {
        "web_search",
        "study_mode",
        "data_viz",
        "subagents",
    }
)
BUILTIN_SELECTION_LABELS = {
    "feature:web_search": "Web Search",
    "feature:study_mode": "Study Mode",
    "feature:data_viz": "Data Visualization",
    "feature:subagents": "Subagents",
}


def _unique_strings(value: Any, *, limit: int = MAX_TOOL_SELECTION_IDS) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        normalized = item.strip()
        if not normalized or len(normalized) > 512 or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
        if len(result) >= limit:
            break
    return result


def normalize_live_tool_selection(value: Any) -> dict:
    """Return the bounded, provider-ready shape accepted by the socket operation."""

    if not isinstance(value, dict):
        raise ValueError("Tool selection must be an object")

    selection_ids = _unique_strings(value.get("selection_ids"))
    tool_ids = _unique_strings(value.get("tool_ids"))

    raw_servers = value.get("tool_servers")
    tool_servers = (
        [server for server in raw_servers if isinstance(server, dict)][
            :MAX_DIRECT_TOOL_SERVERS
        ]
        if isinstance(raw_servers, list)
        else []
    )

    raw_features = value.get("features")
    features = {
        key: bool(raw_features.get(key))
        for key in TOOL_FEATURE_KEYS
        if isinstance(raw_features, dict) and key in raw_features
    }

    raw_labels = value.get("labels")
    labels: dict[str, str] = {}
    if isinstance(raw_labels, dict):
        for selection_id in selection_ids:
            label = raw_labels.get(selection_id)
            if not isinstance(label, str):
                continue
            label = label.strip()
            if label:
                labels[selection_id] = label[:MAX_TOOL_SELECTION_LABEL_LENGTH]
    for selection_id, label in BUILTIN_SELECTION_LABELS.items():
        if selection_id in selection_ids:
            labels[selection_id] = label

    raw_params = value.get("params")
    params = {}
    if isinstance(raw_params, dict) and "subagentExternalToolsEnabled" in raw_params:
        params["subagentExternalToolsEnabled"] = bool(
            raw_params["subagentExternalToolsEnabled"]
        )

    operation_id = value.get("operation_id")
    operation_id = (
        operation_id.strip()[:128]
        if isinstance(operation_id, str) and operation_id.strip()
        else ""
    )
    raw_revision = value.get("revision")
    revision = (
        min(raw_revision, MAX_TOOL_SELECTION_REVISION)
        if isinstance(raw_revision, int)
        and not isinstance(raw_revision, bool)
        and raw_revision >= 0
        else 0
    )

    return {
        "operation_id": operation_id,
        "revision": revision,
        "selection_ids": selection_ids,
        "tool_ids": tool_ids,
        "tool_servers": tool_servers,
        "features": features,
        "labels": labels,
        "params": params,
    }


def build_tool_selection_change_block(current: Any, updated: Any) -> dict | None:
    """Build one net user-side transcript event, or ``None`` for a no-op."""

    current = current if isinstance(current, dict) else {}
    updated = updated if isinstance(updated, dict) else {}
    current_ids = _unique_strings(current.get("selection_ids"))
    updated_ids = _unique_strings(updated.get("selection_ids"))
    current_set = set(current_ids)
    updated_set = set(updated_ids)
    added_ids = [
        selection_id for selection_id in updated_ids if selection_id not in current_set
    ]
    removed_ids = [
        selection_id for selection_id in current_ids if selection_id not in updated_set
    ]
    if not added_ids and not removed_ids:
        return None

    labels = {
        **(current.get("labels") if isinstance(current.get("labels"), dict) else {}),
        **(updated.get("labels") if isinstance(updated.get("labels"), dict) else {}),
        **BUILTIN_SELECTION_LABELS,
    }

    def entries(ids: list[str]) -> list[dict[str, str]]:
        return [
            {
                "id": selection_id,
                "name": str(labels.get(selection_id) or selection_id)[
                    :MAX_TOOL_SELECTION_LABEL_LENGTH
                ],
            }
            for selection_id in ids
        ]

    return {
        "type": "tool_selection_change",
        "operation_id": updated.get("operation_id") or "",
        "added": entries(added_ids),
        "removed": entries(removed_ids),
    }
