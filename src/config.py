import os
import sys
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    val = os.getenv(name)
    if not val:
        print(f"FATAL: missing required env var {name}", file=sys.stderr)
        sys.exit(1)
    return val


_FLAG_TRUE_VALUES = frozenset({"true", "1", "yes", "on"})
_FLAG_FALSE_VALUES = frozenset({"false", "0", "no", "off"})


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value == "":
        return default
    if value in _FLAG_TRUE_VALUES:
        return True
    if value in _FLAG_FALSE_VALUES:
        return False
    print(
        f"FATAL: {name}={raw!r} is not a valid boolean. "
        f"Use one of {sorted(_FLAG_TRUE_VALUES)} "
        f"or {sorted(_FLAG_FALSE_VALUES)}.",
        file=sys.stderr,
    )
    sys.exit(1)


def _optional_env(name: str) -> str | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def _current_environment() -> str:
    for name in (
        "APP_ENV",
        "ENVIRONMENT",
        "RAILWAY_ENVIRONMENT_NAME",
        "RAILWAY_ENVIRONMENT",
    ):
        value = _optional_env(name)
        if value:
            return value.lower()
    return ""


def _solana_quote_publishing_enabled() -> bool:
    explicit = _optional_env("SOLANA_QUOTE_PUBLISHING_ENABLED")
    if explicit is not None:
        return _env_flag("SOLANA_QUOTE_PUBLISHING_ENABLED")

    # Backward-compatible default: non-production environments keep
    # legacy Solana enablement based on configured credentials.
    if _current_environment() == "production":
        return False
    return _optional_env("SOLANA_PRIVATE_KEY") is not None


@dataclass(frozen=True)
class AssetConfig:
    name: str  # lowercase, e.g. "eth"
    hedge_symbol: str  # Hyperliquid symbol, e.g. "ETH"
    leverage: int
    max_exposure: float  # 0.0–1.0, fraction of total capital
    hedge_enabled: bool = True


@dataclass(frozen=True)
class ChainConfig:
    name: str  # "base" | "solana"
    assets: tuple[AssetConfig, ...]


# --- Required ---
MM_PRIVATE_KEY: str = _require("MM_PRIVATE_KEY")
MM_API_KEY: str = _require("MM_API_KEY")
_backend_raw = _require("BACKEND_URL").rstrip("/")
if not _backend_raw.startswith(("http://", "https://")):
    _backend_raw = f"https://{_backend_raw}"
BACKEND_URL: str = _backend_raw
RPC_URL: str = _require("RPC_URL")

# --- Optional with defaults ---
REFRESH_INTERVAL: int = int(os.getenv("REFRESH_INTERVAL", "60"))
REFRESH_INTERVAL_FAST: int = max(int(os.getenv("REFRESH_INTERVAL_FAST", "30")), 5)
FAST_REFRESH_HOURS: int = max(int(os.getenv("FAST_REFRESH_HOURS", "6")), 1)
SPREAD_BPS: int = int(os.getenv("SPREAD_BPS", "200"))
MAX_AMOUNT: int = int(os.getenv("MAX_AMOUNT", "500000000"))
DEADLINE_SECONDS: int = int(os.getenv("DEADLINE_SECONDS", "300"))
CHAIN_ID: int = int(os.getenv("CHAIN_ID", "84532"))
BATCH_SETTLER: str = os.getenv(
    "BATCH_SETTLER",
    "0x3B5d4640233E14cc330A749926838ba2C540054f",
)
RISK_FREE_RATE: float = float(os.getenv("RISK_FREE_RATE", "0.05"))

# --- Hedging ---
HEDGE_MODE: str = os.getenv("HEDGE_MODE", "simulate")  # simulate | live
HYPERLIQUID_TESTNET: bool = os.getenv("HYPERLIQUID_TESTNET", "true").lower() in (
    "true",
    "1",
    "yes",
)
HEDGE_SLIPPAGE: float = float(os.getenv("HEDGE_SLIPPAGE", "0.01"))

# --- Capacity ---
MM_TYPE: str = os.getenv("MM_TYPE", "internal")  # internal | external
CAPACITY_RESERVE_RATIO: float = float(os.getenv("CAPACITY_RESERVE_RATIO", "0.25"))
CAPACITY_PREMIUM_RATIO: float = float(os.getenv("CAPACITY_PREMIUM_RATIO", "0.03"))
CAPACITY_AVG_DELTA: float = float(os.getenv("CAPACITY_AVG_DELTA", "0.3"))
USDC_ADDRESS: str = os.getenv(
    "USDC_ADDRESS",
    "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # Base mainnet USDC
)
MARGIN_POOL_ADDRESS: str = os.getenv(
    "MARGIN_POOL_ADDRESS",
    "0xa1e04873F6d112d84824C88c9D6937bE38811657",  # Base mainnet MarginPool
)

# --- Trade history persistence ---
TRADE_LOG_PATH: str = os.getenv("TRADE_LOG_PATH", "data/trade_history.jsonl")
SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")

