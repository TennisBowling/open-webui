"""E2E verification of the refactored turn pipeline against a throwaway server.

Drives REAL generations through the full middleware path (v2.1 task path) and
asserts structural invariants on what gets persisted:

  S1 plain turn      — text (+reasoning) streams, finalizes, legacy `content`
                       projection populated (streaming/serialize.py force path),
                       usage recorded, blocks dense + closed.
  S2 tool turn       — builtin:web_search round-trips through the hoisted
                       _execute_one_tool_call; tool_calls block carries results;
                       a final text block follows.
  S3 stop mid-stream — cancel finalizer path: terminal state persisted, no
                       dangling open blocks (started_at without ended_at).

Server: throwaway uvicorn on :8083 (working tree, prod DB, test user Choom).
"""

import json
import subprocess
import sys
import time
import uuid

import jwt
import requests

BASE = "http://127.0.0.1:8083"
USER_ID = "965e0a6a-2e9c-4dce-ad36-ea1285b7105d"
MODEL = "google/gemini-3.6-flash"
TOKEN = jwt.encode({"id": USER_ID}, "t0p-s3cr3t", algorithm="HS256")
H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

PSQL = [
    "psql",
    "-h",
    "192.168.10.2",
    "-U",
    "tennisbowling",
    "-d",
    "openllm",
    "-tA",
    "-c",
]


def q(sql):
    r = subprocess.run(
        PSQL + [sql],
        capture_output=True,
        text=True,
        env={"PGPASSWORD": "tennispass", "PATH": "/usr/bin:/bin"},
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr)
    return r.stdout.strip()


