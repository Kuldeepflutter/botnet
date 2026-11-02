"""Deliberately minimal Streamlit control surface for INR Futures grid trading."""
from __future__ import annotations

from decimal import Decimal
import socket

import streamlit as st

from bot_service import BotService
from coindcx_client import CoinDCXClient
from config import MAX_GRID_COUNT, MAX_OPEN_ORDERS, ApiCredentials, BotConfig, GridMode, GridType, PriceSource, TradingMode


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


@st.cache_resource
def grid_registry() -> dict[str, BotService]:
    """Bots live here (per Streamlit process), not in the browser session: refresh/close-tab keeps them running."""
    return {}


def _config() -> BotConfig:
    """Use conservative fixed limits so the UI needs only grid settings."""
    margin = _decimal(st.session_state.margin)
    live = st.session_state.grid_mode_trading.startswith("Live")
    return BotConfig(
        pair=st.session_state.pair, allocated_margin_inr=margin, leverage=int(st.session_state.leverage),
        lower_price=_decimal(st.session_state.lower), upper_price=_decimal(st.session_state.upper),
        grid_count=int(st.session_state.grids), grid_mode=GridMode(st.session_state.grid_mode),
        grid_type=GridType(st.session_state.grid_type), trading_mode=TradingMode.LIVE if live else TradingMode.PAPER,
        max_notional_inr=margin * int(st.session_state.leverage), max_open_orders=MAX_OPEN_ORDERS,
        daily_loss_limit_inr=margin, drawdown_limit_inr=margin,
        minimum_free_margin_inr=Decimal("0"), safety_buffer_fraction=Decimal("0"),
        paper_usdt_inr=_decimal(st.session_state.get("paper_fx", 90)),
        price_source=PriceSource(st.session_state.get("price_source", PriceSource.BINANCE.value)),
        binance_symbol=(st.session_state.get("binance_symbol") or "").strip() or None,
        stop_loss_price=_decimal(st.session_state.stop_loss) if st.session_state.stop_loss else None,
        take_profit_price=_decimal(st.session_state.take_profit) if st.session_state.take_profit else None,
    )


@st.cache_data(ttl=600, show_spinner=False)
def _active_pairs() -> list[str]:
    """Market list changes rarely: one CoinDCX call per 10 minutes instead of one per Streamlit rerun."""
    return sorted(set(CoinDCXClient().active_pairs()))


@st.cache_data(ttl=60, show_spinner=False)
def _wallet_summary() -> Decimal:
    """Return INR Futures balance; never expose credentials."""
    wallets = CoinDCXClient(ApiCredentials.from_environment()).futures_wallets()
    inr = [row for row in wallets if row.get("currency_short_name") == "INR"]
    return sum((_decimal(row.get("balance", 0)) for row in inr), Decimal("0"))


def _money(value: object) -> str:
    amount = _decimal(value)
    return f"{'+' if amount > 0 else ''}₹{amount:,.2f}"


_BADGE = {"running": "🟢 RUNNING", "paused": "🟡 PAUSED (risk gate / manual)", "stopped": "🔴 STOPPED"}


