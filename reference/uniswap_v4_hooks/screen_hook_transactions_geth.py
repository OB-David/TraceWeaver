#!/usr/bin/env python3
"""Screen Hook-calling transactions with the repository's Geth detector."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from labels.geth.detector import detect_arbitrages  # noqa: E402
from labels.geth.rpc import GethRpcClient, _flatten_frame  # noqa: E402


DEFAULT_ENV_FILE = BACKEND_ROOT / ".env"
DEFAULT_BASE_DIR = (
    ROOT / "reference" / "result" / "uniswap_v4_hooks" / "hook_arbitrages"
)
_thread_state = threading.local()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Trace Hook transactions and apply the Geth arbitrage detector."
    )
    parser.add_argument("--hook", required=True)
    parser.add_argument("--transactions-file", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--rpc-env", default="QUICKNODE_RPC_URL")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--request-timeout", type=float, default=300)
    parser.add_argument("--tracer-timeout", default="240s")
    parser.add_argument("--attempts", type=int, default=8)
    parser.add_argument("--retry-delay", type=float, default=10)
    parser.add_argument("--retry-max-delay", type=float, default=120)
    parser.add_argument("--restart", action="store_true")
    return parser.parse_args()


def resolve_rpc_url(env_file: Path, variable: str) -> str:
    value = (os.environ.get(variable) or dotenv_values(env_file).get(variable) or "")
    if not str(value).strip():
        raise RuntimeError(f"{variable} is not configured")
    return str(value).strip()


def thread_client(
    rpc_url: str, request_timeout: float, tracer_timeout: str
) -> GethRpcClient:
    client = getattr(_thread_state, "client", None)
    if client is None:
        client = GethRpcClient(
            rpc_url,
            request_timeout=request_timeout,
            tracer_timeout=tracer_timeout,
            max_attempts=4,
        )
        _thread_state.client = client
    return client


def screen_transaction(
    transaction: dict[str, Any],
    *,
    hook: str,
    rpc_url: str,
    request_timeout: float,
    tracer_timeout: str,
    attempts: int,
    retry_delay: float,
    retry_max_delay: float,
) -> dict[str, Any]:
    tx_hash = transaction["transaction_hash"].lower()
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            frame = thread_client(
                rpc_url, request_timeout, tracer_timeout
            ).request(
                "debug_traceTransaction",
                [tx_hash, {"tracer": "callTracer", "timeout": tracer_timeout}],
            )
            if not isinstance(frame, dict):
                raise RuntimeError("debug_traceTransaction returned a non-object")
            traces = _flatten_frame(
                frame=frame,
                block_number=int(transaction["block_number"]),
                transaction_hash=tx_hash,
                transaction_position=int(transaction["transaction_index"]),
                trace_address=[],
            )
            arbitrages = [item.to_dict() for item in detect_arbitrages(traces)]
            profitable = [
                item
                for item in arbitrages
                if item["error"] is None and int(item["profit_amount"]) > 0
            ]
            return {
                **transaction,
                "transaction_hash": tx_hash,
                "status": "ok",
                "trace_count": len(traces),
                "observed_hook_calls": sum(
                    trace.trace_type == "call"
                    and trace.to_address is not None
                    and trace.to_address.lower() == hook
                    for trace in traces
                ),
                "candidate_count": len(arbitrages),
                "profitable_count": len(profitable),
                "is_arbitrage": bool(profitable),
                "candidates": arbitrages,
                "error": None,
            }
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(min(retry_max_delay, retry_delay * 2.0**attempt))
    assert last_error is not None
    return {
        **transaction,
        "transaction_hash": tx_hash,
        "status": "error",
        "trace_count": None,
        "observed_hook_calls": None,
        "candidate_count": None,
        "profitable_count": None,
        "is_arbitrage": None,
        "candidates": [],
        "error": f"{type(last_error).__name__}: {last_error}",
    }


def load_latest(path: Path) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return latest
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                latest[record["transaction_hash"].lower()] = record
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise RuntimeError(f"invalid checkpoint line {line_number}") from exc
    return latest


def append_record(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "transaction_hash",
        "block_number",
        "transaction_index",
        "swap_event_count",
        "inferred_hook_calls",
        "observed_hook_calls",
        "trace_count",
        "candidate_count",
        "profitable_count",
        "is_arbitrage",
        "status",
        "error",
        "candidates",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    **{field: row.get(field) for field in fields},
                    "candidates": json.dumps(
                        row.get("candidates", []), separators=(",", ":")
                    ),
                }
            )


def main() -> int:
    args = parse_args()
    if min(args.workers, args.attempts) < 1:
        raise ValueError("workers and attempts must be positive")
    if min(args.request_timeout, args.retry_delay, args.retry_max_delay) <= 0:
        raise ValueError("timeouts and retry delays must be positive")

    hook = args.hook.lower()
    default_dir = DEFAULT_BASE_DIR / hook
    output_dir = (args.output_dir or default_dir).resolve()
    transactions_file = (
        args.transactions_file or default_dir / "hook_transactions.json"
    ).resolve()
    transactions = json.loads(transactions_file.read_text(encoding="utf-8"))
    by_hash = {item["transaction_hash"].lower(): item for item in transactions}
    if len(by_hash) != len(transactions):
        raise RuntimeError("transactions file contains duplicate hashes")

    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = output_dir / "geth_trace_screening.jsonl"
    if args.restart:
        checkpoint.unlink(missing_ok=True)
    latest = load_latest(checkpoint)
    completed = {
        tx_hash: record
        for tx_hash, record in latest.items()
        if tx_hash in by_hash and record.get("status") == "ok"
    }
    pending = [
        item
        for item in transactions
        if item["transaction_hash"].lower() not in completed
    ]

    rpc_url = resolve_rpc_url(args.env_file, args.rpc_env)
    client = GethRpcClient(rpc_url, request_timeout=args.request_timeout)
    if client.chain_id() != 1:
        raise RuntimeError("RPC endpoint is not Ethereum mainnet")
    print(
        f"transactions={len(transactions)} completed={len(completed)} "
        f"pending={len(pending)} workers={args.workers}",
        flush=True,
    )

    executor = ThreadPoolExecutor(max_workers=args.workers)
    iterator = iter(pending)
    futures: dict[Any, dict[str, Any]] = {}

    def submit_next() -> bool:
        try:
            transaction = next(iterator)
        except StopIteration:
            return False
        future = executor.submit(
            screen_transaction,
            transaction,
            hook=hook,
            rpc_url=rpc_url,
            request_timeout=args.request_timeout,
            tracer_timeout=args.tracer_timeout,
            attempts=args.attempts,
            retry_delay=args.retry_delay,
            retry_max_delay=args.retry_max_delay,
        )
        futures[future] = transaction
        return True

    for _ in range(args.workers):
        if not submit_next():
            break
    processed = 0
    try:
        while futures:
            done, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                transaction = futures.pop(future)
                try:
                    record = future.result()
                except BaseException as exc:
                    record = {
                        **transaction,
                        "transaction_hash": transaction["transaction_hash"].lower(),
                        "status": "error",
                        "is_arbitrage": None,
                        "candidates": [],
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                append_record(checkpoint, record)
                latest[record["transaction_hash"]] = record
                if record["status"] == "ok":
                    completed[record["transaction_hash"]] = record
                processed += 1
                if processed == 1 or processed % 50 == 0 or processed == len(pending):
                    strict_count = sum(
                        bool(item.get("is_arbitrage")) for item in completed.values()
                    )
                    print(
                        f"processed={processed}/{len(pending)} "
                        f"successful_total={len(completed)}/{len(transactions)} "
                        f"strict_arbitrages={strict_count}",
                        flush=True,
                    )
                submit_next()
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    ordered = [
        latest.get(item["transaction_hash"].lower(), item) for item in transactions
    ]
    successful = [item for item in ordered if item.get("status") == "ok"]
    candidates = [item for item in successful if item.get("candidate_count", 0) > 0]
    arbitrages = [item for item in successful if item.get("is_arbitrage")]
    failures = [item for item in ordered if item.get("status") != "ok"]
    write_json(output_dir / "geth_screening_results.json", ordered)
    write_csv(output_dir / "geth_screening_results.csv", ordered)
    write_json(output_dir / "geth_candidate_transactions.json", candidates)
    write_csv(output_dir / "geth_candidate_transactions.csv", candidates)
    write_json(output_dir / "geth_arbitrage_transactions.json", arbitrages)
    write_csv(output_dir / "geth_arbitrage_transactions.csv", arbitrages)
    write_json(
        output_dir / "geth_screening_summary.json",
        {
            "hook": hook,
            "transaction_count": len(transactions),
            "successful_traces": len(successful),
            "failed_traces": len(failures),
            "candidate_transactions": len(candidates),
            "profitable_arbitrage_transactions": len(arbitrages),
            "complete": not failures,
            "detector": "backend.labels.geth.detector.detect_arbitrages",
        },
    )
    print(
        f"DONE successful={len(successful)} failures={len(failures)} "
        f"candidates={len(candidates)} strict_arbitrages={len(arbitrages)}",
        flush=True,
    )
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
