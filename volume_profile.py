"""
volume_profile.py — Fixed Range Volume Profile for NGAO Scalper
================================================================
Direct Python translation of the ApexFlow Pine Script logic.

Computes:
  - Volume bins across the lookback range (accurate mode: spreads
    each bar's volume across all rows its H-L range touches)
  - POC  — Point of Control (highest volume price)
  - VAH  — Value Area High (upper bound of 70% volume zone)
  - VAL  — Value Area Low  (lower bound of 70% volume zone)
  - Value Area Zone (VAL → VAH)
  - High Volume Nodes (HVN) — price levels with above-average volume
  - Low Volume Nodes  (LVN) — price levels with below-average volume

How it integrates with the bot:
  APA Engine:
    - POC near entry zone       → +0.5 confluence bonus
    - Entry AT VAH/VAL          → +1.0 (price at institutional boundary)
    - TP target at POC/VAH/VAL  → +0.5 (structural TP alignment)
    - Entry in LVN              → -1.0 penalty (price moves fast through LVN,
                                    no support — avoid entries here)

  ICT Engine:
    - IPDA level aligns with POC → +0.5 bonus
    - OTE zone overlaps with VA  → +0.5 bonus (double confluence)
    - Silver Bullet FVG at VAL/VAH → +0.5 bonus

  Trade Management:
    - POC used as intermediate TP between TP1 and TP2
    - VAH/VAL used as final TP3 target when closer than IPDA level
    - If price stalls at POC with no momentum → early exit signal

Usage:
    from volume_profile import VolumeProfile

    vp = VolumeProfile(df_h1, lookback=100, n_rows=24, va_pct=70.0)
    print(vp.poc, vp.vah, vp.val)
    print(vp.score_entry(entry_price=1.0850, direction=1))
"""

from __future__ import annotations
import numpy as np
import pandas as pd
from dataclasses import dataclass, field


# ─────────────────────────────────────────────────────────────────────────────
#  CONFIGURATION DEFAULTS  (mirror Pine Script inputs)
# ─────────────────────────────────────────────────────────────────────────────

VP_LOOKBACK  = 100     # bars (Pine: lookback)
VP_ROWS      = 24      # price buckets (Pine: n_rows)
VP_VA_PCT    = 70.0    # value area percentage (Pine: va_pct)
VP_ACCURATE  = True    # spread volume across bar range (Pine: accurate)

# Thresholds for HVN / LVN classification
HVN_RATIO    = 1.3     # row volume > mean * this → High Volume Node
LVN_RATIO    = 0.5     # row volume < mean * this → Low Volume Node

# Confluence scoring contributions
SCORE_POC_NEAR_ENTRY   =  0.5
SCORE_ENTRY_AT_VA_EDGE =  1.0
SCORE_TP_AT_LEVEL      =  0.5
SCORE_ENTRY_IN_LVN     = -1.0   # penalty
SCORE_IPDA_POC_ALIGN   =  0.5
SCORE_OTE_IN_VA        =  0.5
SCORE_SB_FVG_AT_EDGE   =  0.5

# Proximity threshold: price is "at" a VP level if within this many points
PROXIMITY_MULTIPLIER   = 3.0    # × symbol point size


