"""Configuration for the conservative CoinDCX martingale bot."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
import os

from dotenv import load_dotenv
import json

# Load a local .env for Streamlit/CLI launches. Existing Windows environment
# variables retain precedence, and secrets are never displayed or logged.
load_dotenv(override=False)


class TradingMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"
    BACKTEST = "backtest"


class GridMode(str, Enum):
    NEUTRAL = "neutral"
    LONG = "long"
    SHORT = "short"


class GridType(str, Enum):
    ARITHMETIC = "arithmetic"
    GEOMETRIC = "geometric"


class PriceSource(str, Enum):
    BINANCE = "binance"   # Binance futures WebSocket (no CoinDCX price calls), CoinDCX only as slow fallback
    COINDCX = "coindcx"   # slow, throttled CoinDCX public trades poll


class PositionPolicy(str, Enum):
    HOLD = "hold"
    CLOSE = "close"
    MANUAL = "manual"


MAX_GRID_COUNT = 20       # hard product limit: grid levels == maximum simultaneously live orders
MAX_OPEN_ORDERS = 20


def normalize_pair(pair: str) -> str:
    raw = pair.strip().upper().replace("-", "")
    if raw.startswith("B") and "_" in pair.upper():
        return pair.upper()
    for quote in ("INR", "USDT"):
        if raw.endswith(quote) and len(raw) > len(quote):
            return f"B-{raw[:-len(quote)]}_{quote}"
    raise ValueError("Use a CoinDCX Futures pair such as OMINR or B-OM_INR.")


@dataclass(frozen=True)
class BotConfig:
    pair: str
    base_margin: Decimal = Decimal("100")
    leverage: int = 5
    max_safety_orders: int = 4
    step_pct: Decimal = Decimal("2")
    multiplier: Decimal = Decimal("2")
    tp_pct: Decimal = Decimal("1.5")
    sl_pct: Decimal = Decimal("3")
    cooldown_seconds: int = 30
    sync_seconds: float = 60
    min_touch_sync_seconds: float = 3
    paper_balance: Decimal = Decimal("10000")
    trading_mode: TradingMode = TradingMode.PAPER
    price_source: PriceSource = PriceSource.BINANCE
    # Legacy grid fields retained for dashboard compatibility.
    allocated_margin_inr: Decimal | None = None
    lower_price: Decimal | None = None
    upper_price: Decimal | None = None
    grid_count: int | None = None
    grid_mode: GridMode = GridMode.NEUTRAL
    grid_type: GridType = GridType.ARITHMETIC
    max_notional_inr: Decimal | None = None
    max_position_quantity: Decimal | None = None
    max_open_orders: int = MAX_OPEN_ORDERS  # bot-level hard cap (CoinDCX itself allows 25/market).
    daily_loss_limit_inr: Decimal = Decimal("1000")
    drawdown_limit_inr: Decimal = Decimal("2000")
    minimum_free_margin_inr: Decimal = Decimal("500")
    safety_buffer_fraction: Decimal = Decimal("0.15")
    max_slippage_fraction: Decimal = Decimal("0.003")
    maker_fee_fraction: Decimal = Decimal("0.00025")
    taker_fee_fraction: Decimal = Decimal("0.00075")
    # poll_seconds is only the LOCAL evaluation tick (in-memory price check, zero CoinDCX calls).
    poll_seconds: float = 1.0
    # --- CoinDCX call budget: the API is used only to create / check-fill / cancel orders ---
    price_source: PriceSource = PriceSource.BINANCE
    binance_symbol: str | None = None          # override for odd mappings, e.g. "1000PEPEUSDT"
    max_feed_age_seconds: float = 10.0         # Binance tick older than this => stale
    coindcx_price_seconds: float = 10.0        # min gap between CoinDCX price calls (fallback / coindcx mode)
    sync_seconds: float = 60.0                 # safety fill-check (orders + positions) even if price never touched a level
    min_touch_sync_seconds: float = 3.0        # min gap between price-touch triggered fill checks
    touch_tolerance_fraction: Decimal = Decimal("0.002")  # Binance-vs-CoinDCX slack when deciding a level was touched
    basis_check_seconds: float = 300.0         # how often CoinDCX/Binance basis is re-measured (1 call)
    max_basis_fraction: Decimal = Decimal("0.005")        # refuse to place orders if basis is wider than this
    paper_usdt_inr: Decimal = Decimal("90")
    stop_loss_price: Decimal | None = None
    take_profit_price: Decimal | None = None
    out_of_range_policy: PositionPolicy = PositionPolicy.HOLD

    def validate(self) -> "BotConfig":
        object.__setattr__(self, "pair", normalize_pair(self.pair))
        if self.base_margin <= 0:
            raise ValueError("Base margin must be positive.")
        if self.leverage < 1:
            raise ValueError("Leverage must be at least 1.")
        if self.max_safety_orders < 1 or self.multiplier < 1 or not 0 < self.step_pct < 50 or self.tp_pct <= 0 or self.sl_pct <= 0:
            raise ValueError("Invalid martingale settings.")
        return self

    def to_json(self) -> str:
        return json.dumps({k: (v.value if isinstance(v, Enum) else str(v) if isinstance(v, Decimal) else v) for k, v in self.__dict__.items()})

    @classmethod
    def from_json(cls, value: str) -> "BotConfig":
        raw = json.loads(value)
        for key, value in list(raw.items()):
            if isinstance(getattr(cls, key, None), Decimal) or key in {"base_margin","step_pct","multiplier","tp_pct","sl_pct","paper_balance","allocated_margin_inr","lower_price","upper_price","max_notional_inr","max_position_quantity","daily_loss_limit_inr","drawdown_limit_inr","minimum_free_margin_inr","safety_buffer_fraction","max_slippage_fraction","maker_fee_fraction","taker_fee_fraction","touch_tolerance_fraction","max_basis_fraction","paper_usdt_inr","stop_loss_price","take_profit_price"}:
                if value is not None: raw[key] = Decimal(value)
        raw["trading_mode"] = TradingMode(raw.get("trading_mode", "paper")); raw["price_source"] = PriceSource(raw.get("price_source", "binance")); raw["grid_mode"] = GridMode(raw.get("grid_mode","neutral")); raw["grid_type"] = GridType(raw.get("grid_type","arithmetic")); raw["out_of_range_policy"] = PositionPolicy(raw.get("out_of_range_policy","hold"))
        return cls(**raw).validate()

    @property
    def usable_margin_inr(self) -> Decimal:
        return self.allocated_margin_inr * (Decimal("1") - self.safety_buffer_fraction) - self.minimum_free_margin_inr

    @property
    def max_exposure_inr(self) -> Decimal:
        return min(self.max_notional_inr or self.allocated_margin_inr * self.leverage,
                   self.allocated_margin_inr * self.leverage)


@dataclass(frozen=True)
class ApiCredentials:
    key: str
    secret: str

    @classmethod
    def from_environment(cls) -> "ApiCredentials":
        key, secret = os.getenv("COINDCX_API_KEY", ""), os.getenv("COINDCX_API_SECRET", "")
        if not key or not secret:
            raise ValueError("COINDCX_API_KEY and COINDCX_API_SECRET must be set for live mode.")
        return cls(key=key, secret=secret)
