#!/usr/bin/env python3
"""Benchmark storage-derived ERC-20 transfers, mints and burns."""

from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from dotenv import load_dotenv
from web3 import Web3


BENCHMARK_DIR = Path(__file__).resolve().parent
ROOT = BENCHMARK_DIR.parents[1]
BACKEND_DIR = ROOT / "backend"
DEFAULT_OUTPUT_DIR = ROOT / "reference" / "result" / "transfer_edge_recovery"
sys.path.insert(0, str(BACKEND_DIR))

from utils.evm_information import TraceFormatter  # noqa: E402
from utils.extract_token_changes import pair_transactions  # noqa: E402


TRANSFER_TOPIC = bytes(Web3.keccak(text="Transfer(address,address,uint256)"))
DEPOSIT_TOPIC = bytes(Web3.keccak(text="Deposit(address,uint256)"))
WITHDRAWAL_TOPIC = bytes(Web3.keccak(text="Withdrawal(address,uint256)"))
ZERO_ADDRESS = "0x" + "00" * 20
DEFAULT_WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
CATEGORIES = ("transfer", "mint", "burn")
EventKey = tuple[str, str, str, str, int]


def _load_config() -> tuple[str, int, int | None, Path, set[str]]:
    load_dotenv(BENCHMARK_DIR / ".env")
    load_dotenv(BACKEND_DIR / ".env")

    rpc_url = os.getenv("GETH_API", "").strip()
    if not rpc_url:
        raise ValueError(
            "GETH_API is required in reference/transfer_edge_recovery/.env "
            "or backend/.env"
        )

    block_count = int(os.getenv("BENCHMARK_BLOCK_COUNT", "5"))
    if block_count <= 0:
        raise ValueError("BENCHMARK_BLOCK_COUNT must be greater than zero")

    raw_anchor = os.getenv("BENCHMARK_END_BLOCK", "").strip()
    anchor = int(raw_anchor, 0) if raw_anchor else None
    if anchor is not None and anchor < 0:
        raise ValueError("BENCHMARK_END_BLOCK cannot be negative")

    raw_output = os.getenv("BENCHMARK_OUTPUT_DIR", str(DEFAULT_OUTPUT_DIR)).strip()
    raw_output = raw_output or str(DEFAULT_OUTPUT_DIR)
    output_dir = Path(raw_output)
    if not output_dir.is_absolute():
        output_dir = BENCHMARK_DIR / output_dir

    wrapped_tokens = {
        address.strip().lower()
        for address in os.getenv("BENCHMARK_WRAPPED_NATIVE_TOKENS", DEFAULT_WETH).split(",")
        if address.strip()
    }
    return rpc_url, block_count, anchor, output_dir, wrapped_tokens


def _address_from_topic(topic: Any) -> str:
    raw = bytes(topic)
    if len(raw) != 32:
        raise ValueError("indexed address topic is not 32 bytes")
    return "0x" + raw[-20:].hex()


def _event_key(kind: str, token: str, sender: str, recipient: str, value: int) -> EventKey:
    return kind, token.lower(), sender.lower(), recipient.lower(), value


