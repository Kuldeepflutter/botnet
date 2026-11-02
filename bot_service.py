"""Runtime orchestration. Paper orders are explicitly local and never sent to CoinDCX."""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
from decimal import Decimal
from threading import Event, Thread
import random
import time
import uuid
from typing import Any

from coindcx_client import CoinDCXClient, CoinDCXError, Instrument
from config import ApiCredentials, BotConfig, GridMode, TradingMode
from database import Database
from grid_engine import GridEngine, LevelState, OrderIntent, quote_notional_inr, round_to_increment
from logger import get_logger
from market_data import MarketDataService
from order_manager import OrderManager
from price_source import PriceService, Quote
from risk_manager import RiskManager, RiskSnapshot, RiskViolation


class PaperExchange:
    """In-memory Futures approximation for workflow tests; never contacts private APIs."""
    def __init__(self, price_provider: CoinDCXClient, pair: str):
        self.public, self.pair = price_provider, pair
        self.orders: dict[str, dict[str, Any]] = {}
        self.position = Decimal("0")
        self.avg_price = Decimal("0")

    def _apply_fill(self, side: str, quantity: Decimal, price: Decimal) -> None:
        """Net-position accounting with a volume-weighted average entry price."""
        signed = quantity if side == "buy" else -quantity
        new = self.position + signed
        if self.position == 0 or (self.position > 0) == (signed > 0):
            total = abs(self.position) + quantity
            self.avg_price = (self.avg_price * abs(self.position) + price * quantity) / total
        elif (new > 0) != (self.position > 0) and new != 0:
            self.avg_price = price          # flipped through zero: remainder opens at the fill price
        if new == 0:
            self.avg_price = Decimal("0")
        self.position = new

    def list_orders(self, *, status: str = "open", page: int = 1, size: int = 100) -> list[dict[str, Any]]:
        wanted = set(status.split(","))
        rows = [dict(order) for order in self.orders.values() if order["status"] in wanted]
        return rows[(page - 1) * size: page * size]

    def list_all_orders(self, *, status: str, size: int = 100, max_pages: int = 5, stop_when_found: set[str] | None = None) -> list[dict[str, Any]]:
        return CoinDCXClient.list_all_orders(self, status=status, size=size, max_pages=max_pages, stop_when_found=stop_when_found)  # type: ignore[arg-type]

    def place_limit_order(self, *, pair: str, side: str, price: Decimal, quantity: Decimal, leverage: int, margin_type: str = "isolated") -> dict[str, Any]:
        order_id = f"paper-{uuid.uuid4()}"
        self.orders[order_id] = {"id": order_id, "pair": pair, "side": side, "price": str(price), "total_quantity": str(quantity),
                                 "remaining_quantity": str(quantity), "status": "open", "leverage": leverage, "paper": True}
        return self.orders[order_id]

    def place_market_order(self, *, pair: str, side: str, quantity: Decimal, leverage: int, margin_type: str = "isolated") -> dict[str, Any]:
        order_id = f"paper-market-{uuid.uuid4()}"
        price = self.public.last_price(pair)
        self.orders[order_id] = {"id": order_id, "pair": pair, "side": side, "price": str(price), "avg_price": str(price),
                                 "total_quantity": str(quantity), "remaining_quantity": "0", "status": "filled", "leverage": leverage, "paper": True}
        self._apply_fill(side, quantity, price)
        return self.orders[order_id]

    def create_position_tpsl(self, **kwargs: Any) -> dict[str, Any]:
        return {"paper": True, **kwargs}

    def advance(self, last_price: Decimal) -> None:
        for order in self.orders.values():
            if order["status"] != "open":
                continue
            price = Decimal(str(order["price"]))
            should_fill = (order["side"] == "buy" and last_price <= price) or (order["side"] == "sell" and last_price >= price)
            if should_fill:
                qty = Decimal(str(order["total_quantity"]))
                order.update(status="filled", remaining_quantity="0", avg_price=str(price))
                self._apply_fill(order["side"], qty, price)

    def list_positions(self, pair: str | None = None) -> list[dict[str, Any]]:
        return [{"id": "paper-position", "pair": self.pair, "active_pos": str(self.position), "avg_price": str(self.avg_price), "locked_margin": "0"}]

    def cancel_all_open_orders(self) -> dict[str, Any]:
        for order in self.orders.values():
            if order["status"] == "open":
                order["status"] = "cancelled"
        return {"status": 200, "paper": True}

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        self.orders[order_id]["status"] = "cancelled"
        return {"status": 200, "paper": True}


