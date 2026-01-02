# Spot–Perp Convergence Arbitrage (ADA example)
> Buy ADA spot on Exchange A + short ADAUSDT perpetual on Exchange B, then close both when prices converge.

This README explains **what the strategy is**, **why it works**, **what risks exist**, and a **step-by-step execution checklist** you can follow.

---

## 1) What this strategy is (in one sentence)

You **lock a temporary price difference** between a coin’s **spot price** and its **perpetual futures (perp) price**, while staying **direction-neutral** (you don’t care if ADA goes up or down).

---

## 2) The core idea: hedge the coin, trade the spread

You open **two positions**:

- **Spot leg (long):** you buy the coin (you own ADA).
- **Perp leg (short):** you short the perp (synthetic exposure opposite direction).

If both legs are the **same size**, your net exposure to ADA’s direction is approximately **zero**:

- Spot: `+Q ADA`
- Perp: `-Q ADA`
- Net: `0 ADA`

So your PnL is dominated by **the difference between the two markets**, not ADA’s direction.

---

## 3) Why spot and perps tend to converge

Perpetual futures are designed to track spot. When they deviate, several forces push them back:

### A) Funding rate mechanism (main driver)
Perps have a **funding rate** paid periodically (often every 8 hours).  
- If perp price is **above** spot, funding is typically **positive** and **longs pay shorts**.
- That makes it expensive to stay long and attractive to be short.
- This pressure pushes perp price **down toward spot** (or spot up toward perp).

### B) Arbitrage capital
Other traders do similar hedged trades. Their activity:
- sells perps when perps are rich
- buys spot (or vice versa)
which mechanically reduces the gap.

### C) Market making / liquidity
Perps are usually extremely liquid; mispricings get competed away.

Bottom line: **big gaps don’t usually persist**.

---

## 4) When this strategy makes money

The strategy wins when:
- You enter when **perp is “rich”** (perp price > spot price), then
- The spread **shrinks** (converges), and you close both legs.

### Profit comes from:
- Selling high (shorting perps high) and buying back lower
- Buying spot lower and selling it slightly higher (or losing a bit less than perp gains)
- Potentially collecting **funding** if it’s positive while you’re short

You do **not** need ADA to go up or down — you need the **spread** to compress.

---

## 5) The basic trade directions

There are two symmetric cases:

### Case 1: Perp is higher than spot (common in bullish periods)
**Direction:**
1) Buy ADA spot
2) Short ADAUSDT perp
3) Wait for convergence
4) Close both

### Case 2: Perp is lower than spot (more common in stress / bearish periods)
**Direction:**
1) Short ADA spot (or borrow/short via margin) — often harder for retail
2) Long ADAUSDT perp
3) Wait for convergence
4) Close both

Most retail traders do **Case 1** because shorting spot is often restricted/expensive.

---

## 6) Fees and costs you must beat

This is not “free money.” You must exceed:

- Spot trading fees (entry + exit)
- Perp trading fees (entry + exit)
- Funding payments (could be positive or negative depending on regime)
- Slippage / spread (both venues)
- Borrow costs (only if you short spot in Case 2)

### Rule of thumb
Only take trades where expected convergence is comfortably larger than:
`all fees + worst-case slippage + at least one funding interval buffer`

---

## 7) Risks (read this carefully)

### A) Basis risk (spread may widen first)
The spread can move against you before converging. You can still be hedged directionally but your mark-to-market can dip.

### B) Execution risk
If you enter one leg and fail the other (partial fill), you temporarily become directional. Use limits, and prefer deep liquidity.

### C) Funding risk
Funding can flip sign. If you hold too long and funding becomes unfavorable, it can eat the edge.

### D) Liquidation / margin risk
The perp short uses margin. If the price spikes and you’re under-margined, you can get liquidated even though the spot leg is profitable. Manage leverage conservatively.

### E) Venue / transfer / operational risk
You’re exposed to exchange availability, downtime, and withdrawal restrictions (though this strategy avoids on-chain transfers for the core edge).

---

## 8) Step-by-step execution checklist (retail-friendly, Case 1)

### Preconditions
- You have funds on both exchanges (or at least margin available on the perp exchange).
- You are using the **orderbook / “Pro” interface**, not instant convert.
- You know your fee tier.

### Step 0 — Identify the spread
Compute:
- `spot_price` on Exchange A (Kraken, etc.)
- `perp_price` on Exchange B (Binance, Bybit, etc.)
- `spread_pct = (perp_price - spot_price) / spot_price * 100`

Only proceed if spread is comfortably above your total cost threshold.

### Step 1 — Choose position size
Pick `Q` ADA such that:
- You can buy `Q` ADA on spot exchange
- You can short `Q` ADA notional on perp exchange with safe margin (low leverage)

### Step 2 — Enter both legs (as close in time as possible)
1) Place spot buy for `Q` ADA (limit/market depending on urgency).
2) Place perp short for `Q` ADA notional (same size).

**Goal:** finish with `+Q ADA spot` and `-Q ADA perp`.

### Step 3 — Confirm hedge is correct
Check:
- Spot holdings increased by ~`Q ADA`
- Perp position is `-Q ADA` (or equivalent notional)

If sizes mismatch, adjust immediately.

### Step 4 — Monitor convergence and funding
Track:
- Spread over time
- Funding rate and next funding timestamp
- Your unrealized PnL on both legs

### Step 5 — Exit condition
Close when either:
- Spread compresses to your target (e.g., from +0.60% down to +0.10%), OR
- Your net PnL after fees is at/above target, OR
- Funding regime turns against you.

### Step 6 — Close both legs
1) Close perp short (buy to close `Q` ADA notional).
2) Sell the spot ADA (`Q` ADA).

Order can be reversed; do whichever reduces risk given liquidity.

### Step 7 — End state (what you should have)
- No ADA exposure
- USDT (or USD) net profit (if edge > costs)

---

## 9) Worked example (simple numbers)

Assume at entry:
- Spot (Kraken): 0.3640
- Perp (Binance): 0.3665
- Spread: ~0.69%

Enter:
- Buy 10,000 ADA spot @ 0.3640
- Short 10,000 ADA perp @ 0.3665

Later, prices converge:
- Spot: 0.3650
- Perp: 0.3651

Exit:
- Sell spot 10,000 ADA @ 0.3650
- Buy to close perp 10,000 ADA @ 0.3651

You made money primarily because:
- You shorted perps higher and covered lower
- And/or you captured spread compression (plus possible funding)

Net profit must exceed total fees + slippage.

---

## 10) Practical tips to make it actually work

- Use low leverage on the perp leg (avoid liquidation).
- Prefer exchanges with deep liquidity for both legs.
- Trade larger notionals only when you can safely do so (withdraw fees aren’t the issue here; margin is).
- Don’t hold through many funding intervals unless you’re sure funding favors you.
- Log every fill price and compute *net* PnL with fees.

---

## 11) What this strategy is NOT

- Not a guaranteed profit machine
- Not “spot-to-spot” arbitrage (no on-chain transfer required)
- Not safe if you use high leverage or mismatched position sizes

---

## 12) Summary

This strategy works because **perps are structurally forced to track spot over time** (funding + arbitrage).  
By holding **spot long** and **perp short** in equal size, you become **direction-neutral** and profit from **spread convergence** rather than ADA’s price movement.

---