# --- Base Sepolia tokenized CSP fund allocator (explicit opt-in) ---
FUND_ALLOCATOR_ENABLED: bool = _env_flag("FUND_ALLOCATOR_ENABLED", default=False)
FUND_ALLOCATOR_PRIVATE_KEY: str | None = _optional_env("FUND_ALLOCATOR_PRIVATE_KEY")
FUND_ALLOCATOR_POLICY_PATH: str = os.getenv(
    "FUND_ALLOCATOR_POLICY_PATH",
    "policies/csp_fund_policy.v3.base-sepolia.json",
)
FUND_ALLOCATOR_INTERVAL_SECONDS: int = max(
    int(os.getenv("FUND_ALLOCATOR_INTERVAL_SECONDS", "30")),
    10,
)
FUND_ALLOCATOR_CONFIRMATIONS: int = max(
    int(os.getenv("FUND_ALLOCATOR_CONFIRMATIONS", "2")),
    1,
)
FUND_OPERATIONS_KEEPER_ENABLED: bool = _env_flag(
    "FUND_OPERATIONS_KEEPER_ENABLED",
    default=False,
)
FUND_PROCESSOR_PRIVATE_KEY: str | None = _optional_env(
    "FUND_PROCESSOR_PRIVATE_KEY"
) or _optional_env(
    # Backward-compatible alias for any pre-release local configuration.
    "FUND_OPERATIONS_KEEPER_PRIVATE_KEY"
)
FUND_OPERATIONS_KEEPER_INTERVAL_SECONDS: int = max(
    int(os.getenv("FUND_OPERATIONS_KEEPER_INTERVAL_SECONDS", "30")),
    10,
)
FUND_OPERATIONS_KEEPER_PAGE_SIZE: int = min(
    max(int(os.getenv("FUND_OPERATIONS_KEEPER_PAGE_SIZE", "16")), 1),
    16,
)
FUND_VAULT_ADDRESS: str | None = _optional_env("FUND_VAULT_ADDRESS")
FUND_FLOW_MANAGER_ADDRESS: str | None = _optional_env("FUND_FLOW_MANAGER_ADDRESS")
FUND_STRATEGY_MANAGER_ADDRESS: str | None = _optional_env(
    "FUND_STRATEGY_MANAGER_ADDRESS"
)
FUND_CSP_ADAPTER_ADDRESS: str | None = _optional_env("FUND_CSP_ADAPTER_ADDRESS")
FUND_CSP_VALUATOR_ADDRESS: str | None = _optional_env("FUND_CSP_VALUATOR_ADDRESS")

# --- Base Sepolia tokenized Covered Call fund (explicit opt-in) ---
COVERED_CALL_ALLOCATOR_ENABLED: bool = _env_flag(
    "COVERED_CALL_ALLOCATOR_ENABLED",
    default=False,
)
COVERED_CALL_ALLOCATOR_PRIVATE_KEY: str | None = _optional_env(
    "COVERED_CALL_ALLOCATOR_PRIVATE_KEY"
)
COVERED_CALL_ALLOCATOR_POLICY_PATH: str = os.getenv(
    "COVERED_CALL_ALLOCATOR_POLICY_PATH",
    "policies/covered_call_fund_policy.v3.base-sepolia.json",
)
COVERED_CALL_ALLOCATOR_INTERVAL_SECONDS: int = max(
    int(os.getenv("COVERED_CALL_ALLOCATOR_INTERVAL_SECONDS", "30")),
    10,
)
COVERED_CALL_ALLOCATOR_CONFIRMATIONS: int = max(
    int(os.getenv("COVERED_CALL_ALLOCATOR_CONFIRMATIONS", "2")),
    1,
)
COVERED_CALL_OPERATIONS_KEEPER_ENABLED: bool = _env_flag(
    "COVERED_CALL_OPERATIONS_KEEPER_ENABLED",
    default=False,
)
COVERED_CALL_PROCESSOR_PRIVATE_KEY: str | None = _optional_env(
    "COVERED_CALL_PROCESSOR_PRIVATE_KEY"
)
COVERED_CALL_OPERATIONS_KEEPER_INTERVAL_SECONDS: int = max(
    int(os.getenv("COVERED_CALL_OPERATIONS_KEEPER_INTERVAL_SECONDS", "30")),
    10,
)
COVERED_CALL_OPERATIONS_KEEPER_PAGE_SIZE: int = min(
    max(int(os.getenv("COVERED_CALL_OPERATIONS_KEEPER_PAGE_SIZE", "16")), 1),
    16,
)
COVERED_CALL_VAULT_ADDRESS: str | None = _optional_env("COVERED_CALL_VAULT_ADDRESS")
COVERED_CALL_FLOW_MANAGER_ADDRESS: str | None = _optional_env(
    "COVERED_CALL_FLOW_MANAGER_ADDRESS"
)
COVERED_CALL_STRATEGY_MANAGER_ADDRESS: str | None = _optional_env(
    "COVERED_CALL_STRATEGY_MANAGER_ADDRESS"
)
COVERED_CALL_ADAPTER_ADDRESS: str | None = _optional_env("COVERED_CALL_ADAPTER_ADDRESS")
COVERED_CALL_VALUATOR_ADDRESS: str | None = _optional_env(
    "COVERED_CALL_VALUATOR_ADDRESS"
)
COVERED_CALL_WETH_ADDRESS: str | None = _optional_env("COVERED_CALL_WETH_ADDRESS")


