#!/usr/bin/env python3
"""Check whether discovered Uniswap v4 hooks have verified Etherscan source.

This script uses Etherscan API V2's ``contract/getsourcecode`` endpoint. It
stores source presence and metadata, but deliberately does not duplicate full
source code in the output artifacts.
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests
from dotenv import dotenv_values


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parents[1]
DEFAULT_ENV_FILE = SCRIPT_DIR / ".env"
DEFAULT_HOOKS_FILE = ROOT / "reference" / "result" / "uniswap_v4_hooks" / "hooks.json"
DEFAULT_OUTPUT_DIR = ROOT / "reference" / "result" / "uniswap_v4_hooks" / "etherscan"
ETHERSCAN_API_URL = "https://api.etherscan.io/v2/api"
DEFAULT_CHAIN_ID = 1


class EtherscanError(RuntimeError):
    """An error safe to print without leaking the API key."""


class TransientEtherscanError(EtherscanError):
    """A retryable HTTP or Etherscan service error."""


class RateLimiter:
    def __init__(self, calls_per_second: float) -> None:
        if calls_per_second <= 0:
            raise ValueError("calls-per-second must be positive")
        self.interval = 1.0 / calls_per_second
        self.next_request_at = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            if now < self.next_request_at:
                time.sleep(self.next_request_at - now)
            self.next_request_at = time.monotonic() + self.interval


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check discovered hooks for verified source on Etherscan."
    )
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--hooks-file", type=Path, default=DEFAULT_HOOKS_FILE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--chain-id", type=int, default=DEFAULT_CHAIN_ID)
    parser.add_argument(
        "--calls-per-second",
        type=float,
        default=2.8,
        help="Etherscan request rate (free tier allows 3 calls/second).",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--retries", type=int, default=6)
    parser.add_argument(
        "--workers",
        type=int,
        default=6,
        help="Concurrent requests; their start times still obey the global rate limit.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Only check the first N pending hooks; useful for a smoke test.",
    )
    parser.add_argument(
        "--restart",
        action="store_true",
        help="Discard prior Etherscan results and query every hook again.",
    )
    return parser.parse_args()


def resolve_api_key(env_file: Path) -> str:
    values = dotenv_values(env_file)
    for name in ("etherscan_api_key", "ETHERSCAN_API_KEY"):
        value = (os.environ.get(name) or values.get(name) or "").strip()
        if value:
            return value
    raise RuntimeError(
        f"No Etherscan key found. Set etherscan_api_key in {env_file} "
        "or ETHERSCAN_API_KEY in the process environment."
    )


def write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def load_checkpoint(path: Path) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return records
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                records[str(record["hook"]).lower()] = record
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise RuntimeError(
                    f"Invalid checkpoint record at {path}:{line_number}"
                ) from exc
    return records


def is_transient_message(message: str) -> bool:
    lowered = message.lower()
    return any(
        marker in lowered
        for marker in (
            "rate limit",
            "max calls",
            "timeout",
            "temporarily unavailable",
            "server too busy",
            "try again",
        )
    )


def query_source(
    session: requests.Session,
    limiter: RateLimiter,
    api_key: str,
    chain_id: int,
    address: str,
    timeout: float,
    retries: int,
) -> dict[str, Any]:
    params = {
        "chainid": str(chain_id),
        "module": "contract",
        "action": "getsourcecode",
        "address": address,
        "apikey": api_key,
    }
    last_error: EtherscanError | None = None

    for attempt in range(retries):
        limiter.wait()
        try:
            response = session.get(ETHERSCAN_API_URL, params=params, timeout=timeout)
        except requests.RequestException as exc:
            # Never stringify requests exceptions: their URL contains the API key.
            last_error = TransientEtherscanError(type(exc).__name__)
        else:
            if response.status_code == 429 or response.status_code >= 500:
                last_error = TransientEtherscanError(f"HTTP {response.status_code}")
            elif response.status_code != 200:
                raise EtherscanError(f"HTTP {response.status_code}")
            else:
                try:
                    body = response.json()
                except requests.JSONDecodeError:
                    last_error = TransientEtherscanError("invalid JSON response")
                else:
                    status = str(body.get("status", ""))
                    message = str(body.get("message", ""))
                    result = body.get("result")
                    error_text = result if isinstance(result, str) else message
                    if status == "1" and isinstance(result, list) and result:
                        item = result[0]
                        if not isinstance(item, dict):
                            raise EtherscanError("unexpected result item")
                        return item
                    if "invalid api key" in str(error_text).lower():
                        raise EtherscanError("Etherscan rejected the API key")
                    if is_transient_message(str(error_text)):
                        last_error = TransientEtherscanError(str(error_text))
                    else:
                        raise EtherscanError(
                            f"Etherscan returned status={status}, message={message}, "
                            f"result={str(error_text)[:200]}"
                        )

        if attempt + 1 < retries:
            time.sleep(min(30.0, 2.0**attempt))

    assert last_error is not None
    raise last_error


def normalize_result(hook: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    source = str(item.get("SourceCode") or "")
    abi = str(item.get("ABI") or "")
    verified = bool(source.strip())
    implementation = str(item.get("Implementation") or "").strip()
    if implementation.lower() == "0x0000000000000000000000000000000000000000":
        implementation = ""

    return {
        "hook": hook["hook"],
        "pool_count": hook.get("pool_count"),
        "first_seen_block": hook.get("first_seen_block"),
        "last_seen_block": hook.get("last_seen_block"),
        "current_code_size": hook.get("current_code_size"),
        "current_code_hash": hook.get("current_code_hash"),
        "etherscan_source_verified": verified,
        "source_code_length": len(source),
        "source_code_sha256": hashlib.sha256(source.encode()).hexdigest() if verified else None,
        "abi_available": bool(abi.strip())
        and "not verified" not in abi.lower(),
        "contract_name": str(item.get("ContractName") or ""),
        "contract_file_name": str(item.get("ContractFileName") or ""),
        "compiler_version": str(item.get("CompilerVersion") or ""),
        "compiler_type": str(item.get("CompilerType") or ""),
        "license_type": str(item.get("LicenseType") or ""),
        "is_proxy": str(item.get("Proxy") or "0") == "1",
        "implementation": implementation or None,
        "similar_match": str(item.get("SimilarMatch") or "") or None,
        "query_status": "ok",
        "error": None,
    }


def error_result(hook: dict[str, Any], error: EtherscanError) -> dict[str, Any]:
    return {
        "hook": hook["hook"],
        "pool_count": hook.get("pool_count"),
        "first_seen_block": hook.get("first_seen_block"),
        "last_seen_block": hook.get("last_seen_block"),
        "current_code_size": hook.get("current_code_size"),
        "current_code_hash": hook.get("current_code_hash"),
        "etherscan_source_verified": None,
        "source_code_length": None,
        "source_code_sha256": None,
        "abi_available": None,
        "contract_name": "",
        "contract_file_name": "",
        "compiler_version": "",
        "compiler_type": "",
        "license_type": "",
        "is_proxy": None,
        "implementation": None,
        "similar_match": None,
        "query_status": "error",
        "error": str(error)[:300],
    }


_thread_state = threading.local()


def thread_session() -> requests.Session:
    session = getattr(_thread_state, "session", None)
    if session is None:
        session = requests.Session()
        session.headers["User-Agent"] = "evm-transaction-analyzer-hook-source-check/1.0"
        _thread_state.session = session
    return session


def check_hook(
    hook: dict[str, Any],
    limiter: RateLimiter,
    api_key: str,
    chain_id: int,
    timeout: float,
    retries: int,
) -> dict[str, Any]:
    try:
        item = query_source(
            thread_session(),
            limiter,
            api_key,
            chain_id,
            hook["hook"],
            timeout,
            retries,
        )
        return normalize_result(hook, item)
    except TransientEtherscanError as exc:
        return error_result(hook, exc)


CSV_FIELDS = (
    "hook",
    "pool_count",
    "first_seen_block",
    "last_seen_block",
    "current_code_size",
    "current_code_hash",
    "etherscan_source_verified",
    "source_code_length",
    "contract_name",
    "contract_file_name",
    "compiler_version",
    "compiler_type",
    "license_type",
    "is_proxy",
    "implementation",
    "implementation_source_verified",
    "effective_source_available",
    "similar_match",
    "query_status",
    "error",
)


def write_csv(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)


def write_outputs(
    output_dir: Path,
    hooks: list[dict[str, Any]],
    results_by_address: dict[str, dict[str, Any]],
    implementation_results: dict[str, dict[str, Any]],
    chain_id: int,
) -> None:
    results: list[dict[str, Any]] = []
    for hook in hooks:
        record = results_by_address.get(hook["hook"].lower())
        if record is None:
            continue
        record = dict(record)
        implementation = str(record.get("implementation") or "").lower()
        implementation_record = implementation_results.get(implementation)
        implementation_verified = (
            implementation_record.get("etherscan_source_verified")
            if implementation_record is not None
            else None
        )
        record["implementation_source_verified"] = implementation_verified
        record["effective_source_available"] = bool(
            record["etherscan_source_verified"] is True
            or implementation_verified is True
        )
        results.append(record)
    verified = [r for r in results if r["etherscan_source_verified"] is True]
    unverified = [r for r in results if r["etherscan_source_verified"] is False]
    no_effective_source = [r for r in results if not r["effective_source_available"]]
    errors = [r for r in results if r["query_status"] == "error"]
    required_implementations = {
        str(r["implementation"]).lower()
        for r in results
        if r.get("is_proxy") and r.get("implementation")
    }
    implementation_errors = [
        r for r in implementation_results.values() if r["query_status"] == "error"
    ]
    pending_implementations = required_implementations - implementation_results.keys()
    pending = len(hooks) - len(results)

    write_json(output_dir / "hooks_with_etherscan_status.json", results)
    write_csv(output_dir / "verified_hooks.csv", verified)
    write_csv(output_dir / "unverified_hooks.csv", unverified)
    write_csv(output_dir / "hooks_without_effective_source.csv", no_effective_source)
    write_json(
        output_dir / "implementation_source_checks.json",
        list(implementation_results.values()),
    )
    write_json(output_dir / "query_errors.json", errors)
    write_json(
        output_dir / "summary.json",
        {
            "chain_id": chain_id,
            "total_hooks": len(hooks),
            "checked_hooks": len(results),
            "pending_hooks": pending,
            "verified_source_hooks": len(verified),
            "unverified_source_hooks": len(unverified),
            "proxy_hooks": sum(r["is_proxy"] is True for r in results),
            "unique_proxy_implementations": len(implementation_results),
            "proxy_implementations_with_verified_source": sum(
                r["etherscan_source_verified"] is True
                for r in implementation_results.values()
            ),
            "effective_source_available_hooks": sum(
                r["effective_source_available"] for r in results
            ),
            "hooks_without_effective_source": len(no_effective_source),
            "query_errors": len(errors) + len(implementation_errors),
            "complete": (
                pending == 0
                and not errors
                and not implementation_errors
                and not pending_implementations
            ),
        },
    )


def main() -> int:
    args = parse_args()
    if args.retries < 1 or args.timeout <= 0 or args.workers < 1:
        raise ValueError("retries, timeout, and workers must be positive")
    if args.limit is not None and args.limit < 1:
        raise ValueError("limit must be positive")

    api_key = resolve_api_key(args.env_file)
    hooks = json.loads(args.hooks_file.read_text(encoding="utf-8"))
    if not isinstance(hooks, list) or any("hook" not in hook for hook in hooks):
        raise RuntimeError(f"Unexpected hooks file format: {args.hooks_file}")

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = output_dir / "etherscan_source_checks.jsonl"
    if args.restart:
        for name in (
            "etherscan_source_checks.jsonl",
            "hooks_with_etherscan_status.json",
            "verified_hooks.csv",
            "unverified_hooks.csv",
            "hooks_without_effective_source.csv",
            "implementation_source_checks.jsonl",
            "implementation_source_checks.json",
            "query_errors.json",
            "summary.json",
        ):
            (output_dir / name).unlink(missing_ok=True)

    results_by_address = load_checkpoint(checkpoint_path)
    # Errors are retried on the next run; successful results are never queried twice.
    pending = [
        hook
        for hook in hooks
        if results_by_address.get(hook["hook"].lower(), {}).get("query_status") != "ok"
    ]
    completed_before_run = len(hooks) - len(pending)
    if args.limit is not None:
        pending = pending[: args.limit]

    print(
        f"Hooks={len(hooks)}, completed={completed_before_run}, "
        f"pending this run={len(pending)}, rate={args.calls_per_second:.2f}/s",
        flush=True,
    )
    limiter = RateLimiter(args.calls_per_second)
    started_at = time.monotonic()

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                check_hook,
                hook,
                limiter,
                api_key,
                args.chain_id,
                args.timeout,
                args.retries,
            ): hook
            for hook in pending
        }
        for index, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            address = result["hook"]
            results_by_address[address.lower()] = result
            append_jsonl(checkpoint_path, result)
            if index == 1 or index % 100 == 0 or index == len(pending):
                elapsed = time.monotonic() - started_at
                verified_count = sum(
                    record.get("etherscan_source_verified") is True
                    for record in results_by_address.values()
                )
                print(
                    f"Checked {index}/{len(pending)} this run; "
                    f"verified so far={verified_count}; elapsed={elapsed:.1f}s",
                    flush=True,
                )

    implementation_checkpoint = output_dir / "implementation_source_checks.jsonl"
    implementation_results = load_checkpoint(implementation_checkpoint)
    implementations = sorted(
        {
            str(record["implementation"])
            for record in results_by_address.values()
            if record.get("is_proxy") and record.get("implementation")
        },
        key=str.lower,
    )
    pending_implementations = [
        address
        for address in implementations
        if implementation_results.get(address.lower(), {}).get("query_status") != "ok"
    ]
    if pending_implementations:
        print(
            f"Checking {len(pending_implementations)} proxy implementations",
            flush=True,
        )
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(
                    check_hook,
                    {"hook": address},
                    limiter,
                    api_key,
                    args.chain_id,
                    args.timeout,
                    args.retries,
                ): address
                for address in pending_implementations
            }
            for index, future in enumerate(as_completed(futures), start=1):
                result = future.result()
                implementation_results[result["hook"].lower()] = result
                append_jsonl(implementation_checkpoint, result)
                if index == 1 or index == len(pending_implementations):
                    print(
                        f"Checked proxy implementations "
                        f"{index}/{len(pending_implementations)}",
                        flush=True,
                    )

    write_outputs(
        output_dir,
        hooks,
        results_by_address,
        implementation_results,
        args.chain_id,
    )
    print(f"Results: {output_dir / 'summary.json'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except EtherscanError as exc:
        print(f"Etherscan error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
