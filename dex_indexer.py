import time
import json
import os
import sys
import sqlite3
from datetime import datetime, timezone
from web3 import Web3

sys.stdout.reconfigure(encoding='utf-8')

print('=== MIDAS AI - INITIALIZING PRODUCTION DEX INDEXER & BASE ON-CHAIN ENGINE ===')

# 1. EVM Event Topic Signatures (Multi-DEX & Transfer Coverage)
UNISWAP_V3_SWAP = Web3.keccak(text="Swap(address,address,int256,int256,uint160,128,int24)").hex()
UNISWAP_V2_SWAP = Web3.keccak(text="Swap(address,uint256,uint256,uint256,uint256,address)").hex()
BALANCER_V2_SWAP = Web3.keccak(text="Swap(bytes32,address,address,uint256,uint256)").hex()
AERODROME_V2_SWAP = Web3.keccak(text="Swap(address,address,uint256,uint256,uint256,uint256)").hex()
ERC20_TRANSFER_TOPIC = Web3.keccak(text="Transfer(address,address,uint256)").hex()

# Token Decimals & Whale Alert Thresholds
TOKEN_DECIMALS = {
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": 6,   # Base USDC
    "0x4200000000000000000000000000000000000006": 18   # Base WETH
}

WHALE_THRESHOLDS = {
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": 100000.0, # $100k USDC
    "0x4200000000000000000000000000000000000006": 50.0      # 50 WETH
}

# 2. Resilient Multi-RPC Provider Network
RPC_PROVIDERS = [
    'https://mainnet.base.org',
    'https://base-rpc.publicnode.com',
    'https://1rpc.io/base',
    'https://developer-access-mainnet.base.org'
]

