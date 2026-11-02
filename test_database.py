from decimal import Decimal

from database import Journal


def test_bots_events_cycles_orders_roundtrip():
    j = Journal(":memory:")
    j.save_bot("b1", "{}", '{"a":1}')
    j.save_state("b1", '{"a":2}')
    assert j.load_bots()[0]["state_json"] == '{"a":2}'
    j.add_event("b1", "info", "hello", {"k": 1})
    assert j.events("b1")[0]["severity"] == "INFO"
    j.add_cycle("b1", "P", 1, "take_profit", pnl=Decimal("2.5"))
    j.add_cycle("b1", "P", 2, "stop_loss", pnl=Decimal("-1"))
    assert j.realized_total("b1") == Decimal("1.5") and len(j.cycles()) == 2
    j.add_order("b1", "P", 1, 1, "safety", "buy", 98, 2, "x1")
    j.set_order_status("b1", "x1", "filled")
    assert j.orders("b1")[0]["status"] == "filled"
    j.remove_bot("b1")
    assert j.load_bots() == []
