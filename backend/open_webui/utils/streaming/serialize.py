"""Display-only HTML+markdown projection of structured content_blocks.

Extracted verbatim from utils/middleware.py response_handler (2026-08-02
de-spaghettification); the only closure captures were metadata/request,
now explicit keyword parameters. The API-bound conversion lives in
utils/messages.blocks_to_api_messages — this is purely what the UI/legacy
clients consume.
"""

import html

from open_webui.utils import fast_json as json
from open_webui.env import (
    ENABLE_REALTIME_CHAT_SAVE,
    STREAM_PROTOCOL_VERSION,
)
from open_webui.utils.compaction import COMPACTION_BLOCK_TYPE


def serialize_content_blocks(content_blocks, force=False, *, metadata, request):
    # Display-only HTML+markdown projection of the structured content_blocks.
    # The API-bound conversion lives in `blocks_to_api_messages`; this is
    # purely what the UI's existing Markdown renderer + native <details>
    # collapsibles consume. Kept for older frontend builds that don't
    # render directly from content_blocks (post-Task 5 frontends do).
    #
    # Hot-path short-circuits (skipped when `force=True`):
    #
    # 1) Subagent inner runs never read the projected `content` string —
    #    `SubagentBlock.svelte` renders the structured `content_blocks`
    #    array directly. Returning empty here turns the per-chunk O(N)
    #    walk into O(1), so backend per-stream work scales linearly
    #    with token count even with many concurrent subagents at 200+
    #    TPS. The subagent chat row's `content` column ends up empty
    #    but the row is hidden from the sidebar and re-renders
    #    correctly from `content_blocks` if the user opens it directly.
    #
    # 2) Regular chats with `ENABLE_REALTIME_CHAT_SAVE=False` (the
    #    default): no per-chunk DB write happens, and modern
    #    frontends render from `content_blocks` (see
    #    `ContentRenderer.svelte`'s per-block keyed-each). The
    #    projected string is only needed once at end-of-stream for
    #    the canonical DB write + legacy clients + exports — those
    #    call sites pass `force=True` to bypass this short-circuit.
    #
    # When `ENABLE_REALTIME_CHAT_SAVE=True`, every per-chunk call
    # falls through and computes normally so the per-chunk DB write
    # at L2836 stores a coherent content column.
    #
    # 3) Under STREAM_PROTOCOL_VERSION="v2.1" (B9): the wire
    #    translator (`_wrap_event_emitter_v21`) drops the `content`
    #    string entirely and ships `chat:delta` ops derived from
    #    `content_blocks`. Per-chunk DB writes under v2.1 also skip
    #    the `content` column (see hot-path upsert below). The
    #    `content` column converges at end-of-stream via the
    #    `force=True` call in the success/cancel finalisers, so
    #    legacy clients, exports, and search indexing still get a
    #    populated row once streaming completes.
    if not force:
        if metadata.get("subagent_inner"):
            return ""
        if STREAM_PROTOCOL_VERSION == "v2.1":
            return ""
        if not ENABLE_REALTIME_CHAT_SAVE:
            return ""

    content = ""

    # TOTALITY CONTRACT (do not regress): this projection runs on the
    # terminal-persist critical path, so it must be defined for EVERY
    # block shape that can reach it — including ones added later by a
    # feature that never thought about the legacy string. Read `type`
    # and `content` with `.get`, never `[...]`.
    #
    # The bug this replaces: `{"type": "compaction"}` (a structural
    # transcript marker, deliberately without a `content` key) fell into
    # the trailing `else` below, which did `str(block["content"])` and
    # raised `KeyError: 'content'` from inside the success finaliser —
    # AFTER a full answer had streamed but BEFORE `done: True` was
    # persisted. The turn was reported as the bare error "'content'",
    # the client auto-retried, and the retry appended a second answer
    # into the same still-open text block. Five times.
    for block in content_blocks or []:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            block_content = str(block.get("content") or "").strip()
            if block_content:
                content = f"{content}{block_content}\n"
        elif block_type == "tool_calls":
            attributes = block.get("attributes", {})

            tool_calls = block.get("content", [])
            results = block.get("results", [])

            if content and not content.endswith("\n"):
                content += "\n"

            # Look up subagent_id either from the completed result
            # (set by `_execute_tool_call` after the tool returns)
            # or from the in-flight side channel that the subagent
            # tool stamps right at the start of its execution
            # (before it blocks on the inner chat). This way, even
            # during the long-running window between the parent
            # model emitting the tool call and the tool returning,
            # serialize_content_blocks renders a subagent block
            # instead of a generic "Executing..." tool_call.
            inflight_subagent_id_by_tcid = {}
            try:
                inflight_subagent_id_by_tcid = (
                    getattr(request.state, "subagent_id_by_tool_call", {})
                    or {}
                )
            except Exception:
                inflight_subagent_id_by_tcid = {}

            def _is_subagent_tool(name: str) -> bool:
                return name in ("subagent_launch", "subagent_continue")

            if results:

                tool_calls_display_content = ""
                for tool_call in tool_calls:

                    tool_call_id = tool_call.get("id", "")
                    tool_name = tool_call.get("function", {}).get(
                        "name", ""
                    )
                    tool_arguments = tool_call.get("function", {}).get(
                        "arguments", ""
                    )

                    tool_result = None
                    tool_result_files = None
                    result_subagent_id = None
                    result_error = False
                    result_error_reason = ""
                    result_notice = ""
                    for result in results:
                        if tool_call_id == result.get("tool_call_id", ""):
                            tool_result = result.get("content", None)
                            tool_result_files = result.get("files", None)
                            result_subagent_id = result.get("subagent_id")
                            result_error = bool(result.get("error"))
                            result_error_reason = (
                                result.get("error_reason", "") or ""
                            )
                            result_notice = result.get("notice", "") or ""
                            break

                    # Structured error/notice attributes shared by the
                    # `done="true"` tool_calls writers below. Reload
                    # parses these back into Collapsible attributes so
                    # the collapsed row shows the error/notice exactly
                    # like the live path does.
                    tool_meta_attrs = (
                        (' error="true"' if result_error else "")
                        + (
                            f' error_reason="{html.escape(str(result_error_reason))}"'
                            if result_error_reason
                            else ""
                        )
                        + (
                            f' notice="{html.escape(str(result_notice))}"'
                            if result_notice
                            else ""
                        )
                    )

                    if _is_subagent_tool(tool_name):
                        # Subagent block: lives in `subagentLiveStates`
                        # keyed by tool_call_id on the frontend; the
                        # markdown projection here is just a stub the
                        # `Collapsible.svelte` renderer recognises.
                        sa_id = (
                            result_subagent_id
                            or inflight_subagent_id_by_tcid.get(
                                tool_call_id
                            )
                            or ""
                        )
                        if not sa_id and tool_result is not None:
                            # Malformed subagent call: the tool errored
                            # BEFORE creating a subagent (e.g. missing
                            # name/prompt args), so there is no subagent
                            # to render. Emit a normal tool-result stub
                            # instead of a subagent stub — otherwise the
                            # UI shows a perpetual "Researching…/Subagent
                            # is starting up…" for a call that already
                            # returned an error.
                            tool_result_embeds = result.get("embeds", "")
                            tool_calls_display_content = f'{tool_calls_display_content}<details type="tool_calls" done="true" id="{tool_call_id}" name="{tool_name}" arguments="{html.escape(json.dumps(tool_arguments))}" result="{html.escape(json.dumps(tool_result, ensure_ascii=False))}" files="{html.escape(json.dumps(tool_result_files)) if tool_result_files else ""}" embeds="{html.escape(json.dumps(tool_result_embeds))}"{tool_meta_attrs}>\n<summary>Tool Executed</summary>\n</details>\n'
                        else:
                            done_flag = (
                                "true"
                                if tool_result is not None
                                else "false"
                            )
                            tool_calls_display_content = (
                                f"{tool_calls_display_content}"
                                f'<details type="subagent_launch" done="{done_flag}" '
                                f'tool_call_id="{html.escape(tool_call_id)}" '
                                f'id="{html.escape(sa_id)}" '
                                f'name="{html.escape(tool_name)}" '
                                f'arguments="{html.escape(json.dumps(tool_arguments))}">\n'
                                f"<summary>Subagent</summary>\n"
                                f"</details>\n"
                            )
                    elif tool_result is not None:
                        tool_result_embeds = result.get("embeds", "")
                        tool_calls_display_content = f'{tool_calls_display_content}<details type="tool_calls" done="true" id="{tool_call_id}" name="{tool_name}" arguments="{html.escape(json.dumps(tool_arguments))}" result="{html.escape(json.dumps(tool_result, ensure_ascii=False))}" files="{html.escape(json.dumps(tool_result_files)) if tool_result_files else ""}" embeds="{html.escape(json.dumps(tool_result_embeds))}"{tool_meta_attrs}>\n<summary>Tool Executed</summary>\n</details>\n'
                    else:
                        tool_calls_display_content = f'{tool_calls_display_content}<details type="tool_calls" done="false" id="{tool_call_id}" name="{tool_name}" arguments="{html.escape(json.dumps(tool_arguments))}">\n<summary>Executing...</summary>\n</details>\n'

                content = f"{content}{tool_calls_display_content}"
            else:
                tool_calls_display_content = ""

                for tool_call in tool_calls:
                    tool_call_id = tool_call.get("id", "")
                    tool_name = tool_call.get("function", {}).get(
                        "name", ""
                    )
                    tool_arguments = tool_call.get("function", {}).get(
                        "arguments", ""
                    )

                    if _is_subagent_tool(tool_name):
                        sa_id = (
                            inflight_subagent_id_by_tcid.get(tool_call_id)
                            or ""
                        )
                        tool_calls_display_content = (
                            f"{tool_calls_display_content}\n"
                            f'<details type="subagent_launch" done="false" '
                            f'tool_call_id="{html.escape(tool_call_id)}" '
                            f'id="{html.escape(sa_id)}" '
                            f'name="{html.escape(tool_name)}" '
                            f'arguments="{html.escape(json.dumps(tool_arguments))}">\n'
                            f"<summary>Subagent</summary>\n"
                            f"</details>\n"
                        )
                    else:
                        tool_calls_display_content = f'{tool_calls_display_content}\n<details type="tool_calls" done="false" id="{tool_call_id}" name="{tool_name}" arguments="{html.escape(json.dumps(tool_arguments))}">\n<summary>Executing...</summary>\n</details>\n'

                content = f"{content}{tool_calls_display_content}"

        elif block_type == "reasoning":
            reasoning_display_content = "\n".join(
                (f"> {line}" if not line.startswith(">") else line)
                for line in str(block.get("content") or "").splitlines()
            )

            reasoning_duration = block.get("duration", None)

            if content and not content.endswith("\n"):
                content += "\n"

            if reasoning_duration is not None:
                content = f'{content}<details type="reasoning" done="true" duration="{reasoning_duration}">\n<summary>Thought for {reasoning_duration} seconds</summary>\n{reasoning_display_content}\n</details>\n'
            else:
                content = f'{content}<details type="reasoning" done="false">\n<summary>Thinking…</summary>\n{reasoning_display_content}\n</details>\n'
        elif block_type == "user_steer":
            # A mid-task user interjection (steering). Render as a
            # labeled blockquote so legacy/export projections read
            # naturally; modern frontends render it from the
            # structured block via ContentRenderer.
            steer_content = str(block.get("content", "")).strip()
            if steer_content:
                if content and not content.endswith("\n"):
                    content += "\n"
                quoted = "\n".join(
                    f"> {line}" for line in steer_content.splitlines()
                )
                content = f"{content}**User:**\n{quoted}\n"
        elif block_type == "tool_selection_change":
            added = [
                str(item.get("name") or item.get("id") or "")
                for item in block.get("added", [])
                if isinstance(item, dict)
            ]
            removed = [
                str(item.get("name") or item.get("id") or "")
                for item in block.get("removed", [])
                if isinstance(item, dict)
            ]
            changes = []
            if added:
                changes.append(f"added {', '.join(added)}")
            if removed:
                changes.append(f"removed {', '.join(removed)}")
            if changes:
                if content and not content.endswith("\n"):
                    content += "\n"
                content = f"{content}*Tools updated: {'; '.join(changes)}.*\n"
        elif block_type == COMPACTION_BLOCK_TYPE:
            # Structural transcript marker, not content: it records
            # that everything ABOVE it was replaced by a summary in the
            # OUTBOUND payload only. Nothing was deleted, so the blocks
            # above already project in full and re-stating the cut here
            # would inject text the user never wrote or read. Skipped
            # for exactly the same reason `text_only_content_from_blocks`
            # (the canonical `content` column) skips it.
            pass
        else:
            # Safety net for a block type that has no projection yet.
            # Must stay total — see the TOTALITY CONTRACT above. An
            # unknown block with no string `content` contributes
            # nothing rather than raising.
            raw = block.get("content")
            block_content = (
                str(raw).strip() if isinstance(raw, (str, int, float)) else ""
            )
            if block_content:
                content = f"{content}{block_type}: {block_content}\n"