# 3. Enhancement #3: SQLite Zero-Loss State Store
class SQLiteStateStore:
    def __init__(self, db_path="base_indexer_state.db"):
        self.conn = sqlite3.connect(db_path, timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self.conn.execute("PRAGMA busy_timeout=5000;")
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS checkpoint (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_contiguous_block INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS indexed_blocks (
                start_block INTEGER NOT NULL,
                end_block INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('pending', 'done')),
                indexed_at TEXT NOT NULL,
                PRIMARY KEY (start_block, end_block)
            );

            CREATE TABLE IF NOT EXISTS swap_events (
                tx_hash TEXT NOT NULL,
                log_index INTEGER NOT NULL,
                block_number INTEGER NOT NULL,
                dex TEXT NOT NULL,
                inserted_at TEXT NOT NULL,
                PRIMARY KEY (tx_hash, log_index)
            );

            CREATE TABLE IF NOT EXISTS token_transfers (
                tx_hash TEXT NOT NULL,
                log_index INTEGER NOT NULL,
                block_number INTEGER NOT NULL,
                token TEXT NOT NULL,
                from_addr TEXT NOT NULL,
                to_addr TEXT NOT NULL,
                human_value REAL NOT NULL,
                direction TEXT NOT NULL,
                is_whale INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (tx_hash, log_index)
            );

            CREATE INDEX IF NOT EXISTS idx_swap_block ON swap_events(block_number);
            CREATE INDEX IF NOT EXISTS idx_transfer_block ON token_transfers(block_number);

            INSERT OR IGNORE INTO checkpoint (id, last_contiguous_block) VALUES (1, 0);
        """)
        self.conn.commit()

    def get_last_contiguous_block(self) -> int:
        row = self.conn.execute("SELECT last_contiguous_block FROM checkpoint WHERE id = 1").fetchone()
        return row[0] if row else 0

    def store_chunk_results(self, start_block: int, end_block: int, swaps: list, transfers: list = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO indexed_blocks (start_block, end_block, status, indexed_at)
                VALUES (?, ?, 'done', ?)
                ON CONFLICT(start_block, end_block) DO UPDATE SET status = 'done', indexed_at = excluded.indexed_at
                """,
                (start_block, end_block, now)
            )

            for log in swaps:
                tx_hash = log['transactionHash'].hex() if hasattr(log['transactionHash'], 'hex') else str(log['transactionHash'])
                log_idx = log.get('logIndex', 0)
                blk_num = log.get('blockNumber', 0)
                dex = 'Multi-DEX'
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO swap_events (tx_hash, log_index, block_number, dex, inserted_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (tx_hash, log_idx, blk_num, dex, now)
                )

            if transfers:
                for tr in transfers:
                    self.conn.execute(
                        """
                        INSERT OR IGNORE INTO token_transfers (
                            tx_hash, log_index, block_number, token, from_addr, to_addr, human_value, direction, is_whale
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            tr['tx_hash'], tr['log_index'], tr['block_number'], tr['token'],
                            tr['from'], tr['to'], tr['human_value'], tr['direction'], 1 if tr.get('whale') else 0
                        )
                    )

            last = self.conn.execute("SELECT last_contiguous_block FROM checkpoint WHERE id = 1").fetchone()[0]
            if start_block == last + 1 or last == 0:
                self.conn.execute("UPDATE checkpoint SET last_contiguous_block = ? WHERE id = 1", (end_block,))

    def get_total_swaps(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM swap_events").fetchone()[0]

    def close(self):
        self.conn.close()

# 4. Production DEX Indexer Engine
class ProductionDEXIndexer:
    def __init__(self, rpc_list=None, reorg_safety_depth=12, chunk_size=25):
        self.rpc_list = rpc_list or RPC_PROVIDERS
        self.reorg_safety_depth = reorg_safety_depth
        self.chunk_size = chunk_size
        self.current_rpc_idx = 0
        self.store = SQLiteStateStore()
        self.w3 = self._connect_rpc()

    def _connect_rpc(self):
        url = self.rpc_list[self.current_rpc_idx]
        print(f'Connecting to Base RPC [{self.current_rpc_idx + 1}/{len(self.rpc_list)}]: {url}...')
        w3 = Web3(Web3.HTTPProvider(url, request_kwargs={'timeout': 10}))
        if w3.is_connected():
            print('  -> RPC Connection Verified SUCCESS!')
            return w3
        else:
            print('  -> Connection failed. Trying fallback RPC...')
            self._rotate_rpc()
            return self._connect_rpc()

    def _rotate_rpc(self):
        self.current_rpc_idx = (self.current_rpc_idx + 1) % len(self.rpc_list)
        self.w3 = Web3(Web3.HTTPProvider(self.rpc_list[self.current_rpc_idx], request_kwargs={'timeout': 10}))

    # Enhancement #2: EIP-1559 Dynamic Gas Forecaster
    def get_eip1559_gas(self, strategy="standard") -> dict:
        try:
            fee_hist = self.w3.eth.fee_history(20, 'latest', [25, 50, 75])
            base_fee = fee_hist['baseFeePerGas'][-1]
            rewards = [r for r in fee_hist['reward'] if r]

            p50_vals = [r[1] for r in rewards] if rewards else [Web3.to_wei(0.1, 'gwei')]
            max_priority = p50_vals[len(p50_vals)//2] if p50_vals else Web3.to_wei(0.1, 'gwei')

            max_fee = base_fee * 2 + max_priority
            return {
                'strategy': strategy,
                'base_fee_gwei': float(Web3.from_wei(base_fee, 'gwei')),
                'max_priority_gwei': float(Web3.from_wei(max_priority, 'gwei')),
                'max_fee_gwei': float(Web3.from_wei(max_fee, 'gwei'))
            }
        except Exception:
            gas_price = self.w3.eth.gas_price
            return {'strategy': 'legacy', 'max_fee_gwei': float(Web3.from_wei(gas_price, 'gwei'))}

    def scan_market_events(self, from_block=None, blocks_back=150):
        try:
            latest_block = self.w3.eth.block_number
        except Exception as e:
            print(f'RPC failover triggered: {e}')
            self._rotate_rpc()
            latest_block = self.w3.eth.block_number

        safe_to_block = latest_block - self.reorg_safety_depth

        if from_block is None:
            checkpoint_block = self.store.get_last_contiguous_block()
            if checkpoint_block > 0:
                start_block = max(checkpoint_block + 1, safe_to_block - blocks_back)
            else:
                start_block = safe_to_block - blocks_back
        else:
            start_block = from_block

        if start_block >= safe_to_block:
            print('Scan up to date.')
            return [], safe_to_block

        print(f'Scanning Base Blocks {start_block:,} to {safe_to_block:,} (Depth Safety Margin: {self.reorg_safety_depth} blocks)...')

        all_logs = []
        current_from = start_block

        while current_from <= safe_to_block:
            current_to = min(current_from + self.chunk_size - 1, safe_to_block)
            
            filter_params = {
                'fromBlock': current_from,
                'toBlock': current_to,
                'topics': [[
                    UNISWAP_V3_SWAP,
                    UNISWAP_V2_SWAP,
                    BALANCER_V2_SWAP,
                    AERODROME_V2_SWAP
                ]]
            }
            
            chunk_logs = []
            success = False
            for attempt in range(len(self.rpc_list)):
                try:
                    chunk_logs = self.w3.eth.get_logs(filter_params)
                    all_logs.extend(chunk_logs)
                    
                    # Store chunk atomically in SQLite
                    self.store.store_chunk_results(current_from, current_to, chunk_logs)
                    success = True
                    break
                except Exception as e:
                    print(f'  -> RPC Error on chunk {current_from}-{current_to}: {e}. Rotating RPC...')
                    self._rotate_rpc()

            if not success:
                print(f'Warning: Could not fetch chunk {current_from}-{current_to} across all RPCs.')

            current_from = current_to + 1

        total_stored = self.store.get_total_swaps()
        print(f'Scan Complete: Captured {len(all_logs)} multi-DEX swaps across {safe_to_block - start_block} blocks (SQLite Ledger Total: {total_stored:,} events)!')
        return all_logs, safe_to_block

if __name__ == '__main__':
    indexer = ProductionDEXIndexer()
    gas_info = indexer.get_eip1559_gas()
    print('EIP-1559 Gas Forecast:', json.dumps(gas_info, indent=2))
    logs, last_block = indexer.scan_market_events(blocks_back=150)
    print(f'Last Safe Confirmed Block: {last_block:,}')
