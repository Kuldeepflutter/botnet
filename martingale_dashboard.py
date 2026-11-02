"""Streamlit control surface for the CoinDCX long martingale strategy."""
from decimal import Decimal
import socket
from urllib.request import urlopen
import streamlit as st
from bot_service import BotService
from coindcx_client import CoinDCXClient, Instrument
from coindcx_client import CoinDCXError
from config import ApiCredentials, BotConfig, PriceSource, TradingMode
from database import Journal
from price_source import PriceService
from binance_feed import BinancePriceFeed, binance_symbol_for
from market_data import MarketDataService

def ipv4():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s: s.connect(("8.8.8.8",80)); return s.getsockname()[0]
    except OSError: return "127.0.0.1"
@st.cache_data(ttl=300, show_spinner=False)
def public_ipv4():
    """The external address CoinDCX sees for an IP-bound API key."""
    try:
        with urlopen("https://api.ipify.org", timeout=4) as response:
            return response.read().decode().strip()
    except OSError:
        return "unavailable"
@st.cache_resource
def bots(): return {}
def new_bot():
    mode=TradingMode.LIVE if st.session_state.mode == "Live INR-M Futures" else TradingMode.PAPER
    cfg=BotConfig(pair=st.session_state.pair,base_margin=Decimal(str(st.session_state.margin)),leverage=st.session_state.leverage,max_safety_orders=st.session_state.safety,step_pct=Decimal(str(st.session_state.step)),multiplier=Decimal(str(st.session_state.multiplier)),tp_pct=Decimal(str(st.session_state.tp)),sl_pct=Decimal(str(st.session_state.sl)),paper_balance=Decimal(str(st.session_state.balance)),trading_mode=mode,price_source=PriceSource.COINDCX).validate()
    # CoinDCX INR-M contracts retain the underlying's USDT quote in the pair name
    # (for example B-BTC_USDT); INR-M is selected by margin_currency_short_name.
    if not cfg.pair.endswith("_USDT"): raise ValueError("Choose a CoinDCX Futures contract such as B-BTC_USDT. The bot submits it with INR margin.")
    client=CoinDCXClient(ApiCredentials.from_environment()) if mode is TradingMode.LIVE else CoinDCXClient()
    inst=MarketDataService(client).validate_inr_instrument(cfg.pair,cfg.leverage)
    return BotService(cfg,prices=PriceService(cfg,client.last_price),journal=Journal(),instrument=inst,client=client)
def run():
    st.set_page_config(page_title="CoinDCX Martingale Bot",layout="wide"); st.title("CoinDCX Futures Martingale Bot")
    st.caption(f"LAN dashboard: http://{ipv4()}:8501  ·  CoinDCX API whitelist IP: {public_ipv4()}  ·  Local: http://127.0.0.1:8501")
    with st.expander("Create martingale bot",expanded=not bots()):
        st.text_input("CoinDCX INR-M Futures contract",value="B-BTC_USDT",key="pair",help="CoinDCX INR-M contracts use the B-*_USDT symbol; INR is the margin currency."); a,b,c=st.columns(3); a.number_input("Base margin (INR)",1.,value=1000.,key="margin"); b.number_input("Leverage",1,50,5,key="leverage"); c.number_input("Safety orders",1,10,4,key="safety")
        a,b,c,d=st.columns(4); a.number_input("Safety step (%)",.1,30.,2.,key="step"); b.number_input("Size multiplier",1.,5.,2.,key="multiplier"); c.number_input("Take profit (%)",.1,20.,1.5,key="tp"); d.number_input("Stop loss (%)",.1,30.,3.,key="sl")
        st.radio("Mode",["Paper INR-M Futures","Live INR-M Futures"],key="mode"); st.number_input("Paper INR Futures balance",100.,value=100000.,key="balance")
        if st.session_state.mode == "Live INR-M Futures":
            try:
                wallet=CoinDCXClient(ApiCredentials.from_environment()).futures_wallets(); row=next(x for x in wallet if x.get("currency_short_name")=="INR")
                available=Decimal(str(row.get("balance",0))); locked=Decimal(str(row.get("locked_balance",0)))
                a,b,c=st.columns(3); a.metric("CoinDCX INR Futures available",f"₹{available:,.2f}"); b.metric("Locked in positions/orders",f"₹{locked:,.2f}"); c.metric("Total Futures wallet",f"₹{available + locked:,.2f}")
            except (CoinDCXError, StopIteration, ValueError) as e: st.error(f"Cannot read INR Futures wallet: {e}")
            st.checkbox("I confirm this will submit real INR-M Futures orders.",key="confirm_live")
        st.info("INR-M only. Safety buys compound downwards, one TP sell is re-priced after fills, and stop loss requires every safety order to fill.")
        if st.button("Start martingale bot",type="primary"):
            try:
                if st.session_state.mode == "Live INR-M Futures" and not st.session_state.get("confirm_live"): raise ValueError("Confirm live INR-M trading before starting.")
                b=new_bot(); bots()[b.bot_id]=b; st.rerun()
            except Exception as e: st.error(str(e))
    st.header("Active martingale bots")
    for id,b in list(bots().items()):
        try: b.tick()
        except Exception as e: st.error(f"{id}: {e}")
        s=b.status(); st.subheader(f"{b.config.pair} · {'RUNNING' if b.state.active else 'IDLE'}"); x,y,z=st.columns(3); x.metric("Realized P/L",f"{s['realized_pnl']:.2f} USDT"); y.metric("Safety fills",s['safety_filled']); z.metric("Completed cycles",s['cycles']['completed']); st.json({'take_profit':b.state.tp_price,'stop_loss':b.state.stop_price,'safety_orders':b.state.so_orders})
        if st.button("Stop & cancel",key=id): b.stop(cancel_open_orders=True)
if __name__ == '__main__': run()
