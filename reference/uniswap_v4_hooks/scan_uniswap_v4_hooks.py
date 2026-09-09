#!/usr/bin/env python3
"""Find hooks used by pools initialized through Uniswap v4 PoolManager.

The scanner only reads Ethereum JSON-RPC data. It does not query Etherscan and
therefore makes no source-verification judgement.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from web3 import Web3
from web3._utils.events import get_event_data


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = ROOT / "backend" / ".env"
DEFAULT_OUTPUT_DIR = ROOT / "reference" / "result" / "uniswap_v4_hooks"

POOL_MANAGER = Web3.to_checksum_address(
    "0x000000000004444c5dc75cB358380D2e3dE08A90"
)
POOL_MANAGER_DEPLOYMENT_TX = (
    "0x747e0e02b7590eed32cface28e83260884e0b80675f5ae223c6888053aa68528"
)
EXPECTED_CHAIN_ID = 1
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

INITIALIZE_SIGNATURE = (
    "Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)"
)
INITIALIZE_EVENT_ABI: dict[str, Any] = {
    "anonymous": False,
    "type": "event",
    "name": "Initialize",
    "inputs": [
        {"indexed": True, "name": "id", "type": "bytes32"},
        {"indexed": True, "name": "currency0", "type": "address"},
        {"indexed": True, "name": "currency1", "type": "address"},
        {"indexed": False, "name": "fee", "type": "uint24"},
        {"indexed": False, "name": "tickSpacing", "type": "int24"},
        {"indexed": False, "name": "hooks", "type": "address"},
        {"indexed": False, "name": "sqrtPriceX96", "type": "uint160"},
        {"indexed": False, "name": "tick", "type": "int24"},
    ],
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan Ethereum Uniswap v4 Initialize logs and list used hooks."
    )
    parser.add_argument(
        "--rpc-url",
        help="Ethereum JSON-RPC URL. Defaults to GETH_API, then QUICKNODE_RPC_URL.",
    )
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--from-block", type=int)
    parser.add_argument("--to-block", type=int)
    parser.add_argument("--confirmations", type=int, default=12)
    parser.add_argument("--chunk-size", type=int, default=10_000)
    parser.add_argument("--min-chunk-size", type=int, default=100)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Concurrent eth_getLogs requests (default: 4).",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--restart",
        action="store_true",
        help="Discard a previous checkpoint and scan again from --from-block.",
    )
    return parser.parse_args()


def resolve_rpc_url(explicit: str | None, env_file: Path) -> str:
    if explicit:
        return explicit.strip()
    values = dotenv_values(env_file)
    for name in ("GETH_API", "QUICKNODE_RPC_URL"):
        value = (os.environ.get(name) or values.get(name) or "").strip()
        if value:
            return value
    raise RuntimeError(
        f"No RPC URL found. Set GETH_API or QUICKNODE_RPC_URL in {env_file}, "
        "or pass --rpc-url."
    )


def hex_value(value: Any) -> str:
    return Web3.to_hex(value)


def decode_initialize_log(web3: Web3, log: Any) -> dict[str, Any]:
    decoded = get_event_data(web3.codec, INITIALIZE_EVENT_ABI, log)
    args = decoded["args"]
    return {
        "pool_id": hex_value(args["id"]),
        "currency0": Web3.to_checksum_address(args["currency0"]),
        "currency1": Web3.to_checksum_address(args["currency1"]),
        "fee": int(args["fee"]),
        "tick_spacing": int(args["tickSpacing"]),
        "hook": Web3.to_checksum_address(args["hooks"]),
        "sqrt_price_x96": int(args["sqrtPriceX96"]),
        "initial_tick": int(args["tick"]),
        "block_number": int(log["blockNumber"]),
        "transaction_hash": hex_value(log["transactionHash"]),
        "log_index": int(log["logIndex"]),
    }


def write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        f"Invalid checkpoint data at {path}:{line_number}"
                    ) from exc
    return records


def append_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    if not records:
        return
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def fetch_logs(
    web3: Web3,
    topic: str,
    start: int,
    end: int,
    retries: int,
) -> list[Any]:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            return list(
                web3.eth.get_logs(
                    {
                        "address": POOL_MANAGER,
                        "fromBlock": start,
                        "toBlock": end,
                        "topics": [topic],
                    }
                )
            )
        except Exception as exc:  # RPC providers use several exception classes.
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(2**attempt)
    assert last_error is not None
    raise last_error


def fetch_logs_from_rpc(
    rpc_url: str,
    topic: str,
    start: int,
    end: int,
    retries: int,
) -> list[Any]:
    """Use an independent provider because Web3 sessions are not thread-safe."""
    worker_web3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 90}))
    return fetch_logs(worker_web3, topic, start, end, retries)


def summarize_hooks(pools: list[dict[str, Any]], web3: Web3) -> list[dict[str, Any]]:
    by_hook: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for pool in pools:
        if pool["hook"].lower() != ZERO_ADDRESS:
            by_hook[pool["hook"]].append(pool)

    hooks: list[dict[str, Any]] = []
    total = len(by_hook)
    for index, (hook, hook_pools) in enumerate(
        sorted(by_hook.items(), key=lambda item: min(p["block_number"] for p in item[1])),
        start=1,
    ):
        if index == 1 or index % 100 == 0 or index == total:
            print(f"Reading hook bytecode {index}/{total}", flush=True)
        try:
            code = web3.eth.get_code(hook)
            code_size = len(code)
            code_hash = hex_value(Web3.keccak(code)) if code else None
        except Exception as exc:
            code_size = None
            code_hash = None
            print(f"Warning: eth_getCode failed for {hook}: {exc}", file=sys.stderr)

        hooks.append(
            {
                "hook": hook,
                "pool_count": len(hook_pools),
                "pool_ids": [pool["pool_id"] for pool in hook_pools],
                "first_seen_block": min(pool["block_number"] for pool in hook_pools),
                "last_seen_block": max(pool["block_number"] for pool in hook_pools),
                "current_code_size": code_size,
                "current_code_hash": code_hash,
            }
        )
    return hooks


def write_hook_csv(path: Path, hooks: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "hook",
                "pool_count",
                "first_seen_block",
                "last_seen_block",
                "current_code_size",
                "current_code_hash",
            ),
        )
        writer.writeheader()
        for hook in hooks:
            writer.writerow({key: hook[key] for key in writer.fieldnames})


def main() -> int:
    args = parse_args()
    if args.chunk_size < 1 or args.min_chunk_size < 1:
        raise ValueError("Chunk sizes must be positive")
    if args.retries < 1 or args.workers < 1 or args.confirmations < 0:
        raise ValueError("Retries must be positive and confirmations cannot be negative")

    rpc_url = resolve_rpc_url(args.rpc_url, args.env_file)
    web3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 90}))
    if not web3.is_connected():
        raise RuntimeError("Cannot connect to the configured Ethereum RPC endpoint")
    chain_id = web3.eth.chain_id
    if chain_id != EXPECTED_CHAIN_ID:
        raise RuntimeError(f"Expected Ethereum chain ID 1, got {chain_id}")

    deployment_receipt = web3.eth.get_transaction_receipt(POOL_MANAGER_DEPLOYMENT_TX)
    deployment_block = int(deployment_receipt["blockNumber"])
    from_block = args.from_block if args.from_block is not None else deployment_block
    latest_safe_block = max(0, web3.eth.block_number - args.confirmations)
    to_block = args.to_block if args.to_block is not None else latest_safe_block
    if from_block < deployment_block:
        print(
            f"Warning: --from-block predates PoolManager deployment ({deployment_block})",
            file=sys.stderr,
        )
    if to_block < from_block:
        raise ValueError(f"to-block {to_block} is below from-block {from_block}")

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_path = output_dir / "initialize_logs.jsonl"
    state_path = output_dir / "scan_state.json"

    if args.restart:
        for name in (
            "initialize_logs.jsonl",
            "scan_state.json",
            "pools.json",
            "hooks.json",
            "hooks.csv",
            "summary.json",
        ):
            (output_dir / name).unlink(missing_ok=True)

    current = from_block
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        compatible = (
            state.get("chain_id") == chain_id
            and state.get("pool_manager", "").lower() == POOL_MANAGER.lower()
            and state.get("from_block") == from_block
        )
        if not compatible:
            raise RuntimeError(
                f"Checkpoint {state_path} belongs to another scan; pass --restart"
            )
        current = max(from_block, int(state["next_block"]))
        print(f"Resuming at block {current}")

    topic = Web3.to_hex(Web3.keccak(text=INITIALIZE_SIGNATURE))
    chunk_size = args.chunk_size
    started_at = time.monotonic()

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        while current <= to_block:
            ranges: list[tuple[int, int]] = []
            batch_cursor = current
            for _ in range(args.workers):
                if batch_cursor > to_block:
                    break
                end = min(batch_cursor + chunk_size - 1, to_block)
                ranges.append((batch_cursor, end))
                batch_cursor = end + 1

            futures = [
                executor.submit(
                    fetch_logs_from_rpc,
                    rpc_url,
                    topic,
                    start,
                    end,
                    args.retries,
                )
                for start, end in ranges
            ]
            try:
                batch_logs = [future.result() for future in futures]
            except Exception as exc:
                for future in futures:
                    future.cancel()
                if chunk_size <= args.min_chunk_size:
                    raise RuntimeError(
                        f"eth_getLogs failed near block {current} at minimum "
                        f"chunk size {chunk_size}: {exc}"
                    ) from exc
                chunk_size = max(args.min_chunk_size, chunk_size // 2)
                print(f"RPC rejected range; reducing chunk size to {chunk_size}")
                continue

            # Futures may finish out of order, but persistence stays in block order.
            for (start, end), logs in zip(ranges, batch_logs, strict=True):
                decoded = [decode_initialize_log(web3, log) for log in logs]
                append_jsonl(raw_path, decoded)
                current = end + 1
                write_json(
                    state_path,
                    {
                        "chain_id": chain_id,
                        "pool_manager": POOL_MANAGER,
                        "from_block": from_block,
                        "target_block": to_block,
                        "next_block": current,
                        "chunk_size": chunk_size,
                    },
                )
                elapsed = time.monotonic() - started_at
                print(
                    f"Scanned {end:,}/{to_block:,}; logs={len(decoded)}; "
                    f"elapsed={elapsed:.1f}s",
                    flush=True,
                )

    # Protect against duplicate JSONL records if a process was interrupted between
    # appending a chunk and atomically advancing its checkpoint.
    unique_pools: dict[tuple[str, int], dict[str, Any]] = {}
    for pool in load_jsonl(raw_path):
        unique_pools[(pool["transaction_hash"], pool["log_index"])] = pool
    pools = sorted(
        unique_pools.values(),
        key=lambda pool: (pool["block_number"], pool["log_index"]),
    )
    hooks = summarize_hooks(pools, web3)

    pools_path = output_dir / "pools.json"
    hooks_path = output_dir / "hooks.json"
    csv_path = output_dir / "hooks.csv"
    summary_path = output_dir / "summary.json"
    write_json(pools_path, pools)
    write_json(hooks_path, hooks)
    write_hook_csv(csv_path, hooks)
    write_json(
        summary_path,
        {
            "chain_id": chain_id,
            "pool_manager": POOL_MANAGER,
            "initialize_topic": topic,
            "from_block": from_block,
            "to_block": to_block,
            "pool_count": len(pools),
            "pools_without_hook": sum(
                pool["hook"].lower() == ZERO_ADDRESS for pool in pools
            ),
            "pools_with_hook": sum(
                pool["hook"].lower() != ZERO_ADDRESS for pool in pools
            ),
            "unique_hook_count": len(hooks),
            "hooks_with_current_code": sum(
                (hook["current_code_size"] or 0) > 0 for hook in hooks
            ),
            "hooks_without_current_code": sum(
                hook["current_code_size"] == 0 for hook in hooks
            ),
            "hook_code_read_failures": sum(
                hook["current_code_size"] is None for hook in hooks
            ),
        },
    )
    print(f"Done: {len(pools)} pools, {len(hooks)} unique non-zero hooks")
    print(f"Hook list: {hooks_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
