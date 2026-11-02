import dashboard_helpers as ui


def test_money_and_number_formatting():
    assert ui.fmt_money("1234.5") == "+1,234.50 USDT" and ui.fmt_money("-5") == "-5.00 USDT" and ui.fmt_money("12", signed=False) == "12.00 USDT"
    assert ui.fmt_num("100.0000") == "100" and ui.fmt_num("0.50") == "0.5" and ui.fmt_num(None) == "-" and ui.fmt_num("1E+2") == "100"
    assert ui.tone_for_amount("3") == "cx-pos" and ui.tone_for_amount("-3") == "cx-neg" and ui.tone_for_amount("0") == ""


def test_html_is_escaped():
    assert "<script>" not in ui.kpi("<script>x</script>", "<b>x</b>", "<i>")
    assert "<script>" not in ui.info_rows([("k<", "<script>")])
    assert "<img" not in ui.bot_header("<img src=x>", "running", "paper", "<b>", "<u>")


LEVELS = [{"label": "Base", "price": "100", "state": "filled", "side": "buy"},
          {"label": "Safety #1", "price": "98", "state": "open", "side": "buy"},
          {"label": "Safety #2", "price": "96", "state": "cancelled", "side": "buy"},
          {"label": "Take profit", "price": "101.5", "state": "open", "side": "sell"},
          {"label": "Stop-loss", "price": "90", "state": "stop", "side": "sell"}]


def test_ladder_orders_prices_descending_and_marks_market():
    out = ui.ladder_html(LEVELS, "99")
    prices = [out.index(f"<span>{p}</span>") for p in ("101.5", "100", "98", "96", "90")]
    assert prices == sorted(prices)
    assert out.index("Market 99") > out.index("<span>100</span>") and out.index("Market 99") < out.index("<span>98</span>")
    assert out.count("cx-lr cx-buy") == 1 and out.count("cx-lr cx-sell") == 1 and "market exit" in out and "cancelled" in out


def test_ladder_edges():
    assert "Market 1" in ui.ladder_html(LEVELS, "1") and "Market 500" in ui.ladder_html(LEVELS, "500")
    assert "Market" not in ui.ladder_html(LEVELS, None) and "No active cycle" in ui.ladder_html([], "5")


def test_aggregate_and_tables():
    sts = [{"realized_pnl": "5", "unrealized_pnl": "-2", "run_state": "running", "cycles": {"completed": 3}, "coindcx_calls_last_minute": 4},
           {"realized_pnl": "1", "unrealized_pnl": "0", "run_state": "paused", "cycles": {"completed": 1}, "coindcx_calls_last_minute": 1}]
    agg = ui.aggregate(sts)
    assert agg["running"] == 1 and agg["attention"] == 1 and agg["total"] == 4 and agg["cycles"] == 4 and agg["calls_per_min"] == 5
    assert ui.prepare_calls({"a": 3, "b": 1})[0] == {"Endpoint": "a", "Calls": 3, "Share": "75%"}
    assert ui.prepare_cycles([{"result": "stop_loss", "pnl": "-1.5"}])[0]["Result"].startswith("🛑")
