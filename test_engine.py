from decimal import Decimal

import pytest

from config import BotConfig
from martingale_engine import LadderError, average_entry, build_ladder, plan_rows, stop_price, summarize, tp_price, validate_ladder
from tests.helpers import INST


def cfg(**kw):
    return BotConfig(pair="B-OM_USDT", base_margin=Decimal("100"), **kw)


def test_prices_compound_and_margins_double():
    ladder = build_ladder(Decimal("100"), cfg(), INST)
    assert [lv.price for lv in ladder] == [Decimal(x) for x in ("100", "98.00", "96.04", "94.12", "92.24")]
    assert [lv.margin for lv in ladder] == [Decimal(x) for x in (100, 200, 400, 800, 1600)]


def test_total_margin_matches_geometric_series():
    s = summarize(build_ladder(Decimal("100"), cfg(), INST), cfg(), INST)
    assert s.total_margin == Decimal("3100")           # 100 * (2^5 - 1)


def test_average_and_tp_rounded_up():
    ladder = build_ladder(Decimal("100"), cfg(), INST)
    avg = average_entry(ladder[:2])
    assert Decimal("98.6") < avg < Decimal("98.7")
    assert tp_price(avg, cfg(), INST) >= avg * Decimal("1.015")


def test_stop_below_last_safety_order():
    ladder = build_ladder(Decimal("100"), cfg(), INST)
    assert stop_price(ladder, cfg(sl_pct=Decimal("3")), INST) == Decimal("89.47")   # 92.24 * 0.97 = 89.47


def test_plan_rows_have_cumulative_margin():
    rows = plan_rows(build_ladder(Decimal("100"), cfg(), INST), cfg(), INST)
    assert rows[-1]["Cum. margin"] == Decimal("3100") and rows[0]["Order"].startswith("Base")


def test_min_notional_rejected():
    tiny = BotConfig(pair="B-OM_USDT", base_margin=Decimal("0.1"), leverage=1)
    with pytest.raises(LadderError):
        validate_ladder(build_ladder(Decimal("100"), tiny, INST), INST)


def test_liquidation_warning_when_stop_is_too_deep():
    deep = cfg(leverage=20, sl_pct=Decimal("10"))
    assert summarize(build_ladder(Decimal("100"), deep, INST), deep, INST).warnings
