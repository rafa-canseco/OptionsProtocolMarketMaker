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
SPREAD_BPS: int = int(os.getenv("SPREAD_BPS", "200"))
MAX_AMOUNT: int = int(os.getenv("MAX_AMOUNT", "500000000"))
DEADLINE_SECONDS: int = int(os.getenv("DEADLINE_SECONDS", "180"))
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
