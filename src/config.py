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


@dataclass(frozen=True)
class AssetConfig:
    name: str  # lowercase, e.g. "eth"
    hedge_symbol: str  # Hyperliquid symbol, e.g. "ETH"
    leverage: int
    max_exposure: float  # 0.0–1.0, fraction of total capital


@dataclass(frozen=True)
class EvmChainConfig:
    """Per-EVM-chain settings (RPC, contract addresses)."""

    name: str
    chain_id: int
    rpc_url: str
    batch_settler: str
    usdc_address: str
    margin_pool_address: str


@dataclass(frozen=True)
class ChainConfig:
    name: str  # "base" | "solana" | "xlayer"
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

# --- XLayer (optional — enabled when XLAYER_RPC_URL is set) ---
XLAYER_RPC_URL: str | None = os.getenv("XLAYER_RPC_URL")
XLAYER_CHAIN_ID: int = int(os.getenv("XLAYER_CHAIN_ID", "1952"))
XLAYER_BATCH_SETTLER: str = os.getenv(
    "XLAYER_BATCH_SETTLER",
    "0x6aea5B95d64962E7F001218159cB5fb11712E8B1",
)
XLAYER_USDC_ADDRESS: str = os.getenv(
    "XLAYER_USDC_ADDRESS",
    "0x4A881f3f745B99f0C5575577D80958a5a16b7347",  # MockUSDC on XLayer testnet
)
XLAYER_MARGIN_POOL_ADDRESS: str = os.getenv(
    "XLAYER_MARGIN_POOL_ADDRESS",
    "0x3b14faD41CcbD471296e11Ea348dC303aA3A4156",
)

# --- Trade history persistence ---
TRADE_LOG_PATH: str = os.getenv("TRADE_LOG_PATH", "data/trade_history.jsonl")
SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")


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
            )
        )
    return assets


ASSETS: list[AssetConfig] = _parse_assets()
if not ASSETS:
    print("FATAL: no assets configured (check ASSETS env var)", file=sys.stderr)
    sys.exit(1)
ASSET_MAP: dict[str, AssetConfig] = {a.name: a for a in ASSETS}

# --- Solana (optional — disabled when SOLANA_PRIVATE_KEY is unset) ---
SOLANA_PRIVATE_KEY: str | None = os.getenv("SOLANA_PRIVATE_KEY")
SOLANA_RPC_URL: str | None = os.getenv("SOLANA_RPC_URL")
SOLANA_BATCH_SETTLER: str = os.getenv(
    "SOLANA_BATCH_SETTLER",
    "GpR6id2cHu5fUGsFm7NUKkB4NzfuEDa6brPzkSrgAzvS",  # devnet
)
SOLANA_USDC_MINT: str | None = os.getenv("SOLANA_USDC_MINT")


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
            )
        )
    return assets


SOLANA_ASSETS: list[AssetConfig] = _parse_solana_assets() if SOLANA_PRIVATE_KEY else []
SOLANA_ASSET_MAP: dict[str, AssetConfig] = {a.name: a for a in SOLANA_ASSETS}


# --- XLayer assets ---
def _parse_xlayer_assets() -> list[AssetConfig]:
    raw = os.getenv("XLAYER_ASSETS", "okb")
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
            )
        )
    return assets


XLAYER_ASSETS: list[AssetConfig] = _parse_xlayer_assets() if XLAYER_RPC_URL else []
XLAYER_ASSET_MAP: dict[str, AssetConfig] = {a.name: a for a in XLAYER_ASSETS}

# --- EVM chain configs ---
BASE_EVM = EvmChainConfig(
    name="base",
    chain_id=CHAIN_ID,
    rpc_url=RPC_URL,
    batch_settler=BATCH_SETTLER,
    usdc_address=USDC_ADDRESS,
    margin_pool_address=MARGIN_POOL_ADDRESS,
)

XLAYER_EVM: EvmChainConfig | None = None
if XLAYER_RPC_URL:
    XLAYER_EVM = EvmChainConfig(
        name="xlayer",
        chain_id=XLAYER_CHAIN_ID,
        rpc_url=XLAYER_RPC_URL,
        batch_settler=XLAYER_BATCH_SETTLER,
        usdc_address=XLAYER_USDC_ADDRESS,
        margin_pool_address=XLAYER_MARGIN_POOL_ADDRESS,
    )

EVM_CONFIGS: dict[str, EvmChainConfig] = {"base": BASE_EVM}
if XLAYER_EVM:
    EVM_CONFIGS["xlayer"] = XLAYER_EVM

# --- Chain configs ---
CHAINS: list[ChainConfig] = [ChainConfig(name="base", assets=tuple(ASSETS))]
if XLAYER_RPC_URL:
    CHAINS.append(ChainConfig(name="xlayer", assets=tuple(XLAYER_ASSETS)))
if SOLANA_PRIVATE_KEY:
    if not SOLANA_RPC_URL:
        print(
            "FATAL: SOLANA_RPC_URL required when SOLANA_PRIVATE_KEY is set",
            file=sys.stderr,
        )
        sys.exit(1)
    CHAINS.append(ChainConfig(name="solana", assets=tuple(SOLANA_ASSETS)))
