from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP, ROUND_HALF_UP

class LadderError(ValueError): pass
@dataclass(frozen=True)
class Level: index:int; price:Decimal; margin:Decimal; quantity:Decimal
@dataclass(frozen=True)
class Summary: total_margin:Decimal; warnings:list[str]
def _round(v, inc, mode=ROUND_DOWN): return (v/inc).to_integral_value(rounding=mode)*inc
def build_ladder(entry, cfg, instrument):
    out=[]; price=Decimal(entry)
    for i in range(cfg.max_safety_orders+1):
        margin=cfg.base_margin*(cfg.multiplier**i); qty=_round(margin*cfg.leverage/price,instrument.quantity_increment)
        rounded=_round(price,instrument.price_increment,ROUND_HALF_UP); out.append(Level(i,rounded,margin,qty)); price=rounded*(Decimal(1)-cfg.step_pct/100)
    return out
def average_entry(levels):
    return sum((x.price*x.quantity for x in levels),Decimal(0))/sum((x.quantity for x in levels),Decimal(0))
def tp_price(avg,cfg,inst): return _round(avg*(Decimal(1)+cfg.tp_pct/100),inst.price_increment,ROUND_UP)
def stop_price(levels,cfg,inst): return _round(levels[-1].price*(Decimal(1)-cfg.sl_pct/100),inst.price_increment,ROUND_HALF_UP)
def validate_ladder(levels,inst):
    if any(x.quantity < inst.min_quantity or x.price*x.quantity < inst.min_notional for x in levels): raise LadderError("Order is below exchange minimum")
def summarize(levels,cfg,inst):
    return Summary(sum((x.margin for x in levels),Decimal(0)), ["Stop price may be near liquidation" ] if cfg.sl_pct >= Decimal(10) and cfg.leverage >= 20 else [])
def plan_rows(levels,cfg,inst):
    total=Decimal(0); rows=[]
    for x in levels:
        total+=x.margin; rows.append({"Order":"Base" if x.index==0 else f"Safety #{x.index}","Price":x.price,"Margin":x.margin,"Qty":x.quantity,"Cum. margin":total})
    return rows