def extract_ground_truth(receipt: Any, wrapped_tokens: set[str]) -> dict[str, Counter[EventKey]]:
    """Accept zero-address Transfer and wrapped-token Deposit/Withdrawal as supply evidence."""
    truth = {category: Counter() for category in CATEGORIES}
    transfer_mints: Counter[EventKey] = Counter()
    transfer_burns: Counter[EventKey] = Counter()
    alternative_mints: Counter[EventKey] = Counter()
    alternative_burns: Counter[EventKey] = Counter()

    for log in receipt.get("logs", []):
        topics = log.get("topics", [])
        data = bytes(log.get("data", b""))
        if not topics or len(data) != 32:
            continue
        token = str(log["address"]).lower()
        topic0 = bytes(topics[0])
        value = int.from_bytes(data, byteorder="big", signed=False)

        if topic0 == TRANSFER_TOPIC and len(topics) == 3:
            sender = _address_from_topic(topics[1])
            recipient = _address_from_topic(topics[2])
            if sender == ZERO_ADDRESS:
                transfer_mints[_event_key("mint", token, ZERO_ADDRESS, recipient, value)] += 1
            elif recipient == ZERO_ADDRESS:
                transfer_burns[_event_key("burn", token, sender, ZERO_ADDRESS, value)] += 1
            else:
                truth["transfer"][_event_key("transfer", token, sender, recipient, value)] += 1
        elif token in wrapped_tokens and topic0 == DEPOSIT_TOPIC and len(topics) == 2:
            recipient = _address_from_topic(topics[1])
            alternative_mints[_event_key("mint", token, ZERO_ADDRESS, recipient, value)] += 1
        elif token in wrapped_tokens and topic0 == WITHDRAWAL_TOPIC and len(topics) == 2:
            sender = _address_from_topic(topics[1])
            alternative_burns[_event_key("burn", token, sender, ZERO_ADDRESS, value)] += 1

    # The same supply change may emit both events. Counter union keeps the larger
    # multiplicity, so either evidence source can confirm it without double counting.
    truth["mint"] = transfer_mints | alternative_mints
    truth["burn"] = transfer_burns | alternative_burns
    return truth


def extract_balance_changes(steps: list[dict[str, Any]], slot_map: dict, token_map: dict) -> list[dict]:
    """Benchmark-local balance extraction; intentionally does not build a CFG."""
    traces = defaultdict(lambda: {"value": None, "pc": None, "step": None})
    changes: list[dict] = []
    slots = {str(slot).lower(): address for slot, address in (slot_map or {}).items()}
    tokens = {str(address).lower(): name for address, name in (token_map or {}).items()}

    def normalize_hex(value: Any) -> str:
        text = str(value or "0").lower()
        return text if text.startswith("0x") else f"0x{text}"

    def hex_to_int(value: Any) -> int:
        return int(normalize_hex(value), 16)

    def normalize_address(value: Any) -> str:
        body = str(value).strip().lower().removeprefix("0x")
        return "0x" + body[-40:].zfill(40)

    for index, step in enumerate(steps or []):
        opcode = str(step.get("opcode") or "").upper()
        stack = step.get("stack") or []
        address = step.get("address")
        rw_address = step.get("RW_address")
        pc = step.get("pc", "")

        if opcode == "CALL" and len(stack) >= 3:
            value = hex_to_int(stack[-3])
            if value:
                changes.append({
                    "type": "ETH_TRANSFER",
                    "codecontract_address": address,
                    "from_address": rw_address,
                    "to_address": normalize_address(stack[-2]),
                    "eth_value": str(value),
                    "pc": pc,
                    "step": index,
                })

        if opcode not in {"SLOAD", "SSTORE"} or not stack:
            continue
        user = slots.get(str(stack[-1]).lower())
        token_name = tokens.get(str(rw_address).lower())
        if user is None or not token_name:
            continue
        key = (address, user)

        if opcode == "SLOAD":
            next_stack = steps[index + 1].get("stack", []) if index + 1 < len(steps) else []
            traces[key] = {
                "value": normalize_hex(next_stack[-1] if next_stack else "0x0"),
                "pc": pc,
                "step": index,
            }
            continue

        if len(stack) < 2 or traces[key]["value"] is None:
            continue
        loaded = traces[key]
        difference = hex_to_int(stack[-2]) - hex_to_int(loaded["value"])
        if difference:
            changes.append({
                "type": "ERC20_BALANCE_CHANGE",
                "codecontract_address": address,
                "erc20_token_address": rw_address,
                "token_name": token_name,
                "user_address": user,
                "changed_balance": str(difference),
                "SLOAD_pc": loaded["pc"],
                "SLOAD_step": loaded["step"],
                "SSTORE_pc": pc,
                "SSTORE_step": index,
            })
        traces[key] = {"value": None, "pc": None, "step": None}

    return changes


