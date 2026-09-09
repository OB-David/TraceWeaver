# Count calls to fully unverified Uniswap v4 hooks

Run from the repository root:

```bash
backend/.venv/bin/python reference/uniswap_v4_hooks/count_unverified_hook_calls.py
```

The input set is `etherscan/hooks_without_effective_source.csv`. The scanner
counts successful callback invocations inferred from canonical PoolManager
events and Hook address permission bits:

- each enabled before/after callback counts as one invocation;
- add versus remove liquidity is selected from the signed `liquidityDelta`;
- return-delta permission flags do not add invocations;
- reverted transactions are not present in successful event logs and are not
  counted.

The block cutoff is copied from the original Hook discovery snapshot, so pool
membership and call counts describe the same reproducible chain range.

Results under `reference/result/uniswap_v4_hooks/call_counts/` contain exactly:

```json
{
  "hookaddress": "0x...",
  "total_calls": 123,
  "pools": [{"poolid": "0x...", "calls": 123}]
}
```

The JSON and CSV are sorted by `total_calls` descending. RPC work is split by
PoolId and block range. Every finished task is appended to a JSONL checkpoint;
rerunning the same command resumes instead of double-counting.
The worker queue is bounded and defaults to four concurrent RPC requests to
avoid overloading providers that serialize large `eth_getLogs` calls.