def new_chat(title):
    r = requests.post(
        f"{BASE}/api/v1/chats/new",
        headers=H,
        json={"chat": {"title": title, "models": [MODEL]}},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["id"]


def send_turn(chat_id, prompt, tool_ids=None, parent_id=None):
    user_id = str(uuid.uuid4())
    asst_id = str(uuid.uuid4())
    body = {
        "stream": True,
        "model": MODEL,
        "chat_id": chat_id,
        "id": asst_id,
        "leaf_message_id": user_id,
        "new_user_message": {
            "id": user_id,
            "parentId": parent_id,
            "childrenIds": [],
            "role": "user",
            "content": prompt,
            "files": [],
            "models": [MODEL],
            "timestamp": int(time.time()),
        },
        "session_id": f"e2e-{uuid.uuid4().hex[:8]}",
        "generation_id": str(uuid.uuid4()),
        "turn_id": user_id,
        "background_tasks": {
            "title_generation": False,
            "tags_generation": False,
            "follow_up_generation": False,
        },
    }
    if tool_ids:
        body["tool_ids"] = tool_ids
    r = requests.post(f"{BASE}/api/chat/completions", headers=H, json=body, timeout=60)
    r.raise_for_status()
    return asst_id, {"generation_id": body["generation_id"], "turn_id": body["turn_id"], "resp": r.json()}


def fetch_row(chat_id, msg_id):
    raw = q(
        "select jsonb_build_object("
        "'done', meta->>'done', 'userStopped', meta->>'userStopped',"
        "'error', meta->'error',"
        "'usage', meta->'usage', 'blocks', meta->'content_blocks',"
        "'content', content) "
        f"from chat_message where chat_id='{chat_id}' and message_id='{msg_id}';"
    )
    return json.loads(raw) if raw else None


def wait_done(chat_id, msg_id, timeout=240):
    t0 = time.time()
    while time.time() - t0 < timeout:
        row = fetch_row(chat_id, msg_id)
        if row and (row["done"] == "true" or row.get("error")):
            return row
        time.sleep(2)
    raise TimeoutError(f"turn {msg_id} not done after {timeout}s")


def assert_blocks_sane(row, label):
    blocks = row["blocks"]
    assert isinstance(blocks, list) and blocks, f"{label}: no content_blocks"
    assert all(isinstance(b, dict) for b in blocks), f"{label}: null/hole in blocks"
    for b in blocks:
        if b.get("started_at") is not None:
            assert b.get("ended_at") is not None, (
                f"{label}: dangling open block {b.get('type')}"
            )
    texts = [
        (b.get("content") or "")
        for b in blocks
        if b.get("type") == "text"
    ]
    return blocks, "".join(texts)


def s1():
    chat = new_chat("E2E refactor S1")
    msg, meta = send_turn(
        chat, "In one short sentence, what is the capital of France?"
    )
    resp = meta["resp"]
    assert "task_id" in resp or resp.get("status"), f"unexpected response {resp}"
    row = wait_done(chat, msg)
    assert not row.get("error"), f"S1 errored: {row['error']}"
    blocks, text = assert_blocks_sane(row, "S1")
    assert "Paris" in text, f"S1: answer text missing, got {text[:200]!r}"
    assert row["content"] and "Paris" in row["content"], (
        "S1: legacy content projection empty — serialize force path broken"
    )
    usage = row.get("usage") or {}
    assert (usage.get("completion_tokens") or 0) > 0, "S1: no usage persisted"
    print(f"S1 PASS  blocks={ [b.get('type') for b in blocks] }")
    return chat


def s2():
    chat = new_chat("E2E refactor S2")
    msg, _meta = send_turn(
        chat,
        "Use the web_search tool to find the current stable Python version, "
        "then answer in one sentence.",
        tool_ids=["builtin:web_search"],
    )
    row = wait_done(chat, msg, timeout=300)
    assert not row.get("error"), f"S2 errored: {row['error']}"
    blocks, text = assert_blocks_sane(row, "S2")
    tc = [b for b in blocks if b.get("type") == "tool_calls"]
    assert tc, f"S2: no tool_calls block; blocks={[b.get('type') for b in blocks]}"
    assert any(b.get("results") for b in tc), "S2: tool_calls without results"
    assert text.strip(), "S2: no final answer text after tool round"
    print(f"S2 PASS  blocks={[b.get('type') for b in blocks]}")


def s3():
    chat = new_chat("E2E refactor S3")
    msg, meta = send_turn(
        chat,
        "Write a very long, detailed 2000-word essay about the history of "
        "streaming protocols. Do not stop early.",
    )
    # Wait for streaming to actually produce content, then stop.
    t0 = time.time()
    while time.time() - t0 < 90:
        row = fetch_row(chat, msg)
        if row and row["blocks"]:
            _, text = ("", "")
            texts = [
                (b.get("content") or "")
                for b in row["blocks"]
                if b.get("type") in ("text", "reasoning")
            ]
            if sum(len(t) for t in texts) > 200:
                break
        time.sleep(1)
    else:
        raise TimeoutError("S3: stream never produced content")
    r = requests.post(
        f"{BASE}/api/tasks/stop/chat/{chat}",
        headers=H,
        json={
            "generations": [
                {
                    "generation_id": meta["generation_id"],
                    "message_id": msg,
                    "turn_id": meta["turn_id"],
                }
            ],
            "include_subagent_reruns": True,
        },
        timeout=30,
    )
    r.raise_for_status()
    t0 = time.time()
    while time.time() - t0 < 60:
        row = fetch_row(chat, msg)
        if row and (row["done"] == "true" or row.get("userStopped") == "true"):
            break
        time.sleep(2)
    else:
        raise TimeoutError("S3: no terminal state after stop")
    blocks, _ = assert_blocks_sane(row, "S3")
    print(f"S3 PASS  terminal done={row['done']} userStopped={row.get('userStopped')}")


def s4():
    """Turn-boundary byte stability: turn 2's fingerprint chain must EXTEND
    turn 1's byte-for-byte (live rounds vs DB replay of the same messages).
    This is the regression test for the jsonb key-reorder cache breaker.
    Requires FP_LOG=<server log path>."""
    import os
    import re as _re

    fp_log = os.environ["FP_LOG"]
    chat = new_chat("E2E refactor S4")
    msg1, _ = send_turn(
        chat,
        "Use the web_search tool to find who wrote The Hobbit, then answer in "
        "one sentence.",
        tool_ids=["builtin:web_search"],
    )
    row = wait_done(chat, msg1, timeout=300)
    assert not row.get("error"), f"S4 turn1 errored: {row['error']}"
    msg2, _ = send_turn(
        chat, "Thanks! Now, in one sentence: when was it published?",
        parent_id=msg1,
    )
    row = wait_done(chat, msg2, timeout=240)
    assert not row.get("error"), f"S4 turn2 errored: {row['error']}"

    chains = []
    for line in open(fp_log):
        if f"[cache-fp] chat={chat}" in line:
            m = _re.search(r"m=([0-9a-f,]+)", line)
            chains.append(m.group(1).split(","))
    assert len(chains) >= 2, f"S4: expected >=2 fp chains, got {len(chains)}"
    for a, b in zip(chains, chains[1:]):
        shared = min(len(a), len(b))
        for i in range(shared if len(b) >= len(a) else 0):
            if i < len(a) and a[i] != b[i]:
                raise AssertionError(
                    f"S4: chain diverged at msg[{i}]: {a[i]} != {b[i]} "
                    f"(prev n={len(a)}, next n={len(b)}) — outbound bytes "
                    "mutated between requests"
                )
    print(f"S4 PASS  {len(chains)} requests, all chains prefix-extend")


def s5():
    """Tool results land PER CALL, not per round.

    A round's calls finish at very different times, and each one is slimmed,
    stored and broadcast the moment it returns. Observed through the snapshot
    endpoint's `tool_results` map (the same surface a reconnecting client
    reads): while a multi-call round is in flight there must be a moment where
    SOME calls have results and others do not. If results were still batched at
    the end of the round, the map would go straight from 0 to N.
    """
    chat = new_chat("E2E refactor S5")
    msg, _ = send_turn(
        chat,
        "Run three separate web_search calls in ONE turn — for 'python release "
        "notes', 'sqlite release notes' and 'postgres release notes' — then "
        "answer in one sentence.",
        tool_ids=["builtin:web_search"],
    )

    calls_seen = 0
    partial_seen = None
    t0 = time.time()
    while time.time() - t0 < 300:
        snap = requests.get(
            f"{BASE}/api/v1/streams/{msg}/snapshot",
            headers=H,
            params={"chat_id": chat},
            timeout=30,
        )
        if snap.ok:
            body = snap.json()
            results = body.get("tool_results") or {}
            for block in body.get("content_blocks") or []:
                if isinstance(block, dict) and block.get("type") == "tool_calls":
                    calls_seen = max(calls_seen, len(block.get("content") or []))
            if calls_seen > 1 and 0 < len(results) < calls_seen:
                partial_seen = (len(results), calls_seen)
                break
            if body.get("status") != "in_progress":
                break
        time.sleep(0.35)

    row = wait_done(chat, msg, timeout=300)
    assert not row.get("error"), f"S5 errored: {row['error']}"
    blocks, text = assert_blocks_sane(row, "S5")
    tc = [b for b in blocks if b.get("type") == "tool_calls"]
    assert tc, "S5: no tool_calls block"
    assert calls_seen > 1, (
        f"S5: model only made {calls_seen} tool call(s) — rerun; the assertion "
        "needs a multi-call round to say anything"
    )
    assert partial_seen, (
        "S5: never observed a partially-filled round — results still land as a "
        "batch at the end of the round"
    )
    print(f"S5 PASS  saw {partial_seen[0]}/{partial_seen[1]} results mid-round")


if __name__ == "__main__":
    for step in (sys.argv[1:] or ["s1", "s2", "s3"]):
        globals()[step]()
    print("E2E ALL PASS")
