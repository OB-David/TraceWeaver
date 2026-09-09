# Reference experiments

This directory contains reproducible analysis code. Generated and historical
results stay locally under `result/` and are intentionally ignored by Git.

## Layout

- `transfer_edge_recovery/`: benchmark code for measuring how accurately
  storage-derived ERC-20 transfer, mint, and burn edges are reconstructed.
- `uniswap_v4_hooks/`: Uniswap v4 Hook discovery, Etherscan source checks,
  callback counting, and Geth transaction-screening scripts.
- `result/transfer_edge_recovery/`: benchmark metrics and mismatch details.
- `result/uniswap_v4_hooks/`: the complete Hook scan, source-check, callback
  count, permission-mask, and transaction-screening artifacts.

The scripts default to their matching directory under `reference/result/`.
RPC settings are read from `backend/.env`; the Etherscan key is read from
`reference/uniswap_v4_hooks/.env`. Both `.env` files remain ignored by Git.

Run commands from the repository root. See the README files in each code
directory for inputs, output schemas, and resume/restart behavior.