# ─────────────────────────────────────────────────────────────────────────────
#  VOLUME PROFILE ENGINE
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class VolumeProfile:
    """
    Computes and stores the Fixed Range Volume Profile
    from the last `lookback` bars of an OHLCV DataFrame.

    DataFrame must have columns: open, high, low, close, tick_volume
    (MT5 data uses tick_volume — treated identically to volume in Pine)
    """

    # ── inputs ───────────────────────────────────────────────────────────
    df:        pd.DataFrame
    lookback:  int   = VP_LOOKBACK
    n_rows:    int   = VP_ROWS
    va_pct:    float = VP_VA_PCT
    accurate:  bool  = VP_ACCURATE

    # ── computed outputs (set in __post_init__) ───────────────────────────
    poc:       float = field(init=False)   # Point of Control price
    vah:       float = field(init=False)   # Value Area High
    val:       float = field(init=False)   # Value Area Low
    range_hi:  float = field(init=False)   # Highest high in lookback
    range_lo:  float = field(init=False)   # Lowest low in lookback
    bin_size:  float = field(init=False)   # Price per row
    poc_idx:   int   = field(init=False)   # POC row index
    vah_idx:   int   = field(init=False)   # VAH row index
    val_idx:   int   = field(init=False)   # VAL row index
    vol_bins:  np.ndarray = field(init=False)  # volume per row
    hvn_prices: list = field(init=False)   # High Volume Node prices
    lvn_prices: list = field(init=False)   # Low Volume Node prices
    valid:     bool  = field(init=False)   # False if insufficient data

    def __post_init__(self):
        self.valid = self._compute()

    def _compute(self) -> bool:
        """
        Port of the Pine Script main logic block.
        Returns True if computation succeeded.
        """
        # ── Step 1: effective lookback (Pine: lb = math.min(lookback, bar_index+1))
        lb = min(self.lookback, len(self.df))
        if lb < 5:
            self._set_defaults()
            return False

        window = self.df.iloc[-lb:].reset_index(drop=True)

        # ── Step 2: price range (Pine: rhi/rlo loop)
        rhi = float(window["high"].max())
        rlo = float(window["low"].min())
        self.range_hi = rhi
        self.range_lo = rlo

        # bsz = max((rhi - rlo) / n_rows, syminfo.mintick)
        price_range = rhi - rlo
        if price_range <= 0:
            self._set_defaults()
            return False

        bsz = price_range / self.n_rows
        self.bin_size = bsz

        # ── Step 3: volume bins (Pine: vb array)
        # Uses tick_volume if real volume unavailable (MT5 standard)
        vol_col = "tick_volume" if "tick_volume" in window.columns else "volume"
        vb = np.zeros(self.n_rows, dtype=float)

        for i in range(lb):
            row  = window.iloc[i]
            v    = float(row[vol_col]) if row[vol_col] > 0 else 0.0
            if v == 0:
                continue

            bh  = float(row["high"])
            bl  = float(row["low"])
            rng = bh - bl

            if self.accurate and rng > 0:
                # Accurate mode: spread volume proportionally across touched rows
                # Pine: for r = 0 to n_rows - 1 ... ov = overlap fraction
                for r in range(self.n_rows):
                    row_lo = rlo + r * bsz
                    row_hi = row_lo + bsz
                    ov = min(bh, row_hi) - max(bl, row_lo)
                    if ov > 0:
                        vb[r] += v * (ov / rng)
            else:
                # Fast mode: dump volume into HLC3 bucket
                tp  = (bh + bl + float(row["close"])) / 3.0
                idx = int(np.floor((tp - rlo) / bsz))
                idx = max(0, min(self.n_rows - 1, idx))
                vb[idx] += v

        self.vol_bins = vb

        # ── Step 4: POC (Pine: mvol = array.max(vb), poc_i = indexof)
        max_vol = float(np.max(vb))
        if max_vol == 0:
            self._set_defaults()
            return False

        poc_i = int(np.argmax(vb))
        poc_p = rlo + (poc_i + 0.5) * bsz   # row midpoint

        self.poc_idx = poc_i
        self.poc     = float(poc_p)

        # ── Step 5: Value Area (Pine: expand from POC until va_pct% captured)
        tgt = float(np.sum(vb)) * (self.va_pct / 100.0)
        acc = max_vol
        ui  = poc_i
        di  = poc_i

        while acc < tgt:
            uv = float(vb[ui + 1]) if ui < self.n_rows - 1 else 0.0
            dv = float(vb[di - 1]) if di > 0 else 0.0

            if uv + dv == 0.0:
                break

            # Pine: if uv >= dv → expand up, else → expand down
            if uv >= dv:
                ui  += 1
                acc += uv
            else:
                di  -= 1
                acc += dv

        vah_p = rlo + (ui + 1) * bsz   # Pine: (ui + 1) * bsz
        val_p = rlo + di * bsz          # Pine: di * bsz

        self.vah_idx = ui
        self.val_idx = di
        self.vah     = float(vah_p)
        self.val     = float(val_p)

        # ── Step 6: HVN / LVN classification
        mean_vol = float(np.mean(vb))
        self.hvn_prices = []
        self.lvn_prices = []

        for r in range(self.n_rows):
            row_mid = rlo + (r + 0.5) * bsz
            rv      = float(vb[r])
            if rv > mean_vol * HVN_RATIO:
                self.hvn_prices.append(float(row_mid))
            elif rv < mean_vol * LVN_RATIO:
                self.lvn_prices.append(float(row_mid))

        return True

    def _set_defaults(self):
        self.poc = 0.0; self.vah = 0.0; self.val = 0.0
        self.range_hi = 0.0; self.range_lo = 0.0
        self.bin_size = 0.0; self.poc_idx = 0
        self.vah_idx  = 0; self.val_idx = 0
        self.vol_bins = np.zeros(self.n_rows)
        self.hvn_prices = []; self.lvn_prices = []


    # ─────────────────────────────────────────────────────────────────────
    #  QUERY HELPERS
    # ─────────────────────────────────────────────────────────────────────

    def price_near_level(self, price: float, level: float,
                          sym_point: float) -> bool:
        """True if price is within PROXIMITY_MULTIPLIER × point of level."""
        if level == 0 or sym_point == 0:
            return False
        return abs(price - level) <= PROXIMITY_MULTIPLIER * sym_point * 10

    def price_in_value_area(self, price: float) -> bool:
        """True if price is inside the Value Area (VAL ≤ price ≤ VAH)."""
        return self.val <= price <= self.vah

    def price_at_va_edge(self, price: float, sym_point: float) -> str | None:
        """
        Returns 'vah', 'val', or None.
        'vah' = price is near Value Area High (resistance / short trigger)
        'val' = price is near Value Area Low  (support  / long  trigger)
        """
        if self.price_near_level(price, self.vah, sym_point):
            return "vah"
        if self.price_near_level(price, self.val, sym_point):
            return "val"
        return None

    def price_at_poc(self, price: float, sym_point: float) -> bool:
        return self.price_near_level(price, self.poc, sym_point)

    def price_in_lvn(self, price: float, sym_point: float) -> bool:
        """True if price is in a Low Volume Node (avoid entries here)."""
        for lvn in self.lvn_prices:
            if self.price_near_level(price, lvn, sym_point):
                return True
        return False

    def price_in_hvn(self, price: float, sym_point: float) -> bool:
        """True if price is in a High Volume Node (strong S/R)."""
        for hvn in self.hvn_prices:
            if self.price_near_level(price, hvn, sym_point):
                return True
        return False

    def nearest_tp_level(self, entry: float, direction: int) -> float:
        """
        Returns the nearest VP key level in the direction of trade.
        Used as TP target when closer than IPDA draw.
        Checks POC, VAH, VAL in order of proximity.
        """
        candidates = []
        for level in [self.poc, self.vah, self.val]:
            if level == 0:
                continue
            if direction == 1 and level > entry:
                candidates.append(level)
            elif direction == -1 and level < entry:
                candidates.append(level)

        if not candidates:
            return 0.0

        # Nearest in direction
        if direction == 1:
            return float(min(candidates))
        return float(max(candidates))

    def stall_at_poc(self, df_recent: pd.DataFrame,
                      sym_point: float, bars: int = 3) -> bool:
        """
        True if price has been hovering near POC for `bars` candles.
        Used as early exit signal when trade momentum stalls.
        """
        if not self.valid or self.poc == 0:
            return False
        recent = df_recent.tail(bars)
        hits = sum(
            1 for _, r in recent.iterrows()
            if self.price_near_level(float(r["close"]), self.poc, sym_point)
        )
        return hits >= bars - 1


    # ─────────────────────────────────────────────────────────────────────
    #  CONFLUENCE SCORING — called from APA and ICT engines
    # ─────────────────────────────────────────────────────────────────────

    def score_entry(self, entry_price: float, direction: int,
                     sym_point: float,
                     ote: dict | None = None,
                     ipda_level: float = 0.0,
                     sb_fvg: dict | None = None) -> tuple[float, dict]:
        """
        Score a proposed entry against Volume Profile levels.
        Returns (score_delta, breakdown).

        score_delta is ADDED to the engine's confluence score.
        Can be negative (LVN penalty).
        """
        if not self.valid:
            return 0.0, {"vp": "invalid"}

        score = 0.0
        bd    = {}

        # 1. Entry near POC → +0.5
        if self.price_at_poc(entry_price, sym_point):
            score += SCORE_POC_NEAR_ENTRY
            bd["poc_near_entry"] = SCORE_POC_NEAR_ENTRY

        # 2. Entry at VA edge (VAH for shorts, VAL for longs) → +1.0
        edge = self.price_at_va_edge(entry_price, sym_point)
        if edge:
            va_edge_ok = (direction == 1 and edge == "val") or \
                         (direction == -1 and edge == "vah")
            if va_edge_ok:
                score += SCORE_ENTRY_AT_VA_EDGE
                bd["va_edge_entry"] = SCORE_ENTRY_AT_VA_EDGE

        # 3. Entry in LVN → -1.0 penalty (thin volume = no S/R)
        if self.price_in_lvn(entry_price, sym_point):
            score += SCORE_ENTRY_IN_LVN
            bd["lvn_penalty"] = SCORE_ENTRY_IN_LVN

        # 4. Nearest TP level exists in trade direction → +0.5
        tp_level = self.nearest_tp_level(entry_price, direction)
        if tp_level > 0:
            score += SCORE_TP_AT_LEVEL
            bd["vp_tp_level"] = tp_level

        # 5. IPDA level aligns with POC → +0.5
        if ipda_level > 0 and self.price_near_level(ipda_level,
                                                      self.poc, sym_point):
            score += SCORE_IPDA_POC_ALIGN
            bd["ipda_poc_align"] = SCORE_IPDA_POC_ALIGN

        # 6. OTE zone overlaps with Value Area → +0.5
        if ote:
            ote_lo = ote.get("ote_low",  0.0)
            ote_hi = ote.get("ote_high", 0.0)
            # Overlap: OTE zone and VA zone share any range
            overlap = (ote_lo <= self.vah) and (ote_hi >= self.val)
            if overlap:
                score += SCORE_OTE_IN_VA
                bd["ote_va_overlap"] = SCORE_OTE_IN_VA

        # 7. Silver Bullet FVG midpoint at VAH/VAL → +0.5
        if sb_fvg:
            fvg_mid = sb_fvg.get("fvg_mid", 0.0)
            if fvg_mid > 0:
                if self.price_at_va_edge(fvg_mid, sym_point):
                    score += SCORE_SB_FVG_AT_EDGE
                    bd["sb_fvg_va_edge"] = SCORE_SB_FVG_AT_EDGE

        return score, bd

    def summary(self) -> dict:
        """Return a dict of all key levels for logging / Telegram."""
        return {
            "poc":      round(self.poc,      5),
            "vah":      round(self.vah,      5),
            "val":      round(self.val,      5),
            "range_hi": round(self.range_hi, 5),
            "range_lo": round(self.range_lo, 5),
            "hvn_count":len(self.hvn_prices),
            "lvn_count":len(self.lvn_prices),
            "valid":    self.valid,
        }


