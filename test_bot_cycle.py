from decimal import Decimal

import pytest

import bot_service
from bot_service import BotError
from coindcx_client import CoinDCXError
from martingale_engine import tp_price
from tests.helpers import INST, make_bot


def open_orders(bot):
    return bot.ex.list_open_orders(bot.config.pair)


def total_calls(bot):
    return sum(bot.ex.call_counts.values())


def test_cycle_start_places_base_ladder_and_tp():
    bot, price = make_bot()
    bot.tick()
    st = bot.state
    assert st.active and st.cycle_no == 1 and len(st.so_orders) == 4
    orders = open_orders(bot)
    assert sum(o["side"] == "buy" for o in orders) == 4 and sum(o["side"] == "sell" for o in orders) == 1
    assert Decimal(st.tp_price) >= Decimal("100") * Decimal("1.015") and Decimal(st.tp_qty) == Decimal("5.0")
    assert Decimal(st.stop_price) == Decimal("89.47")


def test_safety_fill_cancels_old_tp_and_reprices_from_new_average():
    bot, price = make_bot()
    bot.tick()
    old_tp = bot.state.tp_order_id
    price(97.9)                                    # safety order #1 (98.00) fills
    bot.tick()
    st = bot.state
    pos = bot._position()
    assert st.so_orders[0]["filled"] and not st.so_orders[1]["filled"]
    assert st.tp_order_id != old_tp and Decimal(st.tp_qty) == pos.qty
    assert Decimal(st.tp_price) == tp_price(pos.avg_price, bot.config, INST)
    sells = [o for o in open_orders(bot) if o["side"] == "sell"]
    assert len(sells) == 1 and Decimal(sells[0]["total_quantity"]) == pos.qty        # exactly one unified TP
    assert pos.avg_price < Decimal("100")


def test_take_profit_hit_resets_cycle_and_cancels_leftovers():
    bot, price = make_bot()
    bot.tick()
    price(97.9); bot.tick()
    target = Decimal(bot.state.tp_price)
    price(target + 1); bot.tick()
    assert not bot.state.active and bot.state.cycles_completed == 1
    assert bot.state.so_orders == [] and bot.state.tp_order_id is None
    assert open_orders(bot) == [] and bot._position() is None
    cycle = bot.journal.cycles(bot.bot_id)[0]
    assert cycle["result"] == "take_profit" and Decimal(cycle["pnl"]) > 0 and cycle["safety_filled"] == 1
    assert bot.status()["realized_pnl"] > 0


def test_stop_loss_only_after_all_safety_orders_filled():
    bot, price = make_bot()
    bot.tick()
    price(95); bot.tick()                          # safety #1 and #2 filled, price still above the stop
    assert bot.state.active and bot.status()["safety_filled"] == 2
    price(85); bot.tick()                          # all filled and price < stop (89.47)
    assert not bot.state.active and bot.state.halted and bot._stop.is_set()
    assert bot.journal.cycles(bot.bot_id)[0]["result"] == "stop_loss"
    assert Decimal(bot.journal.cycles(bot.bot_id)[0]["pnl"]) < 0
    assert bot._position() is None and open_orders(bot) == []


def test_price_below_stop_without_all_fills_does_not_stop_out():
    bot, price = make_bot()
    bot.tick()
    bot.ex.cancel_order(bot.state.so_orders[-1]["id"])           # someone removed the last safety order -> never "all filled"
    bot.state.so_orders[-1]["cancelled"] = True
    price(85); bot.tick()
    assert bot.state.active and not bot.state.halted


def test_low_call_budget_when_price_is_far_from_levels():
    bot, price = make_bot(sync_seconds=60, min_touch_sync_seconds=0)
    bot.tick()
    before = total_calls(bot)
    for p in ("99.9", "100.2", "99.5"):
        price(p); bot.tick()
    assert total_calls(bot) == before                            # zero CoinDCX calls while price is idle
    price("98.1"); bot.tick()                                    # touches safety #1 (within 0.2 %)
    assert total_calls(bot) > before and bot.last_sync_reason == "touch-safety"


def test_touch_with_nothing_found_backs_off():
    bot, price = make_bot(sync_seconds=60, min_touch_sync_seconds=0)
    bot.tick()
    price("98.1"); bot.tick()
    first = total_calls(bot)
    bot.tick()                                                   # still hovering: exponential backoff blocks the next check
    assert total_calls(bot) == first and bot._touch_backoff >= 3


def test_pause_and_drain_flags():
    bot, price = make_bot()
    bot.entries_paused = True
    bot.tick()
    assert not bot.state.active
    bot.entries_paused, bot.drain = False, True
    bot.tick()
    assert not bot.state.active and bot._stop.is_set()


def test_stop_with_cancel_then_resume_rebuilds_ladder_and_tp():
    bot, price = make_bot()
    bot.tick()
    bot.stop(cancel_open_orders=True)
    assert open_orders(bot) == [] and all(s["cancelled"] for s in bot.state.so_orders) and bot._position() is not None
    bot._force_sync = True
    price(99); bot.tick()                                        # what the first tick after Resume does
    orders = open_orders(bot)
    assert sum(o["side"] == "buy" for o in orders) == 4 and sum(o["side"] == "sell" for o in orders) == 1
    assert not any(s["cancelled"] for s in bot.state.so_orders)


def test_base_order_rejection_is_a_bot_error_not_a_position():
    bot, price = make_bot()
    def reject(**kw):
        raise CoinDCXError("insufficient margin", status=400, transient=False)
    bot.ex.place_market_order = reject
    with pytest.raises(BotError):
        bot.tick()
    assert not bot.state.active and bot._position() is None


def test_unknown_order_outcome_is_reconciled_not_duplicated(monkeypatch):
    monkeypatch.setattr(bot_service.time, "sleep", lambda _s: None)
    bot, price = make_bot()
    real, calls = bot.ex.place_limit_order, {"n": 0}
    def flaky(**kw):
        calls["n"] += 1
        resp = real(**kw)
        if calls["n"] == 1:
            raise CoinDCXError("timeout after acceptance", transient=True)     # order WAS accepted, answer was lost
        return resp
    bot.ex.place_limit_order = flaky
    bot.tick()
    assert len(bot.state.so_orders) == 4 and len({s["id"] for s in bot.state.so_orders}) == 4
    assert sum(o["side"] == "buy" for o in open_orders(bot)) == 4              # no duplicate safety order


def test_insufficient_wallet_blocks_cycle():
    bot, price = make_bot(paper_balance=Decimal("500"))
    with pytest.raises(BotError):
        bot.tick()
    assert bot._position() is None