def recover_events(rpc_url: str, web3: Web3, tx_hash: str) -> dict[str, Counter[EventKey]]:
    """Recover normal transfers and keep pending mint/burn in separate categories."""
    tx = web3.eth.get_transaction(tx_hash)
    to_address = tx.get("to")
    if not to_address:
        raise ValueError("contract creation transactions are unsupported")

    formatter = TraceFormatter(rpc_url)
    standardized = formatter.get_standardized_trace(tx_hash)
    token_map = standardized.get("erc20_token_map", {})
    token_decimals = {str(address).lower(): 18 for address in token_map}
    changes = extract_balance_changes(
        standardized.get("steps", []), standardized.get("slot_map", {}), token_map
    )
    pairs, _, pending = pair_transactions(
        [str(tx["from"]).lower(), str(to_address).lower(), int(tx.get("value", 0))],
        changes,
        token_decimals,
    )

    predicted = {category: Counter() for category in CATEGORIES}
    for item in pairs:
        token = str(item.get("token_addr", "")).lower()
        if token and token != "eth":
            key = _event_key(
                "transfer",
                token,
                str(item["from"]),
                str(item["to"]),
                abs(int(item.get("amount_raw", 0))),
            )
            predicted["transfer"][key] += 1

    for item in pending:
        token = str(item.get("token_addr", "")).lower()
        user = str(item.get("user", "")).lower()
        value = int(item.get("value", 0))
        if not token or token == "eth" or not user or value == 0:
            continue
        if value > 0:
            key = _event_key("mint", token, ZERO_ADDRESS, user, value)
            predicted["mint"][key] += 1
        else:
            key = _event_key("burn", token, user, ZERO_ADDRESS, abs(value))
            predicted["burn"][key] += 1
    return predicted


def _combine(groups: dict[str, Counter[EventKey]]) -> Counter[EventKey]:
    combined: Counter[EventKey] = Counter()
    for category in CATEGORIES:
        combined += groups[category]
    return combined


def _comparison(truth: Counter[EventKey], predicted: Counter[EventKey]) -> dict[str, int]:
    return {
        "reference": sum(truth.values()),
        "predicted": sum(predicted.values()),
        "correctly_recovered": sum((truth & predicted).values()),
        "extra_recovered": sum((predicted - truth).values()),
        "missed": sum((truth - predicted).values()),
    }


def _rates(counts: dict[str, int]) -> dict[str, float | None]:
    precision_denominator = counts["correctly_recovered"] + counts["extra_recovered"]
    recall_denominator = counts["correctly_recovered"] + counts["missed"]
    precision = counts["correctly_recovered"] / precision_denominator if precision_denominator else None
    recall = counts["correctly_recovered"] / recall_denominator if recall_denominator else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall > 0
        else None
    )
    return {"precision": precision, "recall": recall, "f1": f1}


def _expanded_difference(left: Counter[EventKey], right: Counter[EventKey]) -> list[dict[str, Any]]:
    records = []
    for (kind, token, sender, recipient, value), count in sorted((left - right).items()):
        records.append({
            "category": kind,
            "token": token,
            "from": sender,
            "to": recipient,
            "value_raw": str(value),
            "count": count,
        })
    return records


def _write_json(path: Path, value: Any) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def _write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")


