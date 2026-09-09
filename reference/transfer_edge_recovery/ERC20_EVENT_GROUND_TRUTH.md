# ERC-20 Transfer Benchmark

在 `reference/transfer_edge_recovery/.env` 或 `backend/.env` 中配置 `GETH_API`；
评测专用的 `BENCHMARK_BLOCK_COUNT`（默认 5）等变量可放在前者。
`BENCHMARK_END_BLOCK` 可指定扫描锚点；留空时以
节点实时最新区块为锚点，扫描 `[anchor-count+1, anchor]`。

```bash
backend/.venv/bin/python reference/transfer_edge_recovery/transfer_benchmark.py
```

脚本分析链上成功且 receipt 含 Transfer 或受支持 wrap/unwrap 日志的交易。普通转账
与 `pairs` 比较；系统的 `pending_erc20` 保持为独立 mint/burn，不再强制计入 Transfer。

mint/burn 采用宽松参考：零地址 Transfer 和配置中 wrapped token 的
`Deposit/Withdrawal` 均可确认。同一变化同时发出两种日志时只计一次。默认支持以太坊
主网 WETH，可通过 `BENCHMARK_WRAPPED_NATIVE_TOKENS` 配置逗号分隔的 token 地址。

恢复路径只扫描标准化 opcode trace 中的 `CALL/SLOAD/SSTORE`。不下载字节码，也不做
基本块划分、CFG 连边或折叠；这些步骤只服务于可视化，不影响余额变化和 Transfer 配对。

输出仅有：

- `reference/result/transfer_edge_recovery/metrics.json`：交易计数，以及 transfer、mint、burn 和 combined
  各自的正确恢复、额外恢复、漏掉、precision/recall/F1。
- `reference/result/transfer_edge_recovery/anomalies.jsonl`：链上失败、分析异常和分类型的不匹配明细。

无参考事件的成功交易只计数，不参与准确率，避免大量 `empty == empty` 抬高指标。
`status == 0` 的交易只计数和记录，不使用 receipt 事件评测。
