"""Simulated exchange with the same method names the bot uses on CoinDCXClient.

Orders fill against the live reference price (limit buys when price <= limit, limit sells when price >= limit),
positions net like CoinDCX's single signed position per pair. Every method also increments a *simulated* call
counter, so paper mode shows exactly how many CoinDCX requests live mode would make.
"""
from __future__ import annotations

from collections import Counter, deque
from decimal import Decimal
from threading import RLock
import time
from typing import Any, Callable

from coindcx_client import CoinDCXError, Instrument


class PaperExchange:
    def __init__(self, *, price_fn: Callable[[str], Decimal], instrument: Instrument,
                 balance: Decimal = Decimal("1000"), fee_rate: Decimal = Decimal("0.0004")):
        self._price, self.instrument = price_fn, instrument
        self.balance, self.fee_rate = balance, fee_rate
        self.realized = Decimal(0)
        self.fees = Decimal(0)
        self.orders: dict[str, dict[str, Any]] = {}
        self.position: dict[str, dict[str, Any]] = {}
        self._seq = 0
        self._lock = RLock()
        self.call_counts: Counter[str] = Counter()
        self._call_times: deque[float] = deque()

    # ------------------------------------------------------------------ accounting
    def _count(self, name: str) -> None:
        now = time.monotonic()
        self.call_counts[f"[paper] {name}"] += 1
        self._call_times.append(now)
        while self._call_times and now - self._call_times[0] > 60:
            self._call_times.popleft()

    def calls_last_minute(self) -> int:
        now = time.monotonic()
        while self._call_times and now - self._call_times[0] > 60:
            self._call_times.popleft()
        return len(self._call_times)

    # ------------------------------------------------------------------ simulation core
    def _apply_fill(self, pair: str, side: str, qty: Decimal, price: Decimal, leverage: int) -> None:
        pos = self.position.get(pair) or {"qty": Decimal(0), "avg": Decimal(0), "leverage": leverage}
        signed = qty if side == "buy" else -qty
        q0 = pos["qty"]
        if q0 == 0 or (q0 > 0) == (signed > 0):
            new = q0 + signed
            pos["avg"] = (abs(q0) * pos["avg"] + abs(signed) * price) / abs(new)
        else:
            closing = min(abs(q0), abs(signed))
            self.realized += (price - pos["avg"]) * closing * (1 if q0 > 0 else -1)
            new = q0 + signed
            if new == 0:
                pos["avg"] = Decimal(0)
            elif (new > 0) != (q0 > 0):
                pos["avg"] = price
        pos["qty"], pos["leverage"] = new, leverage
        self.position[pair] = pos
        self.fees += abs(signed) * price * self.fee_rate

    def _match(self, pair: str, only: str | None = None) -> None:
        """Fill resting limit orders the market has crossed. A resting order fills at ITS limit price (the market moved
        through it); an order that is marketable the moment it is placed (``only``) fills at the better market price."""
        market = self._price(pair)
        for order in list(self.orders.values()):
            if order["status"] != "open" or order["pair"] != pair or (only and order["id"] != only):
                continue
            if order["side"] == "buy" and market <= order["price"]:
                fill_price = min(order["price"], market) if only else order["price"]
            elif order["side"] == "sell" and market >= order["price"]:
                fill_price = max(order["price"], market) if only else order["price"]
            else:
                continue
            order["status"] = "filled"
            self._apply_fill(pair, order["side"], order["qty"], fill_price, order["leverage"])

    def _check_size(self, qty: Decimal) -> None:
        if qty < self.instrument.min_quantity or qty <= 0:
            raise CoinDCXError(f"[paper] quantity {qty} below minimum {self.instrument.min_quantity}", transient=False)

    def _new_order(self, pair: str, side: str, kind: str, price: Decimal, qty: Decimal, leverage: int) -> str:
        self._seq += 1
        oid = f"paper-{self._seq}"
        self.orders[oid] = {"id": oid, "pair": pair, "side": side, "order_type": kind, "price": price, "qty": qty,
                            "leverage": leverage, "status": "open"}
        return oid

    # ------------------------------------------------------------------ reads
    def list_open_orders(self, pair: str) -> list[dict[str, Any]]:
        with self._lock:
            self._count("list_open_orders")
            self._match(pair)
            return [{"id": o["id"], "pair": pair, "side": o["side"], "order_type": o["order_type"], "price": str(o["price"]),
                     "total_quantity": str(o["qty"]), "status": "open"}
                    for o in self.orders.values() if o["status"] == "open" and o["pair"] == pair]

    def list_positions(self, pair: str) -> list[dict[str, Any]]:
        with self._lock:
            self._count("list_positions")
            self._match(pair)
            pos = self.position.get(pair)
            if not pos or pos["qty"] == 0:
                return []
            return [{"id": f"paper-pos-{pair}", "pair": pair, "active_pos": str(pos["qty"]), "avg_price": str(pos["avg"]),
                     "margin_type": "isolated", "leverage": pos["leverage"]}]

    def available_margin(self) -> Decimal:
        with self._lock:
            self._count("available_margin")
            used = sum((abs(p["qty"]) * p["avg"] / p["leverage"] for p in self.position.values() if p["qty"]), Decimal(0))
            return self.balance + self.realized - self.fees - used

    # ------------------------------------------------------------------ writes
    def place_limit_order(self, *, pair: str, side: str, price: Decimal, quantity: Decimal, leverage: int,
                          margin_type: str = "isolated") -> dict[str, Any]:
        with self._lock:
            self._count("place_limit_order")
            self._check_size(quantity)
            oid = self._new_order(pair, side, "limit_order", price, quantity, leverage)
            self._match(pair, only=oid)
            return {"orders": [{"id": oid, "status": "open"}]}

    def place_market_order(self, *, pair: str, side: str, quantity: Decimal, leverage: int,
                           margin_type: str = "isolated") -> dict[str, Any]:
        with self._lock:
            self._count("place_market_order")
            self._check_size(quantity)
            price = self._price(pair)
            oid = self._new_order(pair, side, "market_order", price, quantity, leverage)
            self.orders[oid]["status"] = "filled"
            self._apply_fill(pair, side, quantity, price, leverage)
            return {"orders": [{"id": oid, "status": "filled"}]}

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        with self._lock:
            self._count("cancel_order")
            order = self.orders.get(order_id)
            if not order or order["status"] != "open":
                raise CoinDCXError(f"[paper] order {order_id} is not open", transient=False)
            order["status"] = "cancelled"
            return {"message": "success"}

    def exit_position(self, position_id: str) -> dict[str, Any]:
        with self._lock:
            self._count("exit_position")
            pair = position_id.removeprefix("paper-pos-")
            pos = self.position.get(pair)
            if pos and pos["qty"]:
                side = "sell" if pos["qty"] > 0 else "buy"
                self._apply_fill(pair, side, abs(pos["qty"]), self._price(pair), pos["leverage"])
            return {"message": "success"}