def main() -> int:
    rpc_url, block_count, configured_anchor, output_dir, wrapped_tokens = _load_config()
    web3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 300}))
    if not web3.is_connected():
        raise ConnectionError("cannot connect to GETH_API")

    anchor = configured_anchor if configured_anchor is not None else int(web3.eth.block_number)
    start_block = max(0, anchor - block_count + 1)
    output_dir.mkdir(parents=True, exist_ok=True)
    transaction_counts = {
        "blocks": anchor - start_block + 1,
        "transactions": 0,
        "chain_success": 0,
        "success_without_reference_event": 0,
        "success_with_reference_event": 0,
        "success_match": 0,
        "success_mismatch": 0,
        "analysis_errors": 0,
        "failed_tx": 0,
    }
    category_totals = {
        category: {
            "reference": 0,
            "predicted": 0,
            "correctly_recovered": 0,
            "extra_recovered": 0,
            "missed": 0,
        }
        for category in (*CATEGORIES, "combined")
    }
    anomalies: list[dict[str, Any]] = []

    for block_number in range(start_block, anchor + 1):
        block = web3.eth.get_block(block_number, full_transactions=False)
        for raw_hash in block["transactions"]:
            tx_hash = Web3.to_hex(raw_hash).lower()
            transaction_counts["transactions"] += 1
            receipt = web3.eth.get_transaction_receipt(tx_hash)
            if int(receipt.get("status", 0)) == 0:
                transaction_counts["failed_tx"] += 1
                anomalies.append(
                    {"kind": "chain_failed", "block_number": block_number, "tx_hash": tx_hash}
                )
                continue

            transaction_counts["chain_success"] += 1
            truth = extract_ground_truth(receipt, wrapped_tokens)
            truth["combined"] = _combine(truth)
            if not truth["combined"]:
                transaction_counts["success_without_reference_event"] += 1
                continue

            transaction_counts["success_with_reference_event"] += 1
            try:
                predicted = recover_events(rpc_url, web3, tx_hash)
                predicted["combined"] = _combine(predicted)
            except Exception as exc:
                transaction_counts["success_mismatch"] += 1
                transaction_counts["analysis_errors"] += 1
                for category in (*CATEGORIES, "combined"):
                    comparison = _comparison(truth[category], Counter())
                    for name, value in comparison.items():
                        category_totals[category][name] += value
                anomalies.append({
                    "kind": "analysis_error",
                    "block_number": block_number,
                    "tx_hash": tx_hash,
                    "ground_truth": _expanded_difference(truth["combined"], Counter()),
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:1000],
                })
                continue

            for category in (*CATEGORIES, "combined"):
                category_comparison = _comparison(truth[category], predicted[category])
                for name, value in category_comparison.items():
                    category_totals[category][name] += value
            comparison = _comparison(truth["combined"], predicted["combined"])
            if comparison["extra_recovered"] == 0 and comparison["missed"] == 0:
                transaction_counts["success_match"] += 1
                continue

            transaction_counts["success_mismatch"] += 1
            details = {}
            for category in CATEGORIES:
                category_comparison = _comparison(truth[category], predicted[category])
                if category_comparison["extra_recovered"] or category_comparison["missed"]:
                    details[category] = {
                        **category_comparison,
                        "extra": _expanded_difference(predicted[category], truth[category]),
                        "missing": _expanded_difference(truth[category], predicted[category]),
                    }
            anomalies.append({
                "kind": "recovery_mismatch",
                "block_number": block_number,
                "tx_hash": tx_hash,
                "combined": comparison,
                "categories": details,
            })

    category_results = {}
    for category in (*CATEGORIES, "combined"):
        counts = category_totals[category]
        category_results[category] = {"counts": counts, "metrics": _rates(counts)}

    reference_transactions = transaction_counts["success_with_reference_event"]
    metrics = {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "range": {"start_block": start_block, "end_block": anchor},
        "transaction_counts": transaction_counts,
        "transaction_match_rate": (
            transaction_counts["success_match"] / reference_transactions
            if reference_transactions else None
        ),
        "categories": category_results,
    }
    metrics_path = output_dir / "metrics.json"
    anomalies_path = output_dir / "anomalies.jsonl"
    _write_json(metrics_path, metrics)
    _write_jsonl(anomalies_path, anomalies)
    print(json.dumps({"transaction_match_rate": metrics["transaction_match_rate"], **{
        category: result["metrics"] for category, result in category_results.items()
    }}, separators=(",", ":")))
    print(f"metrics={metrics_path}")
    print(f"anomalies={anomalies_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
