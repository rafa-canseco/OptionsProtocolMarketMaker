from __future__ import annotations

import inspect

from src import (
    covered_call_allocator,
    covered_call_operations_keeper,
    fund_allocator,
    fund_operations_keeper,
    main,
    meta_wheel_allocator,
)
from src.meta_wheel_runtime import BaseSepoliaMetaWheelRuntime


def test_all_recurrent_worker_roots_have_no_provider_reads_or_construction():
    recurrent_roots = (
        fund_allocator.CspFundAllocator.run_once,
        fund_allocator.CspFundAllocator._open,
        fund_allocator.CspFundAllocator._settle,
        covered_call_allocator.CoveredCallFundAllocator.run_once,
        covered_call_allocator.CoveredCallFundAllocator._open,
        covered_call_allocator.CoveredCallFundAllocator._settle_or_normalize,
        fund_operations_keeper.CspFundOperationsKeeper.run_once,
        covered_call_operations_keeper.CoveredCallFundOperationsKeeper.run_once,
        meta_wheel_allocator.MetaWheelAllocator.run_once,
        BaseSepoliaMetaWheelRuntime.read_consumed_snapshot,
        BaseSepoliaMetaWheelRuntime.list_consumed_quotes,
        BaseSepoliaMetaWheelRuntime._observed_state_from_snapshot,
        BaseSepoliaMetaWheelRuntime.reconcile,
        main.run_cycle,
        main._run_asset_cycle,
        main._quote_and_submit,
    )
    for root in recurrent_roots:
        source = inspect.getsource(root)
        assert "HTTPProvider" not in source, root.__qualname__
        assert ".call(" not in source, root.__qualname__
        assert ".eth.call(" not in source, root.__qualname__