# --- Multi-asset configuration ---
def _parse_assets() -> list[AssetConfig]:
    raw = os.getenv("ASSETS", "eth")
    assets = []
    for name in raw.split(","):
        name = name.strip().lower()
        if not name:
            continue
        prefix = name.upper()
        leverage = int(os.getenv(f"{prefix}_HEDGE_LEVERAGE", "3"))
        if leverage < 1:
            print(
                f"FATAL: {prefix}_HEDGE_LEVERAGE must be >= 1, got {leverage}",
                file=sys.stderr,
            )
            sys.exit(1)
        max_exp = float(os.getenv(f"{prefix}_MAX_EXPOSURE", "1.0"))
        if not 0.0 < max_exp <= 1.0:
            print(
                f"FATAL: {prefix}_MAX_EXPOSURE must be in (0, 1], got {max_exp}",
                file=sys.stderr,
            )
            sys.exit(1)
        assets.append(
            AssetConfig(
                name=name,
                hedge_symbol=os.getenv(f"{prefix}_HEDGE_SYMBOL", name.upper()),
                leverage=leverage,
                max_exposure=max_exp,
                hedge_enabled=_env_flag(f"{prefix}_HEDGE_ENABLED", default=True),
            )
        )
    return assets


ASSETS: list[AssetConfig] = _parse_assets()
if not ASSETS:
    print("FATAL: no assets configured (check ASSETS env var)", file=sys.stderr)
    sys.exit(1)
ASSET_MAP: dict[str, AssetConfig] = {a.name: a for a in ASSETS}

# --- Solana quote publication (explicitly gated per environment) ---
SOLANA_QUOTE_PUBLISHING_ENABLED: bool = _solana_quote_publishing_enabled()
SOLANA_PRIVATE_KEY: str | None = os.getenv("SOLANA_PRIVATE_KEY")
SOLANA_RPC_URL: str | None = os.getenv("SOLANA_RPC_URL")
SOLANA_BATCH_SETTLER: str = os.getenv(
    "SOLANA_BATCH_SETTLER",
    "GpR6id2cHu5fUGsFm7NUKkB4NzfuEDa6brPzkSrgAzvS",  # devnet
)
SOLANA_USDC_MINT: str | None = os.getenv("SOLANA_USDC_MINT")
SOLANA_TSLAX_MINT: str | None = os.getenv("SOLANA_TSLAX_MINT")
SOLANA_TSLAX_PYTH_FEED: str | None = os.getenv("SOLANA_TSLAX_PYTH_FEED")


def _parse_solana_assets() -> list[AssetConfig]:
    raw = os.getenv("SOLANA_ASSETS", "sol")
    assets = []
    for name in raw.split(","):
        name = name.strip().lower()
        if not name:
            continue
        prefix = name.upper()
        leverage = int(os.getenv(f"{prefix}_HEDGE_LEVERAGE", "3"))
        if leverage < 1:
            print(
                f"FATAL: {prefix}_HEDGE_LEVERAGE must be >= 1, got {leverage}",
                file=sys.stderr,
            )
            sys.exit(1)
        max_exp = float(os.getenv(f"{prefix}_MAX_EXPOSURE", "1.0"))
        if not 0.0 < max_exp <= 1.0:
            print(
                f"FATAL: {prefix}_MAX_EXPOSURE must be in (0, 1], got {max_exp}",
                file=sys.stderr,
            )
            sys.exit(1)
        assets.append(
            AssetConfig(
                name=name,
                hedge_symbol=os.getenv(f"{prefix}_HEDGE_SYMBOL", name.upper()),
                leverage=leverage,
                max_exposure=max_exp,
                hedge_enabled=_env_flag(f"{prefix}_HEDGE_ENABLED", default=True),
            )
        )
    return assets


SOLANA_ASSETS: list[AssetConfig] = (
    _parse_solana_assets() if SOLANA_QUOTE_PUBLISHING_ENABLED else []
)
SOLANA_ASSET_MAP: dict[str, AssetConfig] = {a.name: a for a in SOLANA_ASSETS}

# --- Chain configs ---
CHAINS: list[ChainConfig] = [ChainConfig(name="base", assets=tuple(ASSETS))]
if SOLANA_QUOTE_PUBLISHING_ENABLED:
    if not SOLANA_PRIVATE_KEY:
        print(
            "FATAL: SOLANA_PRIVATE_KEY required when "
            "SOLANA_QUOTE_PUBLISHING_ENABLED is true",
            file=sys.stderr,
        )
        sys.exit(1)
    if not SOLANA_RPC_URL:
        print(
            "FATAL: SOLANA_RPC_URL required when "
            "SOLANA_QUOTE_PUBLISHING_ENABLED is true",
            file=sys.stderr,
        )
        sys.exit(1)
    CHAINS.append(ChainConfig(name="solana", assets=tuple(SOLANA_ASSETS)))
