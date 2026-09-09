#!/usr/bin/env python3
"""Rank fully Etherscan-unverified Uniswap v4 hooks by callback count.

Counts are inferred from successful canonical PoolManager operation events and
the callback permission bits encoded in each hook address. Return-delta flags
change callback behavior but do not add another callback invocation.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from web3 import Web3


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = ROOT / "backend" / ".env"
RESULT_DIR = ROOT / "reference" / "result" / "uniswap_v4_hooks"
HOOKS_FILE = RESULT_DIR / "hooks.json"
UNVERIFIED_FILE = (
    RESULT_DIR
    / "etherscan"
    / "hooks_without_effective_source.csv"
)
SCAN_SUMMARY_FILE = RESULT_DIR / "summary.json"
DEFAULT_OUTPUT_DIR = RESULT_DIR / "call_counts"

POOL_MANAGER = Web3.to_checksum_address(
    "0x000000000004444c5dc75cB358380D2e3dE08A90"
)

SWAP_TOPIC = Web3.to_hex(
    Web3.keccak(text="Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")
).lower()
MODIFY_LIQUIDITY_TOPIC = Web3.to_hex(
    Web3.keccak(text="ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)")
).lower()
DONATE_TOPIC = Web3.to_hex(
    Web3.keccak(text="Donate(bytes32,address,uint256,uint256)")
).lower()
OPERATION_TOPICS = [SWAP_TOPIC, MODIFY_LIQUIDITY_TOPIC, DONATE_TOPIC]

# Hook address permission bits from Uniswap v4-core Hooks.sol.
BEFORE_INITIALIZE = 1 << 13
AFTER_INITIALIZE = 1 << 12
BEFORE_ADD_LIQUIDITY = 1 << 11
AFTER_ADD_LIQUIDITY = 1 << 10
BEFORE_REMOVE_LIQUIDITY = 1 << 9
AFTER_REMOVE_LIQUIDITY = 1 << 8
BEFORE_SWAP = 1 << 7
AFTER_SWAP = 1 << 6
BEFORE_DONATE = 1 << 5
AFTER_DONATE = 1 << 4


@dataclass(frozen=True)
class Task:
    group_index: int
    start: int
    end: int
    pool_ids: tuple[str, ...]

    @property
    def task_id(self) -> str:
        # Include the pool set in the ID so checkpoints remain correct when
        # --pool-group-size changes. The legacy ID only encoded group_index,
        # which could silently reuse results produced for a different pool set.
        pool_digest = hashlib.sha256(
            "\n".join(self.pool_ids).encode("ascii")
        ).hexdigest()[:16]
        return f"v2:{pool_digest}:{self.start}:{self.end}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Count successful PoolManager callbacks to fully unverified hooks."
    )
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--hooks-file", type=Path, default=HOOKS_FILE)
    parser.add_argument("--unverified-file", type=Path, default=UNVERIFIED_FILE)
    parser.add_argument("--scan-summary-file", type=Path, default=SCAN_SUMMARY_FILE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--pool-group-size",
        type=int,
        default=1_000,
        help="Pool IDs per eth_getLogs filter (default: 1000, Geth's topic limit)",
    )
    parser.add_argument("--chunk-size", type=int, default=10_000)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--restart", action="store_true")
    return parser.parse_args()


def resolve_rpc_url(env_file: Path) -> str:
    values = dotenv_values(env_file)
    for name in ("GETH_API", "QUICKNODE_RPC_URL"):
        value = (os.environ.get(name) or values.get(name) or "").strip()
        if value:
            return value
    raise RuntimeError(f"No GETH_API or QUICKNODE_RPC_URL found in {env_file}")


def callback_weights(address: str) -> dict[str, int]:
    mask = int(address, 16) & ((1 << 14) - 1)
    return {
        "initialize": int(bool(mask & BEFORE_INITIALIZE))
        + int(bool(mask & AFTER_INITIALIZE)),
        "add": int(bool(mask & BEFORE_ADD_LIQUIDITY))
        + int(bool(mask & AFTER_ADD_LIQUIDITY)),
        "remove": int(bool(mask & BEFORE_REMOVE_LIQUIDITY))
        + int(bool(mask & AFTER_REMOVE_LIQUIDITY)),
        "swap": int(bool(mask & BEFORE_SWAP)) + int(bool(mask & AFTER_SWAP)),
        "donate": int(bool(mask & BEFORE_DONATE)) + int(bool(mask & AFTER_DONATE)),
    }


def signed_word(data: Any, word_index: int) -> int:
    raw = bytes(data)
    start = word_index * 32
    word = raw[start : start + 32]
    if len(word) != 32:
        raise RuntimeError(f"Event data is missing word {word_index}")
    return int.from_bytes(word, byteorder="big", signed=True)


def load_inputs(
    hooks_file: Path,
    unverified_file: Path,
) -> tuple[list[dict[str, Any]], dict[str, tuple[str, dict[str, int]]]]:
    with unverified_file.open(newline="", encoding="utf-8") as handle:
        target_addresses = {
            row["hook"].lower() for row in csv.DictReader(handle) if row.get("hook")
        }
    all_hooks = json.loads(hooks_file.read_text(encoding="utf-8"))
    hooks = [hook for hook in all_hooks if hook["hook"].lower() in target_addresses]
    if len(hooks) != len(target_addresses):
        found = {hook["hook"].lower() for hook in hooks}
        raise RuntimeError(
            f"Missing {len(target_addresses - found)} target hooks from {hooks_file}"
        )

    pool_map: dict[str, tuple[str, dict[str, int]]] = {}
    for hook in hooks:
        weights = callback_weights(hook["hook"])
        for pool_id in hook["pool_ids"]:
            normalized_pool = pool_id.lower()
            previous = pool_map.get(normalized_pool)
            if previous is not None and previous[0].lower() != hook["hook"].lower():
                raise RuntimeError(f"Pool {pool_id} maps to multiple hooks")
            pool_map[normalized_pool] = (hook["hook"], weights)
    return hooks, pool_map


def build_tasks(
    hooks: list[dict[str, Any]],
    pool_map: dict[str, tuple[str, dict[str, int]]],
    to_block: int,
    group_size: int,
    chunk_size: int,
) -> list[Task]:
    hook_first_seen = {
        hook["hook"].lower(): int(hook["first_seen_block"]) for hook in hooks
    }
    ordered_pools = sorted(
        pool_map,
        key=lambda pool: (hook_first_seen[pool_map[pool][0].lower()], pool),
    )
    tasks: list[Task] = []
    for group_index, offset in enumerate(range(0, len(ordered_pools), group_size)):
        pool_ids = tuple(ordered_pools[offset : offset + group_size])
        start_block = min(
            hook_first_seen[pool_map[pool][0].lower()] for pool in pool_ids
        )
        for start in range(start_block, to_block + 1, chunk_size):
            tasks.append(
                Task(
                    group_index=group_index,
                    start=start,
                    end=min(start + chunk_size - 1, to_block),
                    pool_ids=pool_ids,
                )
            )
    return tasks


_thread_state = threading.local()


def thread_web3(rpc_url: str, timeout: float) -> Web3:
    web3 = getattr(_thread_state, "web3", None)
    if web3 is None:
        web3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": timeout}))
        _thread_state.web3 = web3
    return web3


def fetch_task(
    task: Task,
    rpc_url: str,
    timeout: float,
    retries: int,
) -> tuple[Task, list[Any]]:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            logs = thread_web3(rpc_url, timeout).eth.get_logs(
                {
                    "address": POOL_MANAGER,
                    "fromBlock": task.start,
                    "toBlock": task.end,
                    "topics": [OPERATION_TOPICS, list(task.pool_ids)],
                }
            )
            return task, list(logs)
        except Exception as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(min(120.0, 10.0 * 2.0**attempt))
    assert last_error is not None
    raise RuntimeError(
        f"RPC failed for task {task.task_id}: {type(last_error).__name__}"
    ) from last_error


def count_task_logs(
    task: Task,
    logs: list[Any],
    pool_map: dict[str, tuple[str, dict[str, int]]],
) -> dict[str, Any]:
    counts: dict[str, int] = defaultdict(int)
    matched_events = 0
    for log in logs:
        if len(log["topics"]) < 2:
            continue
        pool_id = Web3.to_hex(log["topics"][1]).lower()
        mapping = pool_map.get(pool_id)
        if mapping is None:
            raise RuntimeError(f"RPC returned unexpected PoolId {pool_id}")
        _, weights = mapping
        topic = Web3.to_hex(log["topics"][0]).lower()
        if topic == SWAP_TOPIC:
            increment = weights["swap"]
        elif topic == DONATE_TOPIC:
            increment = weights["donate"]
        elif topic == MODIFY_LIQUIDITY_TOPIC:
            liquidity_delta = signed_word(log["data"], 2)
            increment = weights["add"] if liquidity_delta > 0 else weights["remove"]
        else:
            raise RuntimeError(f"Unexpected event topic {topic}")
        matched_events += 1
        if increment:
            counts[pool_id] += increment
    return {
        "task_id": task.task_id,
        "group_index": task.group_index,
        "from_block": task.start,
        "to_block": task.end,
        "matched_events": matched_events,
        "callback_counts": dict(counts),
    }


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def load_task_results(path: Path) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return results
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                results[record["task_id"]] = record
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise RuntimeError(f"Invalid checkpoint at {path}:{line_number}") from exc
    return results


def select_task_results(
    tasks: list[Task],
    checkpoint_results: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], int]:
    """Select only results produced for the current exact task definitions."""
    expected_ids = {task.task_id for task in tasks}
    selected = {
        task_id: result
        for task_id, result in checkpoint_results.items()
        if task_id in expected_ids
    }
    return selected, len(checkpoint_results) - len(selected)


def write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def build_ranking(
    hooks: list[dict[str, Any]],
    task_results: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    counts: dict[str, int] = defaultdict(int)
    # A successful Initialize log means enabled initialize callbacks completed.
    for hook in hooks:
        initialize_weight = callback_weights(hook["hook"])["initialize"]
        for pool_id in hook["pool_ids"]:
            counts[pool_id.lower()] += initialize_weight

    for result in task_results.values():
        for pool_id, count in result["callback_counts"].items():
            counts[pool_id.lower()] += int(count)

    ranking = []
    for hook in hooks:
        pools = [
            {"poolid": pool_id, "calls": counts[pool_id.lower()]}
            for pool_id in hook["pool_ids"]
        ]
        pools.sort(key=lambda item: (-item["calls"], item["poolid"].lower()))
        ranking.append(
            {
                "hookaddress": hook["hook"],
                "total_calls": sum(item["calls"] for item in pools),
                "pools": pools,
            }
        )
    ranking.sort(key=lambda item: (-item["total_calls"], item["hookaddress"].lower()))
    return ranking


def write_csv(path: Path, ranking: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=("hookaddress", "total_calls", "pools")
        )
        writer.writeheader()
        for record in ranking:
            writer.writerow(
                {
                    "hookaddress": record["hookaddress"],
                    "total_calls": record["total_calls"],
                    "pools": json.dumps(record["pools"], separators=(",", ":")),
                }
            )


def main() -> int:
    args = parse_args()
    if min(args.pool_group_size, args.chunk_size, args.workers, args.retries) < 1:
        raise ValueError("group size, chunk size, workers, and retries must be positive")
    if args.timeout <= 0:
        raise ValueError("timeout must be positive")

    hooks, pool_map = load_inputs(args.hooks_file, args.unverified_file)
    scan_summary = json.loads(args.scan_summary_file.read_text(encoding="utf-8"))
    to_block = int(scan_summary["to_block"])
    tasks = build_tasks(
        hooks,
        pool_map,
        to_block,
        args.pool_group_size,
        args.chunk_size,
    )

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = output_dir / "operation_count_tasks.jsonl"
    if args.restart:
        for name in (
            "operation_count_tasks.jsonl",
            "unverified_hook_call_counts.json",
            "unverified_hook_call_counts.csv",
            "summary.json",
        ):
            (output_dir / name).unlink(missing_ok=True)

    checkpoint_results = load_task_results(checkpoint_path)
    completed, ignored_checkpoint_records = select_task_results(
        tasks, checkpoint_results
    )
    pending = sorted(
        (task for task in tasks if task.task_id not in completed),
        key=lambda task: (task.start, task.group_index),
    )
    rpc_url = resolve_rpc_url(args.env_file)
    web3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": args.timeout}))
    if not web3.is_connected() or web3.eth.chain_id != 1:
        raise RuntimeError("Configured RPC is not a connected Ethereum mainnet endpoint")

    print(
        f"Hooks={len(hooks)}, pools={len(pool_map)}, tasks={len(tasks)}, "
        f"completed={len(completed)}, pending={len(pending)}, "
        f"ignored_checkpoint_records={ignored_checkpoint_records}, "
        f"to_block={to_block}",
        flush=True,
    )
    started_at = time.monotonic()
    executor = ThreadPoolExecutor(max_workers=args.workers)
    pending_iterator = iter(pending)
    futures: dict[Any, Task] = {}

    def submit_next() -> bool:
        try:
            next_task = next(pending_iterator)
        except StopIteration:
            return False
        future = executor.submit(
            fetch_task, next_task, rpc_url, args.timeout, args.retries
        )
        futures[future] = next_task
        return True

    for _ in range(args.workers):
        if not submit_next():
            break

    index = 0
    try:
        while futures:
            done, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                futures.pop(future)
                task, logs = future.result()
                result = count_task_logs(task, logs, pool_map)
                completed[task.task_id] = result
                append_jsonl(checkpoint_path, result)
                index += 1
                if index == 1 or index % 100 == 0 or index == len(pending):
                    elapsed = time.monotonic() - started_at
                    print(
                        f"Completed {index}/{len(pending)} tasks this run; "
                        f"matched events="
                        f"{sum(r['matched_events'] for r in completed.values()):,}; "
                        f"elapsed={elapsed:.1f}s",
                        flush=True,
                    )
                submit_next()
    except BaseException:
        for future in futures:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)

    missing = {task.task_id for task in tasks} - completed.keys()
    if missing:
        raise RuntimeError(f"Cannot rank with {len(missing)} incomplete tasks")
    ranking = build_ranking(hooks, completed)
    json_path = output_dir / "unverified_hook_call_counts.json"
    csv_path = output_dir / "unverified_hook_call_counts.csv"
    write_json(json_path, ranking)
    write_csv(csv_path, ranking)
    write_json(
        output_dir / "summary.json",
        {
            "chain_id": 1,
            "pool_manager": POOL_MANAGER,
            "to_block": to_block,
            "hook_count": len(hooks),
            "pool_count": len(pool_map),
            "task_count": len(tasks),
            "matched_operation_events": sum(
                result["matched_events"] for result in completed.values()
            ),
            "total_callback_calls": sum(item["total_calls"] for item in ranking),
            "complete": True,
        },
    )
    print(f"Results: {json_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted; completed tasks remain resumable", file=sys.stderr)
        raise SystemExit(130) from None