def local_ipv4() -> str:
    """Best-effort LAN address for sharing the Streamlit dashboard locally."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


@st.fragment(run_every=3)
def _bot_card(bot_id: str) -> None:
    service = grid_registry().get(bot_id)
    if service is None:
        return
    s = service.status()
    with st.container(border=True):
        st.markdown(f"#### Bot `{bot_id}`  \n{_BADGE[s['run_state']]}  ·  {s['mode']} · {s['pair']} · {service.config.grid_mode.value}")
        stop_col, resume_col, remove_col = st.columns(3)
        if stop_col.button("Stop", key=f"stop_{bot_id}", disabled=s["run_state"] == "stopped",
                           help="Stops the bot and cancels its open orders. An open position is NOT closed."):
            try:
                service.stop(cancel_open_orders=True)
            except Exception as exc:
                st.error(str(exc))
        if resume_col.button("Resume", key=f"resume_{bot_id}", disabled=s["run_state"] == "running",
                             help="Cancels this bot's open orders and re-places the grid around the current price. Positions are kept."):
            try:
                if s["run_state"] == "paused":
                    service.stop(cancel_open_orders=True)   # a paused grid may be missing a level; rebuild it cleanly
                service.restart()
            except Exception as exc:
                st.error(str(exc))
        if remove_col.button("Remove", key=f"remove_{bot_id}", disabled=s["run_state"] != "stopped"):
            removed = grid_registry().pop(bot_id, None)
            if removed is not None:
                removed.shutdown()
            st.rerun()
        realized, unrealized = _decimal(s["realized_profit_inr"]), _decimal(s["unrealized_pnl_inr"])
        a, b, c = st.columns(3)
        a.metric("Realized profit / loss", _money(realized), help="Completed grid cycles since this bot was created.")
        b.metric("Unrealized profit / loss", _money(unrealized), help="Open position marked to the last price (fees/funding excluded).")
        c.metric("Total", _money(realized + unrealized))
        st.caption(f"Position: {s['position_quantity']} @ avg {s['position_avg_price']}  ·  last price: {s['last_price'] or '-'}  ·  "
                   f"completed cycles: {s['cycles']['completed']}")
        st.caption(f"Price feed: {s['price_source'] or '-'} (age {s['price_age_seconds'] if s['price_age_seconds'] is not None else '-'}s)  ·  "
                   f"CoinDCX/Binance basis: {s['basis'] or '-'}  ·  last fill check: {s['last_sync_reason'] or '-'} "
                   f"{s['last_sync_age_seconds'] if s['last_sync_age_seconds'] is not None else '-'}s ago  ·  "
                   f"CoinDCX calls in last minute: {s['coindcx_calls_last_minute']}  ·  queued orders: {s['pending_orders']}")
        if s["price_gate"]:
            st.warning(s["price_gate"])
        if s["last_error"]:
            st.error(s["last_error"])
        with st.expander("Grid levels, orders and events"):
            st.dataframe(s["levels"])
            st.dataframe([o for o in s["orders"] if o["pair"] == s["pair"]])
            st.dataframe(s["events"])
            st.caption("CoinDCX requests made by this bot (by endpoint)")
            st.dataframe([{"endpoint": k, "calls": v} for k, v in sorted(s["coindcx_call_counts"].items())])


def run() -> None:
    st.set_page_config(page_title="CoinDCX INR Futures Bots", layout="centered")
    st.title("CoinDCX INR Futures Grid")
    st.caption(f"Dashboard address: http://{local_ipv4()}:8501  ·  local-only: http://127.0.0.1:8501")
    try:
        pairs = _active_pairs()
    except Exception as exc:
        st.error(f"Could not load INR Futures markets: {exc}")
        return
    bots = grid_registry()

    with st.expander("Create a new grid bot", expanded=not bots):
        st.selectbox("INR Futures market", pairs, key="pair")
        st.number_input("Total allocated margin (INR)", min_value=1.0, value=10000.0, key="margin")
        st.number_input("Leverage", min_value=1, max_value=100, value=1, step=1, key="leverage")
        st.number_input("Lower price", min_value=0.0000001, value=90.0, format="%.8f", key="lower")
        st.number_input("Upper price", min_value=0.0000001, value=110.0, format="%.8f", key="upper")
        st.number_input(f"Grid size (max {MAX_GRID_COUNT})", min_value=2, max_value=MAX_GRID_COUNT, value=10, key="grids",
                        help=f"Hard limit of {MAX_GRID_COUNT} levels / live orders per bot.")
        st.selectbox("Price source", [PriceSource.BINANCE.value, PriceSource.COINDCX.value], key="price_source",
                     help="Binance streams prices over a free WebSocket, so CoinDCX is only called to create orders, check fills and cancel. "
                          "CoinDCX mode polls CoinDCX prices every 10 s.")
        if st.session_state.get("price_source") == PriceSource.BINANCE.value:
            st.text_input("Binance symbol override (optional)", key="binance_symbol",
                          help="Leave empty to map B-BTC_USDT -> BTCUSDT automatically. Needed for names like 1000PEPEUSDT.")
        st.selectbox("Grid mode", [mode.value for mode in GridMode], key="grid_mode")
        st.selectbox("Grid spacing", [kind.value for kind in GridType], key="grid_type")
        st.number_input("Stop loss price (0 = disabled)", min_value=0.0, value=0.0, format="%.8f", key="stop_loss")
        st.number_input("Take profit price (0 = disabled)", min_value=0.0, value=0.0, format="%.8f", key="take_profit")
        live = st.radio("Mode", ["Paper (simulated, no real orders)", "Live (real INR Futures orders)"], key="grid_mode_trading").startswith("Live")
        if live:
            try:
                st.metric("Current INR Futures balance", f"₹{_wallet_summary():,.2f}")
            except Exception as exc:
                st.error(f"Could not read INR Futures balance: {exc}")
            st.checkbox("I confirm that Start sends real INR Futures orders.", key="live_confirm")
        else:
            st.number_input("Paper USDT/INR assumption", min_value=1.0, value=90.0, key="paper_fx")
        st.caption("The lower and upper prices are grid bounds. Stop loss and take profit are separate full-position CoinDCX triggers.")
        if st.button("Create and start bot", type="primary"):
            try:
                if live and not st.session_state.live_confirm:
                    raise ValueError("Confirmation is required before live trading.")
                service = BotService(_config())
                service.start()
                bots[service.bot_id] = service
                st.success(f"Bot {service.bot_id} created and running.")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))

    st.header("My grid bots")
    if not bots:
        st.info("No bots yet. Create one above.")
    for bot_id in list(bots):
        _bot_card(bot_id)


if __name__ == "__main__":
    from martingale_dashboard import run as martingale_run
    martingale_run()
