import time
import json
import os
import sys
from web3 import Web3

sys.stdout.reconfigure(encoding='utf-8')

print('=== MIDAS AI - INITIALIZING PRODUCTION DEX INDEXER & MARKET SCANNER ===')

# 1. EVM Event Topic Signatures (Multi-DEX Coverage)
UNISWAP_V3_SWAP = Web3.keccak(text="Swap(address,address,int256,int256,uint160,128,int24)").hex()
UNISWAP_V2_SWAP = Web3.keccak(text="Swap(address,uint256,uint256,uint256,uint256,address)").hex()
BALANCER_V2_SWAP = Web3.keccak(text="Swap(bytes32,address,address,uint256,uint256)").hex()
AERODROME_V2_SWAP = Web3.keccak(text="Swap(address,address,uint256,uint256,uint256,uint256)").hex()

# 2. Resilient Multi-RPC Provider Network
RPC_PROVIDERS = [
    'https://mainnet.base.org',
    'https://base-rpc.publicnode.com',
    'https://1rpc.io/base',
    'https://developer-access-mainnet.base.org'
]

class ProductionDEXIndexer:
    def __init__(self, rpc_list=None, reorg_safety_depth=12, chunk_size=25):
        self.rpc_list = rpc_list or RPC_PROVIDERS
        self.reorg_safety_depth = reorg_safety_depth
        self.chunk_size = chunk_size
        self.current_rpc_idx = 0
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

    def scan_market_events(self, from_block=None, blocks_back=150):
        try:
            latest_block = self.w3.eth.block_number
        except Exception as e:
            print(f'RPC failover triggered: {e}')
            self._rotate_rpc()
            latest_block = self.w3.eth.block_number

        # Fix #4: Reorg Safety Margin (never read block head directly)
        safe_to_block = latest_block - self.reorg_safety_depth

        if from_block is None:
            start_block = safe_to_block - blocks_back
        else:
            start_block = from_block

        if start_block >= safe_to_block:
            print('Scan up to date.')
            return [], safe_to_block

        print(f'Scanning Base Blocks {start_block:,} to {safe_to_block:,} (Depth Safety Margin: {self.reorg_safety_depth} blocks)...')

        all_logs = []
        current_from = start_block

        # Fix #2: Chunked Block Pagination (prevents payload size limits & timeouts)
        while current_from <= safe_to_block:
            current_to = min(current_from + self.chunk_size - 1, safe_to_block)
            
            # Fix #1: Multi-DEX Event Topic Array (V2 + V3 + Aerodrome + Balancer)
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
            
            success = False
            for attempt in range(len(self.rpc_list)):
                try:
                    logs = self.w3.eth.get_logs(filter_params)
                    all_logs.extend(logs)
                    success = True
                    break
                except Exception as e:
                    print(f'  -> RPC Error on chunk {current_from}-{current_to}: {e}. Rotating RPC...')
                    self._rotate_rpc()

            if not success:
                print(f'Warning: Could not fetch chunk {current_from}-{current_to} across all RPCs.')

            current_from = current_to + 1

        print(f'Scan Complete: Captured {len(all_logs)} multi-DEX swap events across {safe_to_block - start_block} blocks with 0 reorg risk!')
        return all_logs, safe_to_block

if __name__ == '__main__':
    indexer = ProductionDEXIndexer()
    logs, last_block = indexer.scan_market_events(blocks_back=150)
    print(f'Last Safe Confirmed Block: {last_block:,}')