class BotError(RuntimeError): pass

class _State:
    def __init__(self): self.active=False; self.halted=False; self.cycle_no=0; self.cycles_completed=0; self.so_orders=[]; self.tp_order_id=None; self.tp_price=None; self.tp_qty=None; self.stop_price=None

class BotService:
    """A local, paper-first long martingale runner. Live clients share this reconciliation flow."""
    def __init__(self, config, *, prices, journal, instrument, client=None, bot_id=None):
        from paper_exchange import PaperExchange
        config.validate(); self.config=config; self.prices=prices; self.journal=journal; self.instrument=instrument; self.bot_id=bot_id or uuid.uuid4().hex
        self.ex=PaperExchange(price_fn=lambda _p: self.prices.get().price, instrument=instrument, balance=config.paper_balance) if config.trading_mode is TradingMode.PAPER else client
        self.state=_State(); self.entries_paused=False; self.drain=False; self._stop=Event(); self._force_sync=False; self._touch_backoff=0; self.last_sync_reason=None
    def prepare(self): return self
    def _position(self):
        rows=self.ex.list_positions(self.config.pair); return None if not rows else type('Position',(),{'id':rows[0]['id'],'qty':Decimal(rows[0]['active_pos']),'avg_price':Decimal(rows[0]['avg_price'])})()
    def _cancel(self, oid):
        try: self.ex.cancel_order(oid)
        except CoinDCXError: pass
    def _place_tp(self, pos):
        from martingale_engine import tp_price
        if self.state.tp_order_id: self._cancel(self.state.tp_order_id)
        price=tp_price(pos.avg_price,self.config,self.instrument); r=self.ex.place_limit_order(pair=self.config.pair,side='sell',price=price,quantity=pos.qty,leverage=self.config.leverage)
        self.state.tp_order_id=r['orders'][0]['id']; self.state.tp_price=str(price); self.state.tp_qty=str(pos.qty)
    def _start(self, price):
        from martingale_engine import build_ladder, stop_price, summarize
        ladder=build_ladder(price,self.config,self.instrument)
        if self.ex.available_margin() < summarize(ladder,self.config,self.instrument).total_margin: raise BotError('Insufficient wallet balance for full ladder')
        base=ladder[0]; self.ex.place_market_order(pair=self.config.pair,side='buy',quantity=base.quantity,leverage=self.config.leverage)
        self.state.active=True; self.state.cycle_no+=1; self.state.stop_price=str(stop_price(ladder,self.config,self.instrument)); self.state.so_orders=[]
        for lv in ladder[1:]:
            r=self.ex.place_limit_order(pair=self.config.pair,side='buy',price=lv.price,quantity=lv.quantity,leverage=self.config.leverage); self.state.so_orders.append({'id':r['orders'][0]['id'],'price':str(lv.price),'filled':False,'cancelled':False})
        self._place_tp(self._position())
    def _finish(self,result,price):
        pos=self._position(); pnl=(Decimal(price)-pos.avg_price)*pos.qty if pos else Decimal(0)
        for s in self.state.so_orders: self._cancel(s['id'])
        if pos: self.ex.exit_position(pos.id)
        self.journal.add_cycle(self.bot_id,self.config.pair,self.state.cycle_no,result,pnl=pnl); self.state.active=False; self.state.cycles_completed+=1; self.state.so_orders=[]; self.state.tp_order_id=None
        if result=='stop_loss': self.state.halted=True; self._stop.set()
    def tick(self):
        if self.drain: self._stop.set(); return
        if self._stop.is_set() and not self._force_sync: return
        price=self.prices.get().price
        if not self.state.active:
            if not self.entries_paused: self._start(price)
            return
        open_ids={o['id'] for o in self.ex.list_open_orders(self.config.pair)}; changed=False
        for s in self.state.so_orders:
            if not s['cancelled'] and not s['filled'] and s['id'] not in open_ids: s['filled']=True; changed=True
        if self.state.tp_order_id not in open_ids:
            self._finish('take_profit',price); return
        pos=self._position()
        if changed and pos: self._place_tp(pos)
        all_filled=all(s['filled'] for s in self.state.so_orders if not s['cancelled']) and not any(s['cancelled'] for s in self.state.so_orders)
        if all_filled and price <= Decimal(self.state.stop_price): self._finish('stop_loss',price)
    def stop(self,cancel_open_orders=False):
        if cancel_open_orders:
            for s in self.state.so_orders: self._cancel(s['id']); s['cancelled']=True
            if self.state.tp_order_id: self._cancel(self.state.tp_order_id)
        self._stop.set()
    def status(self):
        pos=self._position(); return {'realized_pnl':self.journal.realized_total(self.bot_id),'unrealized_pnl':Decimal(0) if not pos else (self.prices.get().price-pos.avg_price)*pos.qty,'safety_filled':sum(1 for s in self.state.so_orders if s['filled']),'run_state':'stopped' if self._stop.is_set() else 'running','cycles':{'completed':self.state.cycles_completed},'coindcx_calls_last_minute':self.ex.calls_last_minute()}

