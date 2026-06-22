import argparse
import asyncio

from open_webui.scripts.benchmark_stream_runtime import run_benchmark, run_benchmark_suite


def test_stream_runtime_benchmark_reports_v21_savings():
    args = argparse.Namespace(
        tokens=48,
        token_chars=8,
        tool_results=2,
        tool_result_chars=2048,
        browser_frames=4,
        browser_frame_chars=4096,
        visible_tabs=1,
        hidden_tabs=2,
        json=False,
        preset="default",
    )

    result = asyncio.run(run_benchmark(args))
    legacy = result["scenarios"]["legacy"]
    optimized = result["scenarios"]["v2.1"]

    assert legacy["emits"]["packets"] > 0
    assert optimized["emits"]["packets"] > 0
    assert optimized["hidden_packets"] == 0
    assert legacy["hidden_packets"] > 0
    assert optimized["emits"]["bytes"] < legacy["emits"]["bytes"]
    assert result["savings"]["bytes"] > 0


def test_stream_runtime_benchmark_all_preset_runs_profile_matrix():
    args = argparse.Namespace(
        tokens=4,
        token_chars=2,
        tool_results=0,
        tool_result_chars=0,
        browser_frames=0,
        browser_frame_chars=0,
        visible_tabs=1,
        hidden_tabs=1,
        json=False,
        preset="all",
    )

    result = asyncio.run(run_benchmark_suite(args))
    assert result["preset"] == "all"
    assert set(result["benchmarks"]) == {
        "default",
        "long_text",
        "tool_heavy",
        "browser_heavy",
        "multi_tab",
    }
    for item in result["benchmarks"].values():
        assert item["scenarios"]["legacy"]["emits"]["packets"] > 0
        assert item["scenarios"]["v2.1"]["emits"]["packets"] > 0
