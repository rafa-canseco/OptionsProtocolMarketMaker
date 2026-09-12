"""Fail-closed accounting-asset redemption keeper for the CSP fund."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from eth_account import Account
from web3 import Web3

from src import config
from src.fund_tx import ConfirmedTransaction, send_confirmed_transaction
from src.snapshot_consumer import SnapshotBundle, SnapshotConsumer, supervise_worker

log = logging.getLogger(__name__)

BPS = 10_000
VIRTUAL_ASSETS = 1
MAX_PROCESSING_PAGE = 16

_VAULT_ABI = [
    {
        "name": "activeNavWindow",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [
            {
                "type": "tuple",
                "components": [
                    {"name": "grossAssets", "type": "uint256"},
                    {"name": "liabilities", "type": "uint256"},
                    {"name": "netAssets", "type": "uint256"},
                    {"name": "liquidAccountingAssets", "type": "uint256"},
                    {"name": "baseExitCost", "type": "uint256"},
                    {"name": "snapshotBlock", "type": "uint64"},
                    {"name": "validAfterBlock", "type": "uint64"},
                    {"name": "validUntilBlock", "type": "uint64"},
                    {"name": "reporterSetVersion", "type": "uint64"},
                    {"name": "reportNonce", "type": "uint64"},
                    {"name": "positionsHash", "type": "bytes32"},
                    {"name": "reportHash", "type": "bytes32"},
                    {"name": "signaturesHash", "type": "bytes32"},
                    {"name": "fundFlowNonce", "type": "uint64"},
                    {"name": "idleStateHash", "type": "bytes32"},
                ],
            }
        ],
    },
    {
        "name": "accountedIdleAssets",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint256"}],
    },
    {
        "name": "shareSupply",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint256"}],
    },
    {
        "name": "virtualShares",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint256"}],
    },
]

_FLOW_ABI = [
    {
        "name": "totalPendingShares",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint256"}],
    },
    {
        "name": "nextProcessBatchId",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint64"}],
    },
    {
        "name": "openBatchId",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"type": "uint64"}],
    },
    {
        "name": "exitPolicy",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [
            {"name": "maxExitFeeBps", "type": "uint16"},
            {"name": "maxWindowOutflowBps", "type": "uint16"},
        ],
    },
    {
        "name": "windowOutflow",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"type": "uint64"}],
        "outputs": [
            {"name": "eligibleSupply", "type": "uint256"},
            {"name": "processedShares", "type": "uint256"},
        ],
    },
    {
        "name": "batch",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"type": "uint64"}],
        "outputs": [
            {
                "type": "tuple",
                "components": [
                    {"name": "totalPendingShares", "type": "uint256"},
                    {"name": "processedShares", "type": "uint256"},
                    {"name": "reservedAssets", "type": "uint256"},
                    {"name": "marginalExitCost", "type": "uint256"},
                    {"name": "processingNav", "type": "uint256"},
                    {"name": "eligibleSupply", "type": "uint256"},
                    {"name": "roundPendingShares", "type": "uint256"},
                    {"name": "roundTargetShares", "type": "uint256"},
                    {"name": "roundCumulativeShares", "type": "uint256"},
                    {"name": "roundAllocatedShares", "type": "uint256"},
                    {"name": "roundAssetBudget", "type": "uint256"},
                    {"name": "roundAllocatedAssets", "type": "uint256"},
                    {"name": "processingPositionsHash", "type": "bytes32"},
                    {"name": "processingBlock", "type": "uint64"},
                    {"name": "processingReportNonce", "type": "uint64"},
                    {"name": "processingValidUntilBlock", "type": "uint64"},
                    {"name": "processingCursor", "type": "uint16"},
                    {"name": "mode", "type": "uint8"},
                    {"name": "isSealed", "type": "bool"},
                    {"name": "processing", "type": "bool"},
                    {"name": "unwindCommitted", "type": "bool"},
                    {"name": "isReleased", "type": "bool"},
                ],
            }
        ],
    },
    {
        "name": "sealRedeemBatch",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"type": "uint64"}],
        "outputs": [],
    },
    {
        "name": "startRedeemBatch",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"type": "uint64"},
            {"type": "uint256"},
            {"type": "uint256"},
        ],
        "outputs": [],
    },
    {
        "name": "processRedeemBatch",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"type": "uint64"}, {"type": "uint16"}],
        "outputs": [
            {"type": "uint16"},
            {"type": "bool"},
        ],
    },
]


def bounded_redeem_shares(
    *,
    pending_shares: int,
    idle_assets: int,
    net_assets: int,
    eligible_supply: int,
    virtual_shares: int,
    max_window_outflow_bps: int,
    window_eligible_supply: int,
    window_processed_shares: int,
) -> int:
    """Return a conservative redemption size backed by current idle USDC."""
    if (
        pending_shares <= 0
        or idle_assets <= 0
        or net_assets <= 0
        or eligible_supply <= 0
        or virtual_shares <= 0
        or not 0 < max_window_outflow_bps <= BPS
    ):
        return 0

    window_supply = window_eligible_supply or eligible_supply
    window_cap = window_supply * max_window_outflow_bps // BPS
    if window_processed_shares >= window_cap:
        return 0
    window_remaining = window_cap - window_processed_shares

    # FundMath.redemptionPayout floors gross assets. Limiting shares using the
    # pre-cost gross ratio is conservative because exit costs/fees only reduce
    # the accounting assets that the vault must reserve.
    liquidity_cap = (
        idle_assets
        * (eligible_supply + virtual_shares)
        // (net_assets + VIRTUAL_ASSETS)
    )
    return min(pending_shares, window_remaining, liquidity_cap)


def marginal_exit_cost(base_exit_cost: int, shares: int, eligible_supply: int) -> int:
    if base_exit_cost <= 0 or shares <= 0:
        return 0
    if eligible_supply <= 0:
        raise ValueError("eligible_supply must be positive")
    return (base_exit_cost * shares + eligible_supply - 1) // eligible_supply


class CspFundOperationsKeeper:
    def __init__(self, snapshots: SnapshotConsumer, transaction_w3: Web3) -> None:
        self._validate_runtime_config()
        self.snapshots = snapshots
        self.w3 = transaction_w3
        self.account = Account.from_key(config.FUND_PROCESSOR_PRIVATE_KEY)
        self.vault = self.w3.eth.contract(
            address=Web3.to_checksum_address(config.FUND_VAULT_ADDRESS),
            abi=_VAULT_ABI,
        )
        self.flow = self.w3.eth.contract(
            address=Web3.to_checksum_address(config.FUND_FLOW_MANAGER_ADDRESS),
            abi=_FLOW_ABI,
        )

    @staticmethod
    def _validate_runtime_config() -> None:
        required = {
            "FUND_PROCESSOR_PRIVATE_KEY": config.FUND_PROCESSOR_PRIVATE_KEY,
            "FUND_VAULT_ADDRESS": config.FUND_VAULT_ADDRESS,
            "FUND_FLOW_MANAGER_ADDRESS": config.FUND_FLOW_MANAGER_ADDRESS,
        }
        missing = sorted(name for name, value in required.items() if not value)
        if missing:
            raise RuntimeError(f"Missing fund operations configuration: {missing}")
        if config.CHAIN_ID != 84532:
            raise RuntimeError("Fund operations keeper requires CHAIN_ID=84532")
        environment = config._current_environment()
        if environment and environment not in {"staging", "development", "test"}:
            raise RuntimeError(
                "Fund operations keeper may only run in a non-production environment"
            )
        if (
            config.FUND_ALLOCATOR_ENABLED
            and config.FUND_ALLOCATOR_PRIVATE_KEY
            and Account.from_key(config.FUND_ALLOCATOR_PRIVATE_KEY).address
            == Account.from_key(config.FUND_PROCESSOR_PRIVATE_KEY).address
        ):
            raise RuntimeError(
                "Allocator and fund operations keeper require separate configured keys"
            )

    def _send(self, function: Any, bundle: SnapshotBundle) -> ConfirmedTransaction:
        self.snapshots.require(bundle)
        tx = send_confirmed_transaction(
            w3=self.w3,
            account=self.account,
            function=function,
            chain_id=84532,
            confirmations=config.FUND_ALLOCATOR_CONFIRMATIONS,
            decision_validator=lambda: self.snapshots.require(bundle),
        )
        self.snapshots.wait_after_receipt(
            pre_send_generation=bundle.generation,
            receipt_block=tx.block_number,
            receipt_block_hash=tx.block_hash,
        )
        return tx

    def run_once(self) -> None:
        bundle = self.snapshots.current()
        state = bundle.fund(
            "csp",
            "operations",
            expected_address=config.FUND_VAULT_ADDRESS,
        )
        latest_block = int(state["latest_block"])
        batch_id = int(state["batch_id"])
        batch = state["batch"]

        if batch[19]:
            tx = self._send(
                self.flow.functions.processRedeemBatch(
                    batch_id,
                    config.FUND_OPERATIONS_KEEPER_PAGE_SIZE,
                ),
                bundle,
            )
            log.info(
                "Fund operations decision=process batch_id=%d tx=%s",
                batch_id,
                tx.tx_hash,
            )
            return

        pending_shares = batch[0]
        if pending_shares == 0:
            return

        if not batch[18]:
            open_batch_id = int(state["open_batch_id"])
            if open_batch_id != batch_id:
                raise RuntimeError("Next redemption batch is neither sealed nor open")
            tx = self._send(self.flow.functions.sealRedeemBatch(batch_id), bundle)
            log.info(
                "Fund operations decision=seal batch_id=%d shares=%d tx=%s",
                batch_id,
                pending_shares,
                tx.tx_hash,
            )
            return

        nav = state["nav"]
        if not nav[6] <= latest_block <= nav[7]:
            log.info(
                "Fund operations decision=skip reason=nav_not_active "
                "batch_id=%d report_nonce=%d",
                batch_id,
                nav[9],
            )
            return

        eligible_supply = int(state["eligible_supply"])
        idle_assets = int(state["idle_assets"])
        virtual_shares = int(state["virtual_shares"])
        max_window_outflow_bps = int(state["max_window_outflow_bps"])
        window_eligible_supply = int(state["window_eligible_supply"])
        window_processed = int(state["window_processed_shares"])
        shares = bounded_redeem_shares(
            pending_shares=pending_shares,
            idle_assets=idle_assets,
            net_assets=nav[2],
            eligible_supply=eligible_supply,
            virtual_shares=virtual_shares,
            max_window_outflow_bps=max_window_outflow_bps,
            window_eligible_supply=window_eligible_supply,
            window_processed_shares=window_processed,
        )
        if shares == 0:
            log.info(
                "Fund operations decision=skip reason=awaiting_strategy_liquidity "
                "batch_id=%d pending_shares=%d idle_assets=%d",
                batch_id,
                pending_shares,
                idle_assets,
            )
            return

        exit_cost = marginal_exit_cost(nav[4], shares, eligible_supply)
        tx = self._send(
            self.flow.functions.startRedeemBatch(batch_id, shares, exit_cost), bundle
        )
        log.info(
            "Fund operations decision=start batch_id=%d shares=%d "
            "idle_assets=%d report_nonce=%d report_hash=%s tx=%s",
            batch_id,
            shares,
            idle_assets,
            nav[9],
            Web3.to_hex(nav[11]),
            tx.tx_hash,
        )

    def run_forever(self) -> None:
        log.info(
            "Fund operations keeper enabled: address=%s interval=%ds",
            self.account.address,
            config.FUND_OPERATIONS_KEEPER_INTERVAL_SECONDS,
        )
        while True:
            try:
                self.run_once()
            except Exception:
                log.warning("Fund operations cycle failed closed", exc_info=True)
            time.sleep(config.FUND_OPERATIONS_KEEPER_INTERVAL_SECONDS)


def start(snapshots: SnapshotConsumer, transaction_w3: Web3) -> threading.Thread | None:
    if not config.V2_SNAPSHOT_ENABLED or not config.FUND_OPERATIONS_KEEPER_ENABLED:
        log.info("Fund operations keeper disabled")
        return None
    thread = threading.Thread(
        target=supervise_worker,
        args=(
            "csp-fund-operations-keeper",
            lambda: CspFundOperationsKeeper(snapshots, transaction_w3),
        ),
        name="csp-fund-operations-keeper",
        daemon=True,
    )
    thread.start()
    return thread
