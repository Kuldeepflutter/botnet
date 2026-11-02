from decimal import Decimal

import pytest

from coindcx_client import CoinDCXError
from paper_exchange import PaperExchange
from tests.helpers import INST


def make():
    px = {"v": Decimal("100")}
    return PaperExchange(price_fn=lambda _p: px["v"], instrument=INST, balance=Decimal(1000), fee_rate=Decimal(0)), px


def test_market_buy_and_limit_fill_average():
    ex, px = make()
    ex.place_market_order(pair="P", side="buy", quantity=Decimal("10"), leverage=5)
    ex.place_limit_order(pair="P", side="buy", price=Decimal("90"), quantity=Decimal("10"), leverage=5)
    assert len(ex.list_open_orders("P")) == 1
    px["v"] = Decimal("89")
    assert ex.list_open_orders("P") == []                      # filled at its limit, not at the worse market
    row = ex.list_positions("P")[0]
    assert Decimal(row["active_pos"]) == 20 and Decimal(row["avg_price"]) == 95


def test_sell_closes_and_realizes_profit():
    ex, px = make()
    ex.place_market_order(pair="P", side="buy", quantity=Decimal("10"), leverage=5)
    ex.place_limit_order(pair="P", side="sell", price=Decimal("101.5"), quantity=Decimal("10"), leverage=5)
    px["v"] = Decimal("102")
    assert ex.list_positions("P") == [] and ex.realized == Decimal("15")


def test_cancel_and_exit_and_rejections():
    ex, px = make()
    oid = ex.place_limit_order(pair="P", side="buy", price=Decimal("90"), quantity=Decimal("1"), leverage=5)["orders"][0]["id"]
    ex.cancel_order(oid)
    with pytest.raises(CoinDCXError) as err:
        ex.cancel_order(oid)
    assert err.value.transient is False
    with pytest.raises(CoinDCXError):
        ex.place_market_order(pair="P", side="buy", quantity=Decimal("0.01"), leverage=5)    # below min quantity
    ex.place_market_order(pair="P", side="buy", quantity=Decimal("5"), leverage=5)
    ex.exit_position("paper-pos-P")
    assert ex.list_positions("P") == []


def test_simulated_call_counter():
    ex, _ = make()
    ex.list_open_orders("P"); ex.list_positions("P")
    assert ex.calls_last_minute() == 2 and sum(ex.call_counts.values()) == 2