# ─────────────────────────────────────────────────────────────────────────────
#  MULTI-TIMEFRAME VOLUME PROFILE BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def build_vp_stack(symbol_data: dict,
                   sym_point:   float) -> dict:
    """
    Build Volume Profiles on H1 and M15 simultaneously.
    H1  VP (100 bars) → macro reference levels
    M15 VP (100 bars) → micro entry confirmation levels

    Returns:
      {
        "h1":  VolumeProfile | None,
        "m15": VolumeProfile | None,
      }
    """
    result = {"h1": None, "m15": None}

    df_h1  = symbol_data.get("h1")
    df_m15 = symbol_data.get("m15")

    if df_h1 is not None and len(df_h1) >= 20:
        result["h1"] = VolumeProfile(
            df=df_h1, lookback=VP_LOOKBACK,
            n_rows=VP_ROWS, va_pct=VP_VA_PCT, accurate=VP_ACCURATE
        )

    if df_m15 is not None and len(df_m15) >= 20:
        result["m15"] = VolumeProfile(
            df=df_m15, lookback=VP_LOOKBACK,
            n_rows=VP_ROWS, va_pct=VP_VA_PCT, accurate=VP_ACCURATE
        )

    return result


def get_vp_confluence(
    vp_stack:    dict,
    entry_price: float,
    direction:   int,
    sym_point:   float,
    ote:         dict | None = None,
    ipda_level:  float = 0.0,
    sb_fvg:      dict | None = None,
) -> tuple[float, dict]:
    """
    Score entry against both H1 and M15 volume profiles.
    H1 VP gets full weight. M15 VP gets 0.5× weight (confirming role).

    Returns (total_vp_score, breakdown).
    """
    total = 0.0
    breakdown = {}

    vp_h1  = vp_stack.get("h1")
    vp_m15 = vp_stack.get("m15")

    if vp_h1 and vp_h1.valid:
        h1_score, h1_bd = vp_h1.score_entry(
            entry_price, direction, sym_point, ote, ipda_level, sb_fvg
        )
        total += h1_score
        breakdown["h1_vp"] = {"score": h1_score, **h1_bd}
        breakdown["h1_levels"] = vp_h1.summary()

    if vp_m15 and vp_m15.valid:
        m15_score, m15_bd = vp_m15.score_entry(
            entry_price, direction, sym_point
        )
        total += m15_score * 0.5   # M15 is confirming, not primary
        breakdown["m15_vp"] = {"score": m15_score * 0.5, **m15_bd}
        breakdown["m15_levels"] = vp_m15.summary()

    return total, breakdown


def get_vp_tp_target(vp_stack: dict, entry: float,
                      direction: int) -> float:
    """
    Get the nearest VP key level as TP target.
    Prefers H1 VP; falls back to M15 VP.
    Returns 0.0 if none found.
    """
    for key in ("h1", "m15"):
        vp = vp_stack.get(key)
        if vp and vp.valid:
            tp = vp.nearest_tp_level(entry, direction)
            if tp > 0:
                return tp
    return 0.0


def check_poc_stall(vp_stack: dict, df_m5: pd.DataFrame,
                     sym_point: float) -> bool:
    """
    True if price is stalling at the H1 POC for 3+ consecutive bars.
    Used as early exit signal.
    """
    vp_h1 = vp_stack.get("h1")
    if vp_h1 and vp_h1.valid:
        return vp_h1.stall_at_poc(df_m5, sym_point)
    return False
