"""Shared fixtures: a bot wired to a PaperExchange and a hand-driven price feed (no network, no sleeping)."""
from decimal import Decimal

from binance_feed import BinancePriceFeed
from bot_service import BotService
from coindcx_client import Instrument
from config import BotConfig, TradingMode
from database import Journal
from price_source import PriceService

INST = Instrument(pair="B-OM_USDT", status="active", price_increment=Decimal("0.01"), quantity_increment=Decimal("0.1"),
                  min_quantity=Decimal("0.1"), max_quantity=Decimal("1000000"), min_notional=Decimal("1"), max_leverage=50)


def make_bot(**overrides):
    params = dict(pair="B-OM_USDT", trading_mode=TradingMode.PAPER, leverage=5, base_margin=Decimal("100"), max_safety_orders=4,
                  step_pct=Decimal("2"), multiplier=Decimal("2"), tp_pct=Decimal("1.5"), sl_pct=Decimal("3"),
                  cooldown_seconds=0, sync_seconds=0, min_touch_sync_seconds=0, paper_balance=Decimal("100000"))
    params.update(overrides)
    cfg = BotConfig(**params)
    px = {"v": Decimal("100")}
    feed = BinancePriceFeed("OMUSDT")
    prices = PriceService(cfg, lambda _p: px["v"], feed=feed)

    def set_price(value):
        px["v"] = Decimal(str(value))
        feed.feed_tick(px["v"])

    set_price(100)
    bot = BotService(cfg, prices=prices, journal=Journal(":memory:"), instrument=INST, client=_NoNetClient())
    bot.prepare()
    return bot, set_price


class _NoNetClient:
    """Stands in for CoinDCXClient: any real REST call in a test is a bug."""
    call_counts = {}

    def calls_last_minute(self):
        return 0

    def last_price(self, pair):
        raise AssertionError("unexpected CoinDCX call")