class LegacyBotService:
    def __init__(self, config: BotConfig, database_path: str = "grid_bot.sqlite3", bot_id: str | None = None):
        config.validate()
        self.config = config
        self.bot_id = bot_id or f"grid-{uuid.uuid4().hex[:8]}"
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.db = Database(database_path)
        self.log = get_logger("coindcx_grid.service")
        public_client = CoinDCXClient()
        # Reused for every poll tick instead of opening a brand-new HTTPS session (and
        # paying a fresh TLS handshake) every few seconds for the lifetime of the bot.
        self._price_client = public_client
        # Prices come from Binance's free WebSocket (CoinDCX only as a throttled fallback). CoinDCX REST is
        # reserved for: creating orders, checking fills, cancelling orders (and one-off startup lookups).
        self.prices = PriceService(config, public_client.last_price)
        market = MarketDataService(public_client)
        self.instrument = market.validate_inr_instrument(config.pair, config.leverage)
        # CoinDCX documents conversion as an authenticated endpoint. Paper mode therefore
        # uses the user's clearly displayed simulation assumption, never a fabricated live rate.
        self.conversion_rate = (CoinDCXClient(ApiCredentials.from_environment()).conversion_rate()
                                if config.trading_mode is TradingMode.LIVE else config.paper_usdt_inr)
        self.engine = GridEngine(config, self.instrument, self.conversion_rate)
        self.exchange: Any = (CoinDCXClient(ApiCredentials.from_environment()) if config.trading_mode is TradingMode.LIVE
                              else PaperExchange(public_client, config.pair))
        self.risk = RiskManager(config, self.instrument, self.conversion_rate)
        self.orders = OrderManager(self.exchange, self.db, self.engine, self.risk)
        self.paused, self.stopped = False, False
        # Directional grids (long/short) open their starting inventory at market, like Binance/Bitget.
        self._inv_entry_sent = False
        self._inv_entry_price: Decimal | None = None
        self._inv_expected = Decimal("0")
        self._resuming = False
        self._tpsl_set = False
        self.last_error: str | None = None
        self.last_heartbeat: float | None = None
        self.last_price: Decimal | None = None
        self._position_qty, self._position_avg = Decimal("0"), Decimal("0")  # cached from the loop; no extra API calls
        self._position_id: str | None = None
        self.price_quote: Quote | None = None
        self._snapshot_cache = RiskSnapshot()          # local view; refreshed only by _sync and advanced locally on each order we place
        self._pending: list[OrderIntent] = []          # counterpart orders still to be placed (survive RiskViolation / errors)
        self._synced_at: float | None = None
        self._last_sync_reason: str | None = None
        self._force_sync = False
        self._awaiting_reconcile = False               # a create-order outcome is unknown: reconcile before ANY further POST
        self._touch_interval = config.min_touch_sync_seconds
        self._price_gate: str | None = None            # set when Binance/CoinDCX basis is too wide
        self._submit_failures = 0
        self._last_status_write = (0.0, None)
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._recover()
        self._initialised = any(level.exchange_order_id for level in self.engine.levels)

    def _recover(self) -> None:
        """Recover only open orders that still exist at CoinDCX.

        A local journal can contain cancelled orders from an older grid size.
        Restoring them into a new layout was the source of the prior index
        error. Live, unknown or mismatched exchange orders are rejected with a
        clear message rather than silently creating duplicate grid orders.
        """
        active_statuses = {"open", "partially_filled"}
        journal = [row for row in self.db.orders()
                   if row["pair"] == self.config.pair and str(row["status"]).lower() in active_statuses]
        remote_by_id: dict[str, dict[str, Any]] = {}
        if self.config.trading_mode is TradingMode.LIVE:
            remote_by_id = {str(item["id"]): item for item in self.exchange.list_orders(status="open,partially_filled")
                            if item.get("pair") == self.config.pair and item.get("id") is not None}
        journal_ids = {str(row["exchange_order_id"]) for row in journal}
        if set(remote_by_id).difference(journal_ids):
            raise RuntimeError("Grid start is blocked: this market has open CoinDCX orders not owned by this bot. Cancel or manage them manually first.")
        for row in journal:
            order_id = str(row["exchange_order_id"])
            remote = remote_by_id.get(order_id)
            if self.config.trading_mode is TradingMode.LIVE and remote is None:
                self.db.log("WARNING", "Stale local open-order journal entry ignored", {"id": order_id})
                continue
            level_index = int(row["level_index"])
            if not 0 <= level_index < len(self.engine.levels):
                raise RuntimeError("Saved open orders use a different grid size. Cancel those CoinDCX orders before starting a new grid.")
            level = self.engine.levels[level_index]
            if Decimal(str(row["price"])) != level.price or Decimal(str(row["quantity"])) != self.engine.quantity:
                raise RuntimeError("Saved open orders use different grid settings. Cancel those CoinDCX orders before starting a new grid.")
            intent = OrderIntent(level_index, row["side"], Decimal(row["price"]), Decimal(row["quantity"]), "recovered")
            self.engine.record_submission(intent, order_id)
            self.engine.record_status(order_id, str((remote or row)["status"]), Decimal(str((remote or row).get("filled_quantity", row["filled_quantity"]))))
        self.db.log("INFO", "Open-order journal recovery completed", {"pair": self.config.pair, "recovered": len(journal)})

    # ------------------------------------------------------------------ exchange sync (the ONLY place that reads CoinDCX state)
    def _list_orders(self, status: str, stop_when_found: set[str] | None = None) -> list[dict[str, Any]]:
        rows = self.exchange.list_all_orders(status=status, stop_when_found=stop_when_found)
        return [row for row in rows if row.get("pair") == self.config.pair and row.get("id") is not None]

    def _refresh_position(self) -> None:
        positions = self.exchange.list_positions(self.config.pair)
        mine = next((p for p in positions if p.get("pair") == self.config.pair), {})
        self._position_qty, self._position_avg = Decimal(str(mine.get("active_pos", 0))), Decimal(str(mine.get("avg_price") or 0))
        self._position_id = str(mine["id"]) if mine.get("id") is not None else None

    def _build_snapshot(self, open_rows: list[dict[str, Any]], last_price: Decimal) -> RiskSnapshot:
        pos = self._position_qty
        long_notional = (pos * last_price * self.instrument.unit_contract_value * self.conversion_rate if pos > 0 else Decimal("0"))
        short_notional = (-pos * last_price * self.instrument.unit_contract_value * self.conversion_rate if pos < 0 else Decimal("0"))
        for order in open_rows:
            quantity = Decimal(str(order.get("remaining_quantity", order.get("total_quantity", 0))))
            order_notional = Decimal(str(order.get("price", 0))) * quantity * self.instrument.unit_contract_value * self.conversion_rate
            if str(order.get("side", "")).lower() == "buy":
                long_notional += order_notional
            else:
                short_notional += order_notional
        return RiskSnapshot(signed_position_quantity=pos, open_order_count=len(open_rows),
                            notional_inr=max(long_notional, short_notional),
                            long_notional_inr=long_notional, short_notional_inr=short_notional)

    def _sync(self, reason: str, current: Decimal) -> None:
        """One shared exchange snapshot per check: open orders (+ terminal rows only if one of ours vanished),
        and positions only when something changed or on the slow safety cadence. The same rows feed
        reconciliation, the duplicate-order cache and the risk snapshot."""
        open_rows = self._list_orders("open,partially_filled")
        tracked_open = {str(r["exchange_order_id"]) for r in self.db.orders()
                        if r["pair"] == self.config.pair and str(r["status"]).lower() in ("open", "partially_filled")}
        missing = tracked_open - {str(o["id"]) for o in open_rows}
        terminal = self._list_orders("filled,cancelled,rejected", stop_when_found=missing) if missing else []
        intents = self.orders.reconcile(open_rows + terminal)
        changed = bool(missing) or self.orders.last_reconcile_changes > 0
        if changed or reason in ("startup", "periodic", "forced"):
            self._refresh_position()
        self._pending.extend(i for i in intents if i not in self._pending)
        self._snapshot_cache = self._build_snapshot(open_rows, current)
        self._synced_at = time.monotonic()
        self._last_sync_reason = reason
        self._force_sync = False
        self._awaiting_reconcile = False          # the exchange state has now been re-read after any unknown outcome
        # Touch checks that find nothing back off exponentially, so a price hovering at a level cannot spam CoinDCX.
        self._touch_interval = (self.config.min_touch_sync_seconds if changed or reason != "touch"
                                else min(self._touch_interval * 2, self.config.sync_seconds))

    def _touched(self, current: Decimal) -> bool:
        """True if the reference price reached any resting order (with slack for the Binance/CoinDCX basis)."""
        tol = max(self.config.touch_tolerance_fraction, abs(self.prices.basis or Decimal("0")))
        for level in self.engine.open_levels():
            if level.side == "buy" and current <= level.price * (Decimal("1") + tol):
                return True
            if level.side == "sell" and current >= level.price * (Decimal("1") - tol):
                return True
        return False

    def _sync_reason(self, current: Decimal) -> str | None:
        if self._synced_at is None:
            return "startup"
        age = time.monotonic() - self._synced_at
        if self._force_sync and age >= min(2.0, self.config.min_touch_sync_seconds):
            return "forced"
        if age >= self.config.sync_seconds:
            return "periodic"
        if age >= self._touch_interval and self._touched(current):
            return "touch"
        return None

    def _check_basis(self) -> None:
        interval = 30.0 if self._price_gate else None
        if not self.prices.feed or not (self.prices.basis_due() if interval is None else self.prices.basis_due(interval)):
            return
        basis = self.prices.measure_basis()
        if basis is None:
            return
        if abs(basis) > self.config.max_basis_fraction:
            self._price_gate = f"CoinDCX/Binance price basis {basis:.3%} exceeds {self.config.max_basis_fraction:.3%}; new orders held."
            self.db.log("WARNING", "Price basis gate closed", {"basis": str(basis)})
        elif self._price_gate:
            self._price_gate = None
            self.db.log("INFO", "Price basis gate reopened", {"basis": str(basis)})

    def _advance_snapshot(self, snapshot: RiskSnapshot, intent: OrderIntent) -> RiskSnapshot:
        """Update a snapshot in-memory for an order we just placed ourselves.

        Placing one more resting limit order deterministically adds one open order
        and its notional on its own side; it cannot change the position quantity
        or any other account-wide figure. Re-deriving that locally lets a grid of
        many levels place all of its orders with one snapshot fetch per loop tick
        instead of a full list_positions + list_orders round trip after every
        single order, which is what made large grids slow to start.
        """
        order_notional = quote_notional_inr(intent.price, intent.quantity, self.instrument, self.conversion_rate)
        long_notional = snapshot.long_notional_inr or Decimal("0")
        short_notional = snapshot.short_notional_inr or Decimal("0")
        if intent.side == "buy":
            long_notional += order_notional
        else:
            short_notional += order_notional
        return replace(snapshot, open_order_count=snapshot.open_order_count + 1,
                       notional_inr=max(long_notional, short_notional),
                       long_notional_inr=long_notional, short_notional_inr=short_notional)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.stopped = False
        self._stop_event.clear()
        self.prices.start()
        self._thread = Thread(target=self._run, daemon=True, name="grid-bot")
        self.db.log("INFO", f"Bot started in {self.config.trading_mode.value} mode")
        self._thread.start()

    def shutdown(self) -> None:
        """Release the price-feed thread (call when a bot is removed for good)."""
        self.prices.stop()

    def _entries_allowed(self) -> bool:
        return not self.paused and self._price_gate is None and not self._awaiting_reconcile

    def _submit(self, intent: OrderIntent) -> None:
        """Place one order with the cached snapshot. After an unknown outcome nothing more is sent until a sync."""
        try:
            order_id = self.orders.submit(intent, self._snapshot_cache)
        except CoinDCXError:
            # The POST may or may not have reached CoinDCX: block further POSTs and re-read exchange state first.
            self._awaiting_reconcile, self._force_sync = True, True
            self._submit_failures += 1
            if self._submit_failures >= 3:
                self.paused = True
                self.last_error = "Order creation failed 3 times in a row; bot paused. Check the exchange and Resume."
                self.db.log("ERROR", self.last_error, {"intent": intent.__dict__})
                self._submit_failures = 0
            raise
        self._submit_failures = 0
        if order_id:
            self._snapshot_cache = self._advance_snapshot(self._snapshot_cache, intent)

    def _tick(self) -> None:
        quote = self.prices.get()                       # in-memory Binance tick; CoinDCX only as a throttled fallback
        current, self.price_quote, self.last_price = quote.price, quote, quote.price
        if isinstance(self.exchange, PaperExchange):
            self.exchange.advance(current)
        self._check_basis()
        reason = self._sync_reason(current)
        if reason:
            self._sync(reason, current)
        if self._entries_allowed():
            if not self._initialised:
                # Place the initial grid against CoinDCX's own price (1 throttled call), not the Binance reference.
                reference = self.prices.coindcx_reference() if self.prices.feed else current
                if self.config.grid_mode in (GridMode.LONG, GridMode.SHORT):
                    self._initialised = self._bootstrap_inventory(reference, self._snapshot_cache)
                else:
                    for intent in self.engine.initial_intents(reference):
                        self._submit(intent)
                    self._initialised = True
        self._apply_tpsl(current)
        while self._pending and self._entries_allowed() and self._initialised:
            self._submit(self._pending[0])
            self._pending.pop(0)                         # removed only after the order is safely recorded
        self._write_status(current)

    def _write_status(self, current: Decimal) -> None:
        """Journal a status row on change or every 60 s, not on every local tick."""
        snap = self._snapshot_cache
        key = (str(snap.signed_position_quantity), snap.open_order_count)
        last_at, last_key = self._last_status_write
        if key != last_key or time.monotonic() - last_at >= 60:
            self.db.snapshot("status", {"price": str(current), "position": key[0], "open_orders": key[1]})
            self._last_status_write = (time.monotonic(), key)

    def _run(self) -> None:
        streak = 0
        while not self._stop_event.is_set():
            wait = self.config.poll_seconds
            try:
                self.last_heartbeat = time.time()
                self._tick()
                streak = 0
            except RiskViolation as exc:
                self.paused = True
                self.last_error = str(exc)
                self.db.log("WARNING", "Risk gate paused new orders", {"reason": str(exc)})
            except Exception as exc:
                self.last_error = str(exc)
                self.db.log("ERROR", "Bot loop error; no automatic resubmission performed", {"error": str(exc)})
                # Exponential backoff with jitter (also covers HTTP 429): never a tight retry loop.
                streak += 1
                wait = min(self.config.poll_seconds * (2 ** streak), 60.0) + random.uniform(0, 0.25 * self.config.poll_seconds)
            self._stop_event.wait(wait)

    def _bootstrap_inventory(self, current: Decimal, snapshot: RiskSnapshot) -> bool:
        """Open the starting inventory at market, then place both grid sides (Binance/Bitget style).

        Long, range 1-2, 10 grids, price 1.5: levels 1.6..2.0 are above price, so five units are
        bought at market and five limit sells go on those levels; limit buys sit on 1.1..1.4.
        Short is the mirror: units are sold at market for the levels below price, with limit
        buy-backs on them and limit sells above. A fill restores its neighbouring order.
        """
        long = self.config.grid_mode is GridMode.LONG
        sign, entry_side, close_side = (1, "buy", "sell") if long else (-1, "sell", "buy")
        label = "long" if long else "short"
        held = sign * snapshot.signed_position_quantity            # >0: same direction as this grid
        if not self._inv_entry_sent:
            if snapshot.signed_position_quantity != 0 and not (self._resuming and held > 0):
                raise RuntimeError(f"Refusing to start a {label} grid: this market already has a "
                                   f"{'conflicting ' if self._resuming else ''}position. Close it on CoinDCX first.")
            required = self.engine.initial_market_quantity(current)
            to_open = round_to_increment(max(Decimal("0"), required - max(held, Decimal("0"))), self.instrument.quantity_increment)
            self._inv_expected = max(held, Decimal("0")) + to_open
            self._inv_entry_sent, self._inv_entry_price = True, current
            if to_open >= self.instrument.min_quantity:
                entry = OrderIntent(-1, entry_side, current, to_open, f"initial {label} grid inventory")
                self.risk.check(entry, snapshot)
                # A timed-out POST can reach CoinDCX. Mark it first so the next loop reconciles the
                # position instead of issuing a duplicate market order.
                self._force_sync = True
                self.exchange.place_market_order(pair=self.config.pair, side=entry_side, quantity=to_open, leverage=self.config.leverage)
                self.db.snapshot(f"{label}_market_entry", {"pair": self.config.pair, "price": str(current), "quantity": str(to_open)})
                self.db.log("INFO", f"{label.title()} grid inventory submitted; waiting for full position confirmation",
                            {"price": str(current), "quantity": str(to_open)})
                return False
        if held < self._inv_expected:
            self._force_sync = True      # re-read the position shortly (bounded by the forced-sync gap), not every tick
            self.db.log("WARNING", f"{label.title()} grid inventory is not fully filled; grid orders remain blocked",
                        {"position": str(snapshot.signed_position_quantity), "expected": str(self._inv_expected)})
            return False
        actual_entry_price = self._position_avg or self._inv_entry_price or current   # avg price from the latest sync
        for raw_intent in self.engine.initial_intents(current):
            intent = raw_intent
            if raw_intent.side == close_side:   # these orders close the inventory opened above
                intent = OrderIntent(raw_intent.level_index, raw_intent.side, raw_intent.price, raw_intent.quantity,
                                     f"initial {label} grid inventory exit", entry_order_id=f"initial-{label}-market-entry",
                                     entry_price=actual_entry_price, entry_side=entry_side)
            self._submit(intent)
        self._resuming = False
        self.db.log("INFO", f"Initial {label} grid is confirmed; all grid orders placed", {"entry_price": str(actual_entry_price)})
        return True

    def _apply_tpsl(self, current: Decimal) -> None:
        if self._tpsl_set or self.config.stop_loss_price is None:
            return
        # Uses the position cached by the last sync; no extra CoinDCX request.
        if self._position_qty == 0 or self._position_id is None:
            return
        quantity = self._position_qty
        stop_loss, take_profit = self.config.stop_loss_price, self.config.take_profit_price
        valid = ((quantity > 0 and stop_loss < current < take_profit) or
                 (quantity < 0 and take_profit < current < stop_loss))
        self._tpsl_set = True
        if not valid:
            self.paused = True
            self.db.log("ERROR", "SL/TP direction is invalid for the open position; new orders paused", {"price": str(current)})
            return
        if self.config.trading_mode is TradingMode.LIVE:
            self.exchange.create_position_tpsl(position_id=self._position_id, stop_loss_price=stop_loss, take_profit_price=take_profit)
        self.db.log("INFO", "Full-position SL/TP request accepted", {"stop_loss": str(stop_loss), "take_profit": str(take_profit)})

    def pause(self) -> None:
        self.paused = True
        self.db.log("WARNING", "New orders paused")

    def resume(self) -> None:
        if not self.stopped:
            self.paused = False
            self.db.log("INFO", "New orders resumed")

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self.stopped)

    @property
    def run_state(self) -> str:
        if not self.is_running:
            return "stopped"
        return "paused" if self.paused else "running"

    def stop(self, cancel_open_orders: bool = True) -> None:
        self.paused, self.stopped = True, True
        self._stop_event.set()
        # Let an in-flight loop iteration finish first so it cannot place orders after we cancel.
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self.config.poll_seconds + 15)
        if cancel_open_orders:
            self.orders.cancel_all()
        self.db.log("WARNING", "Bot stopped", {"cancel_open_orders": cancel_open_orders, "bot": self.bot_id})

    def restart(self) -> None:
        """Resume a STOPPED bot: re-place the grid around the current price and run again.

        Orders were cancelled by Stop, so levels are reset. An existing position is kept (never
        auto-closed); risk caps still apply to every new order.
        """
        if self.is_running:
            return
        if self._thread and self._thread.is_alive():
            raise RuntimeError("Previous loop is still shutting down; try again in a few seconds.")
        for level in self.engine.levels:
            level.state, level.side, level.exchange_order_id, level.filled_quantity = LevelState.IDLE, None, None, Decimal("0")
        self._initialised = False
        self._pending.clear()
        self._synced_at, self._force_sync, self._awaiting_reconcile = None, False, False   # fresh exchange read before re-placing
        self._touch_interval = self.config.min_touch_sync_seconds
        self.orders.invalidate_cache()
        if self.config.grid_mode in (GridMode.LONG, GridMode.SHORT):
            # Keep any inventory already held; the bootstrap tops it up to what the current price needs.
            self._inv_entry_sent, self._inv_expected, self._inv_entry_price, self._resuming = False, Decimal("0"), None, True
        self.last_error = None
        self.paused, self.stopped = False, False
        self.db.log("INFO", "Bot resumed", {"bot": self.bot_id})
        self.start()

    def pnl(self) -> dict[str, Decimal]:
        """Realized (this bot run's completed cycles) and unrealized (open position vs last price), in INR."""
        realized = Decimal(str(self.db.cycle_summary_since(self.config.pair, self.started_at)["realized_profit_inr"]))
        unrealized = Decimal("0")
        if self.last_price is not None and self._position_qty != 0 and self._position_avg > 0:
            unrealized = ((self.last_price - self._position_avg) * self._position_qty
                          * self.instrument.unit_contract_value * self.conversion_rate)
        return {"realized_inr": realized, "unrealized_inr": unrealized}

    def _clients(self) -> list[CoinDCXClient]:
        clients = [self._price_client]
        if isinstance(self.exchange, CoinDCXClient) and self.exchange is not self._price_client:
            clients.append(self.exchange)
        return clients

    def coindcx_calls_last_minute(self) -> int:
        return sum(c.calls_last_minute() for c in self._clients())

    def coindcx_call_counts(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for client in self._clients():
            for key, value in client.call_counts.items():
                totals[key] = totals.get(key, 0) + value
        return totals

    def status(self) -> dict[str, Any]:
        pnl = self.pnl()
        return {"bot_id": self.bot_id, "run_state": self.run_state, "realized_profit_inr": str(pnl["realized_inr"]),
                "unrealized_pnl_inr": str(pnl["unrealized_inr"]), "position_quantity": str(self._position_qty),
                "position_avg_price": str(self._position_avg), "last_price": str(self.last_price) if self.last_price else None,
                "mode": self.config.trading_mode.value, "pair": self.config.pair, "paused": self.paused, "stopped": self.stopped,
                "last_error": self.last_error, "last_heartbeat": self.last_heartbeat,
                "price_source": self.price_quote.source if self.price_quote else None,
                "price_age_seconds": round(self.price_quote.age_seconds, 1) if self.price_quote else None,
                "basis": f"{self.prices.basis:.4%}" if self.prices.basis is not None else None, "price_gate": self._price_gate,
                "last_sync_reason": self._last_sync_reason,
                "last_sync_age_seconds": round(time.monotonic() - self._synced_at, 1) if self._synced_at else None,
                "pending_orders": len(self._pending), "coindcx_calls_last_minute": self.coindcx_calls_last_minute(),
                "coindcx_call_counts": self.coindcx_call_counts(),
                "levels": [{"index": x.index, "price": str(x.price), "state": x.state.value, "side": x.side, "order_id": x.exchange_order_id} for x in self.engine.levels],
                "estimated_cycle_net_inr": str(self.engine.estimated_cycle_net_inr()), "cycles": self.db.cycle_summary(self.config.pair),
                "orders": self.db.orders(), "events": self.db.events(50)}
