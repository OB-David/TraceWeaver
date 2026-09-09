# Check Hook source verification on Etherscan

The input is the `hooks.json` produced by `scan_uniswap_v4_hooks.py`. Put the
API key in `reference/uniswap_v4_hooks/.env`:

```dotenv
etherscan_api_key=YOUR_KEY
```

Run from the repository root:

```bash
backend/.venv/bin/python reference/uniswap_v4_hooks/check_hook_sources_etherscan.py
```

The script uses Etherscan API V2 `contract/getsourcecode`. It defaults to 2.8
requests per second, below the current free-tier limit of 3 requests per
second. Six workers hide network latency, while a shared limiter keeps request
start times within that rate. A JSONL checkpoint is flushed after every
address, so the same command resumes after interruption. Use `--restart` only
when every address should be queried again.

Results are written under `reference/result/uniswap_v4_hooks/etherscan/`:

- `verified_hooks.csv`: Hook addresses with verified source.
- `unverified_hooks.csv`: Hook addresses without verified Etherscan source.
- `hooks_without_effective_source.csv`: neither the Hook address nor its proxy
  implementation has verified source.
- `hooks_with_etherscan_status.json`: metadata for every checked Hook.
- `implementation_source_checks.json`: source status of unique proxy
  implementation addresses.
- `query_errors.json`: requests that still failed after retries.
- `summary.json`: totals and completion status.

Full source text is not copied into these artifacts. For verified contracts,
the output records its character length and SHA-256 digest plus contract,
compiler, license, proxy, implementation, and similar-match metadata.
Proxy implementations are checked separately, and `effective_source_available`
is true when either the Hook address itself or its implementation has verified
source.

Smoke test only the first three pending addresses:

```bash
backend/.venv/bin/python reference/uniswap_v4_hooks/check_hook_sources_etherscan.py \
  --limit 3 --output-dir /tmp/hook-etherscan-smoke --restart
```
