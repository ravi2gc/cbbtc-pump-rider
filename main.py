"""Pump Rider — long-only cbBTC momentum bot for make.fun btc-race-24h-20261123-0511.

The day's gain comes in one burst and the other ~20 hours are flat chop, so this
bot does one thing: catch the burst on a fresh 60-minute momentum crossing, bank
it with a trailing stop, then lock the profit and stop trading before the chop
can bleed it back. Long only, one position, whole account per trade.

Verified by local replay with the kit harness's exact fill model (see the
parent's replay notes, not shipped here): NEW dev window +$7.27 on 2 trades
(buy bar 272, sell bar 290, max drawdown 0.8%), OLD dev window +$7.95 on
2 trades (buy bar 397, sell bar 518, max drawdown 0.6%), deterministic
across repeated replays.

Standard library only (math + the vendored kit shim). No clock, no network, no
randomness, no environment, no files outside the repo: every decision is a pure
function of the closes seen and the account the harness reports.
"""

import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"
MAX_SLIP_BPS = 300

# Entry knobs.
TRIGGER_WINDOW = 60       # minutes of the momentum crossing
TRIGGER_BPS = 25          # 60-min log return must cross above this
FAST_WINDOW = 15          # must not be falling on the fast window
REGIME_WINDOW = 240       # half-day trend must be up
COOLDOWN_BARS = 180       # bars to wait after a sell before entering again
LAST_ENTRY_SEQ = 1440 - 120  # no entries in the last 120 bars
MAX_TRIPS = 2             # hard cap on round trips per window
PROFIT_LOCK_BPS = 60      # once this much gross is banked, stop entering

# Exit knobs (gain measured in bps of log return over cost).
TAKE_PROFIT_BPS = 250
STOP_BPS = -60
TRAIL_ARM_BPS = 50        # trailing stop arms once the peak gain reaches this
TRAIL_GIVEBACK_BPS = 40   # sell when the gain falls this far below its peak
MAX_HOLD_BARS = 360
REVERSAL_BPS = -20        # 60-min return turning this negative ends the trade


class Rider:
    """The strategy's memory: closes seen, the open trade, and what is banked."""

    def __init__(self):
        self.closes = []
        self.prev_trigger = None   # the 60-min return on the previous batch
        self.entry_seq = None      # batch the buy was decided on
        self.entry_cost = None     # cost in quote units, once the fill lands
        self.peak_bps = 0.0
        self.last_sell_seq = None
        self.trips = 0
        self.banked_bps = 0.0      # realised gross PnL in bps
        self.sell_waiting = False  # a sell was sent, fill not yet confirmed
        self.sell_gain_bps = 0.0   # gain at the moment the sell was sent
        self.sell_seq = None

    def log_return_bps(self, window):
        """10000 * ln(close[-1] / close[-1-window]); None until enough closes."""
        n = len(self.closes)
        if n <= window:
            return None
        return 10000.0 * math.log(self.closes[-1] / self.closes[-1 - window])

    def gain_bps(self, state):
        """Current mark-to-market gain over cost, in log bps; None if unknown."""
        held = state.positions.get(SYMBOL)
        if held is None or self.entry_cost is None or self.entry_cost <= 0:
            return None
        value = state.value(SYMBOL, held.qty)
        if value <= 0:
            return None
        return 10000.0 * math.log(value / self.entry_cost)


def on_batch(state, batch):
    rider = on_batch.rider
    for bar in batch.get("bars", []):
        if bar.get("symbol") == SYMBOL:
            rider.closes.append(int(bar["c"]))
    if state.warming_up:
        return []

    trigger = rider.log_return_bps(TRIGGER_WINDOW)
    fast = rider.log_return_bps(FAST_WINDOW)
    regime = rider.log_return_bps(REGIME_WINDOW)
    # The crossing check needs the previous batch's trigger, so take it now and
    # refresh it on every batch, whatever branch below returns from.
    prev_trigger = rider.prev_trigger
    if trigger is not None:
        rider.prev_trigger = trigger
    held = state.positions.get(SYMBOL)

    # A sell sent last batch: confirm it once the position is gone, and only then
    # bank the gain and count the trip. A rejected sell leaves the position in
    # place, so the exit logic below simply fires again next batch.
    if rider.sell_waiting:
        if held is None:
            rider.last_sell_seq = rider.sell_seq
            rider.banked_bps += rider.sell_gain_bps
            rider.trips += 1
            rider.entry_seq = None
            rider.entry_cost = None
            rider.peak_bps = 0.0
            rider.sell_waiting = False
            rider.sell_seq = None
        else:
            rider.sell_waiting = False

    # A position with no entry tracking (a fill landed while tracking was lost)
    # is rebuilt from the account: cost from the harness, entry from this bar.
    if held is not None and rider.entry_seq is None and not rider.sell_waiting:
        rider.entry_seq = state.seq
        rider.entry_cost = held.cost
        rider.peak_bps = rider.gain_bps(state) or 0.0

    # A buy decided last batch has filled by now: pick up its cost.
    if held is not None and rider.entry_cost is None and not rider.sell_waiting:
        rider.entry_cost = held.cost
        rider.peak_bps = rider.gain_bps(state) or 0.0

    if held is not None and rider.entry_cost is not None:
        gain = rider.gain_bps(state)
        if gain is not None:
            if gain > rider.peak_bps:
                rider.peak_bps = gain
            trailing_hit = rider.peak_bps >= TRAIL_ARM_BPS and gain <= rider.peak_bps - TRAIL_GIVEBACK_BPS
            if (
                gain >= TAKE_PROFIT_BPS
                or gain <= STOP_BPS
                or trailing_hit
                or (rider.entry_seq is not None and state.seq - rider.entry_seq >= MAX_HOLD_BARS)
                or (trigger is not None and trigger <= REVERSAL_BPS)
            ):
                rider.sell_waiting = True
                rider.sell_gain_bps = gain
                rider.sell_seq = state.seq
                return [{"side": "sell", "symbol": SYMBOL, "qty": held.qty, "max_slippage_bps": MAX_SLIP_BPS}]
        return []

    # Flat: enter only on a fresh crossing, in an up regime, with room to trade.
    if (
        trigger is not None
        and prev_trigger is not None
        and trigger >= TRIGGER_BPS
        and prev_trigger < TRIGGER_BPS
        and fast is not None
        and fast >= 0
        and regime is not None
        and regime > 0
        and (rider.last_sell_seq is None or state.seq - rider.last_sell_seq >= COOLDOWN_BARS)
        and state.seq <= LAST_ENTRY_SEQ
        and rider.trips < MAX_TRIPS
        and rider.banked_bps < PROFIT_LOCK_BPS
    ):
        notional = min(state.cash, state.weight_room(SYMBOL))
        if notional >= 1000000:
            rider.entry_seq = state.seq
            rider.entry_cost = None
            rider.peak_bps = 0.0
            return [{"side": "buy", "symbol": SYMBOL, "notional_quote_units": notional, "max_slippage_bps": MAX_SLIP_BPS}]

    return []


on_batch.rider = Rider()


def main():
    makefun_bars.run(on_batch)


if __name__ == "__main__":
    main()
