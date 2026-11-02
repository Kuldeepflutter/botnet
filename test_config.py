from decimal import Decimal

import pytest

from config import BotConfig, PriceSource, TradingMode, normalize_pair


def test_normalize_pair():
    assert normalize_pair("omusdt") == "B-OM_USDT" and normalize_pair("B-OM_USDT") == "B-OM_USDT"
    with pytest.raises(ValueError):
        normalize_pair("OMINR")


def test_json_roundtrip():
    cfg = BotConfig(pair="OMUSDT", trading_mode=TradingMode.LIVE, step_pct=Decimal("2.5"), price_source=PriceSource.COINDCX).validate()
    again = BotConfig.from_json(cfg.to_json())
    assert again == cfg and again.pair == "B-OM_USDT"


@pytest.mark.parametrize("bad", [dict(leverage=0), dict(base_margin=Decimal(0)), dict(max_safety_orders=0), dict(multiplier=Decimal("0.5")),
                                 dict(step_pct=Decimal(60)), dict(tp_pct=Decimal(0))])
def test_validation_rejects(bad):
    with pytest.raises(ValueError):
        BotConfig(pair="B-OM_USDT", **bad).validate()
