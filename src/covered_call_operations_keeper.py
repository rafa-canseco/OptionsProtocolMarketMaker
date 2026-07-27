"""Fail-closed WETH redemption keeper for the B1N-362 covered-call fund."""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from pathlib import Path

from eth_account import Account
from web3 import Web3

from src import config
from src.covered_call_allocator import load_covered_call_policy
from src.fund_operations_keeper import (
    _FLOW_ABI,
    _VAULT_ABI,
    bounded_redeem_shares,
    marginal_exit_cost,
)
from src.fund_tx import ConfirmedTransaction, send_confirmed_transaction

log = logging.getLogger(__name__)


class CoveredCallFundOperationsKeeper:
    def __init__(self) -> None:
        policy_path = Path(config.COVERED_CALL_ALLOCATOR_POLICY_PATH)
        load_covered_call_policy(policy_path)
        self.policy_hash = hashlib.sha256(policy_path.read_bytes()).hexdigest()
        self._validate_runtime_config()
        self.w3 = Web3(Web3.HTTPProvider(config.RPC_URL))
        if self.w3.eth.chain_id != 84532:
            raise RuntimeError("Covered-call operations keeper requires Base Sepolia")
        self.account = Account.from_key(config.COVERED_CALL_PROCESSOR_PRIVATE_KEY)
        self.vault = self.w3.eth.contract(
            address=Web3.to_checksum_address(config.COVERED_CALL_VAULT_ADDRESS),
            abi=_VAULT_ABI,
        )
        self.flow = self.w3.eth.contract(
            address=Web3.to_checksum_address(config.COVERED_CALL_FLOW_MANAGER_ADDRESS),
            abi=_FLOW_ABI,
        )
        for address in (self.vault.address, self.flow.address):
            if not self.w3.eth.get_code(address):
                raise RuntimeError(
                    f"Configured covered-call fund address has no code: {address}"
                )

    @staticmethod
    def _validate_runtime_config() -> None:
        required = {
            "COVERED_CALL_PROCESSOR_PRIVATE_KEY": (
                config.COVERED_CALL_PROCESSOR_PRIVATE_KEY
            ),
            "COVERED_CALL_VAULT_ADDRESS": config.COVERED_CALL_VAULT_ADDRESS,
            "COVERED_CALL_FLOW_MANAGER_ADDRESS": (
                config.COVERED_CALL_FLOW_MANAGER_ADDRESS
            ),
        }
        missing = sorted(name for name, value in required.items() if not value)
        if missing:
            raise RuntimeError(
                f"Missing covered-call operations configuration: {missing}"
            )
        if config.CHAIN_ID != 84532:
            raise RuntimeError("Covered-call operations keeper requires CHAIN_ID=84532")
        environment = config._current_environment()
        if environment and environment not in {"staging", "development", "test"}:
            raise RuntimeError("Covered-call operations keeper is non-production only")
        if (
            config.COVERED_CALL_ALLOCATOR_ENABLED
            and config.COVERED_CALL_ALLOCATOR_PRIVATE_KEY
            and Account.from_key(config.COVERED_CALL_ALLOCATOR_PRIVATE_KEY).address
            == Account.from_key(config.COVERED_CALL_PROCESSOR_PRIVATE_KEY).address
        ):
            raise RuntimeError(
                "Covered-call allocator and processor require separate keys"
            )

    def _safe_block(self) -> tuple[int, int]:
        latest = self.w3.eth.block_number
        safe = max(
            latest - config.COVERED_CALL_ALLOCATOR_CONFIRMATIONS,
            0,
        )
        return safe, latest

    def _send(self, function) -> ConfirmedTransaction:
        return send_confirmed_transaction(
            w3=self.w3,
            account=self.account,
            function=function,
            chain_id=84532,
            confirmations=config.COVERED_CALL_ALLOCATOR_CONFIRMATIONS,
        )

    def run_once(self) -> None:
        safe_block, latest_block = self._safe_block()
        batch_id = self.flow.functions.nextProcessBatchId().call(
            block_identifier=safe_block
        )
        batch = self.flow.functions.batch(batch_id).call(block_identifier=safe_block)
        if batch[19]:
            tx = self._send(
                self.flow.functions.processRedeemBatch(
                    batch_id,
                    config.COVERED_CALL_OPERATIONS_KEEPER_PAGE_SIZE,
                )
            )
            log.info(
                "Covered-call operations decision=process batch_id=%d "
                "policy_hash=%s tx=%s tx_nonce=%d replaced=%s",
                batch_id,
                self.policy_hash,
                tx.tx_hash,
                tx.nonce,
                tx.replaced,
            )
            return

        pending_shares = int(batch[0])
        if pending_shares == 0:
            return
        if not batch[18]:
            open_batch_id = self.flow.functions.openBatchId().call(
                block_identifier=safe_block
            )
            if open_batch_id != batch_id:
                raise RuntimeError("Next redemption batch is neither sealed nor open")
            tx = self._send(self.flow.functions.sealRedeemBatch(batch_id))
            log.info(
                "Covered-call operations decision=seal batch_id=%d shares=%d "
                "policy_hash=%s tx=%s tx_nonce=%d replaced=%s",
                batch_id,
                pending_shares,
                self.policy_hash,
                tx.tx_hash,
                tx.nonce,
                tx.replaced,
            )
            return

        nav = self.vault.functions.activeNavWindow().call(block_identifier=safe_block)
        if not nav[6] <= latest_block <= nav[7]:
            log.info(
                "Covered-call operations decision=skip reason=nav_not_active "
                "batch_id=%d report_nonce=%d",
                batch_id,
                nav[9],
            )
            return
        eligible_supply = self.vault.functions.shareSupply().call(
            block_identifier=safe_block
        )
        idle_weth = self.vault.functions.accountedIdleAssets().call(
            block_identifier=safe_block
        )
        virtual_shares = self.vault.functions.virtualShares().call(
            block_identifier=safe_block
        )
        _, max_window_outflow_bps = self.flow.functions.exitPolicy().call(
            block_identifier=safe_block
        )
        window_eligible_supply, window_processed = self.flow.functions.windowOutflow(
            nav[9]
        ).call(block_identifier=safe_block)
        shares = bounded_redeem_shares(
            pending_shares=pending_shares,
            idle_assets=idle_weth,
            net_assets=nav[2],
            eligible_supply=eligible_supply,
            virtual_shares=virtual_shares,
            max_window_outflow_bps=max_window_outflow_bps,
            window_eligible_supply=window_eligible_supply,
            window_processed_shares=window_processed,
        )
        if shares == 0:
            log.info(
                "Covered-call operations decision=skip "
                "reason=awaiting_weth_from_strategy batch_id=%d shares=%d",
                batch_id,
                pending_shares,
            )
            return
        exit_cost = marginal_exit_cost(nav[4], shares, eligible_supply)
        tx = self._send(
            self.flow.functions.startRedeemBatch(batch_id, shares, exit_cost)
        )
        log.info(
            "Covered-call operations decision=start batch_id=%d shares=%d "
            "idle_weth=%d policy_hash=%s report_nonce=%d report_hash=%s "
            "tx=%s tx_nonce=%d replaced=%s",
            batch_id,
            shares,
            idle_weth,
            self.policy_hash,
            nav[9],
            Web3.to_hex(nav[11]),
            tx.tx_hash,
            tx.nonce,
            tx.replaced,
        )

    def run_forever(self) -> None:
        log.info(
            "Covered-call operations keeper enabled address=%s interval=%ds",
            self.account.address,
            config.COVERED_CALL_OPERATIONS_KEEPER_INTERVAL_SECONDS,
        )
        while True:
            try:
                self.run_once()
            except Exception:
                log.warning(
                    "Covered-call operations cycle failed closed",
                    exc_info=True,
                )
            time.sleep(config.COVERED_CALL_OPERATIONS_KEEPER_INTERVAL_SECONDS)


def start() -> threading.Thread | None:
    if not config.COVERED_CALL_OPERATIONS_KEEPER_ENABLED:
        log.info("Covered-call operations keeper disabled")
        return None
    keeper = CoveredCallFundOperationsKeeper()
    thread = threading.Thread(
        target=keeper.run_forever,
        name="covered-call-fund-operations-keeper",
        daemon=True,
    )
    thread.start()
    return thread
