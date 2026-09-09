# Uniswap v4 hook scanner

This isolated test script scans Ethereum mainnet's canonical Uniswap v4
`PoolManager` `Initialize` logs and lists every non-zero hook used by an
initialized pool.

Run from the repository root:

```bash
backend/.venv/bin/python reference/uniswap_v4_hooks/scan_uniswap_v4_hooks.py
```

The RPC URL is read from `backend/.env`: `GETH_API` is preferred, with
`QUICKNODE_RPC_URL` as fallback. The URL is never printed. Results are written
to `reference/result/uniswap_v4_hooks/`:

- `hooks.json` and `hooks.csv`: deduplicated hooks and their pool counts.
- `pools.json`: decoded pools and their hook addresses.
- `initialize_logs.jsonl`: append-only checkpoint data.
- `scan_state.json`: the next block for resuming an interrupted scan.
- `summary.json`: scan range and totals.

Useful options:

```bash
# Rebuild results from the PoolManager deployment block
backend/.venv/bin/python reference/uniswap_v4_hooks/scan_uniswap_v4_hooks.py --restart

# Scan a bounded range while testing
backend/.venv/bin/python reference/uniswap_v4_hooks/scan_uniswap_v4_hooks.py \
  --from-block 21688329 --to-block 21698328 --restart

# Use a different endpoint without editing .env
backend/.venv/bin/python reference/uniswap_v4_hooks/scan_uniswap_v4_hooks.py \
  --rpc-url http://127.0.0.1:8545 --restart
```

The script does not query Etherscan and does not classify source verification.
It answers only the first question: which hook addresses have actually been
used by pools initialized through the canonical mainnet PoolManager?

Log queries use four workers by default, while results are persisted in block
order. Use `--workers 1` for a provider that does not permit concurrent calls.
