# Entry-conversion research: what demonstrably improves breakout entry conversion in thin small-caps

**Date:** 2026-07-02
**Method:** deep-research fan-out — 5 parallel search angles (entry timing, confirmation filters, position management, stop placement, breakout-failure microstructure), ~20 primary sources fetched, load-bearing claims adversarially re-verified against primary sources.
**Context this feeds:** the bot's proven bottleneck is ENTRY-CONVERSION (7 refuted selection levers; 76–84% of ORB fires poke-then-stop; the one +EV subset found in-house is breakouts confirming within ~10–15 min of the open, +0.42R).

**Evidence-quality labels used throughout:** `ACADEMIC` (peer-reviewed or serious preprint), `BACKTEST#` (non-academic but real numbers, reproducible-ish), `GURU` (practitioner claim, no verifiable numbers, often selling something), `SEO` (content-farm fluff).

---

## Headline synthesis

Three independent literatures converge on one mechanism for why our fires poke-then-die:

> The breakout level is where resting **supply** is maximal (take-profit clusters, ATM-offering desks, distributing pumpers), while stop-loss orders cluster just beyond it. Merely *reaching* the level most often produces a rejection — the poke is the statistically expected outcome — while a genuine *crossing* that absorbs the shelf ignites a real trend. The reach-vs-cross question resolves fast (rejections play out within ~30 min).

- Osler (NY Fed, `ACADEMIC`, numbers verified against the paper): take-profits cluster ON round-number levels (9.9% of TP orders vs 3.8% baseline), stop-losses just beyond (14.3% of stop-buys priced in the 01–10 digit band vs 6.9% in 90–99). Prices reverse abnormally often upon merely *reaching* a level (59.3% vs 54.8% at arbitrary prices), and that rejection completes within ~30 min; genuine crossings, by contrast, trend significantly for **at least two hours** (FX data). Tradable content: poke-rejection is fast and the base case; a confirmed cross carries durable momentum. [sr150.pdf](https://www.newyorkfed.org/medialibrary/media/research/staff_reports/sr150.pdf)
- SmallCapLab (~3,000 small-cap gap-up events, `BACKTEST#`, short-biased vendor; verified with caveats): 64.2% of gap-ups close below their RTH open, ~72% below VWAP, **46.6% of highs-of-day print in the first 15 min, ~70% by 10:30** (the page's "85%+ by 10:30" caption contradicts its own cumulative chart at 69.6%; ~86% by 13:00); fade-severity buckets imply ~65% collapse ≥20% off HOD. The fade is the base case; the whole long game is the open. Universe caveat: gap ≥45% vs prior close, price ≥$0.30 — *bigger* gaps than the bot's ≤35%-gap universe, so directional only. [smallcaplab.com/research](https://www.smallcaplab.com/research)
- Zarattini/Barbon/Aziz (`ACADEMIC` working paper): 5-min opening range Sharpe 2.81 vs 15-min 1.43 vs 30-min 0.21 — the edge **decays fast with later entry**. [SSRN 4729284](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4729284)

All three externally corroborate the in-house +0.42R early-confirm finding. The literature's answer to "how do winners enter?" is *not* a cleverer trigger — every credibly profitable published system enters **immediately on the break** — it is (a) only trading the first minutes, (b) an asymmetric exit structure (tight-ish stop, no target, let the fat tail run to EoD), and (c) not paying full risk on unconfirmed pokes.

---

## 1. Breakout entry timing: on-break vs retest vs first-pullback

### What the evidence says

**Enter-on-break is the only variant with credible supporting numbers.**

- Zarattini/Barbon/Aziz 2024 ([SSRN 4729284](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4729284), `ACADEMIC` working paper, verified against source): resting stop order AT the 5-min OR high — no confirmation, no retest. Top-20 opening-RVOL "Stocks in Play", 2016–2023, 7,000+ stocks: **+1,637% net, Sharpe 2.81, ann. alpha ~36%**. Independent QuantConnect replication: Sharpe 2.40, **~17% per-trade win rate** (profit rides rare 10R+ winners) ([QuantConnect study](https://www.quantconnect.com/research/18444/opening-range-breakout-for-stocks-in-play/), `BACKTEST#`). Caveats: universe is price>$5 / >1M sh avg volume / ATR>$0.50 — *liquid* stocks-in-play, not our thin sub-$5 tier; **zero slippage assumed**; both authors sell trading education.
- The companion QQQ paper ([SSRN 4416622](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4416622)) likewise enters at the open of the 2nd 5-min candle with zero confirmation. Baseline: 24% win rate, +0.13R/trade.

**Retest / wait-for-pullback entries: the only direct head-to-head found says they are much worse.**

- Mesfin 2026 ([arXiv 2605.04004](https://arxiv.org/abs/2605.04004), `ACADEMIC` preprint, falsification-style with friction; numbers verified exact): MNQ futures, 947 days, 5-min bars, 25-min opening range. Immediate ORB entry: 51.9% win (fails net of friction; best immediate variant +2.82 pts at bar+15, still fails T≥2). **Pullback/retest entry: N=83, 19.3% win rate, 80.7% stop-out rate, −4.44 pts.** Paper's verdict: pullback entries are "systematically wrong" because "a high proportion of apparent breakouts simply fail and reverse." Caveats: index futures, unconditioned signal, small N, and the comparison is not perfectly apples-to-apples (pullback variant used a 20-pt stop while immediate variants used fixed-horizon exits) — but it is the only rigorous break-vs-retest comparison found anywhere.
- Bulkowski (`BACKTEST#`, informal daily-bar pattern stats): throwbacks/pullbacks occur after ~60–74% of breakouts and patterns **with** throwbacks *underperform* those without — waiting for the retest adversely selects the weak breakouts and misses the ~30–40% strongest moves that never come back ([thepatternsite.com](https://thepatternsite.com/fallwedge.html)).
- Mechanistically (Mesfin): entering a bar or more after the trigger is structurally post-exhaustion — expansion-bar continuation tested *actively wrong* (T=−10.96 at bar+1; measured in the Asia session, so context caveat applies); the move is largely contained in the trigger bar. Entry latency consumes the edge.

**First-pullback / bull-flag advocacy is evidence-free.** All numeric claims circulating for intraday small-cap flags (~52–75% win rates) trace to `SEO`/`GURU` content with no reproducible methodology. Warrior Trading's Ross Cameron publishes CPA-reviewed *aggregate* P&L ($18.8M 2017–2025 from $583) but **no audited per-setup statistics** — and the FTC settled with Warrior Trading for **$3M in 2022 over deceptive earnings claims** ([warriortrading.com earnings page](https://www.warriortrading.com/ross-camerons-verified-day-trading-earnings/), [FTC action]) — n=1 discretionary trader, course-seller conflict. This is consistent with our own no-chase VWAP-pullback backtest coming out negative after cost, and with the VWAP-reclaim shadow track's do-not-promote verdict.

### Verdict
| Variant | Evidence | Direction |
|---|---|---|
| On-break (stop order at trigger) | ACADEMIC + replication | Only variant with a documented net edge (when paired with selection + exit structure) |
| Wait-for-retest | ACADEMIC (1 study) + BACKTEST# | Materially worse; adverse selection |
| First-pullback (flag) | GURU only | Untested anywhere credible; our own test negative |

**Testable rule (mostly a stop-doing rule):** do not build retest/pullback entry logic; keep the on-break trigger and attack conversion via the time window, exit structure, and sizing below.

---

## 2. Confirmation at the moment of entry (1-min OHLCV computable)

**The only confirmation filter with credible published numbers is opening relative volume**, and it is a *selection* filter, not a trigger filter: Zarattini et al. define RVOL = first-5-min volume ÷ 14-day average first-5-min volume. Trades with RVOL<100% averaged **−0.02R**; RVOL>100% averaged **+0.08R** (`ACADEMIC`, verified). Computable: yes (needs 14 days of per-symbol history). Note: this exact definition (same-minutes-of-day baseline) may differ from the bot's current RVOL — worth checking the implementation matches before assuming this box is ticked.

**Bar-close-above-trigger vs intrabar poke: empirically OPEN.** No published head-to-head with numbers exists despite targeted searching. Every "wait for the close above" claim traces to unbacktested blog assertions (`SEO`). Meanwhile the best-documented ORB system fills *intrabar* on the poke and works. Given Mesfin's latency finding (one bar late = post-exhaustion), waiting 1 minute for a close plausibly costs more than it saves — but this is cheaply testable in-house on logged fires.

**Close-location-in-range (CLR = (close−low)/(high−low)) carries a warning label:** on daily bars in liquid stocks, closing near the high is a *mean-reversion sell* signal — IBS>0.8 fade has worked for two decades (Alvarez, `BACKTEST#`, [IBS post](https://alvarezquanttrading.com/blog/internal-bar-strength-for-mean-reversion/)). High-RVOL gapper minutes may flip the sign, but "strong close = confirmation" is not a market universal.

**Academic base rate for volume confirmation is directional only:** Lo/Mamaysky/Wang 2000 and Gervais/Kaniel/Mingelgrin 2001 (`ACADEMIC`, peer-reviewed) show volume carries incremental information — at day-to-month horizons on 1962–1996 liquid stocks. No intraday entry-bar numbers. The practitioner heuristics (breakout-bar volume ≥1.2–1.3× 20-bar average; price above rising VWAP; CLR in top 30%) are coherent mechanics with **zero published before/after deltas** (`GURU`/`SEO`).

**Testable rules (pre-register, shadow only):**
1. `confirm_close`: require the trigger-minute bar to CLOSE above trigger (vs current intrabar fire) — measure fill-price cost vs whipsaw avoided on logged fires.
2. `vol_surge`: breakout-bar volume ≥ 2× median of prior 20 one-min bars.
3. `clr`: trigger-bar CLR ≥ 0.7.
Each judged on the standard bars (+1R rate, expectancy net of cost, vs same-day ORB baseline). Expectation set LOW: these are folklore until proven on our fires.

---

## 3. Position management: scaling in/out, probes, pyramids

**Scale-OUT (Warrior-style "sell partials into strength") is the best-refuted idea in this whole research pass.** Every source with actual numbers says it reduces expectancy; it buys variance-reduction and win-rate optics:

- Howard Bandy via Bulkowski (`BACKTEST#`, 4,176 trades, 72 liquid tickers 2003–2008): the closer the first target, the more average profit is reduced; BE-stop-after-partial cut profits to **one-third**. Bulkowski: "If you want to make less profit, then scale out of a trade." ([thepatternsite.com/ScalingOut.html](https://thepatternsite.com/ScalingOut.html))
- Alvarez (`BACKTEST#`): scaling out → "large drop in CAR, no change or larger MDD" ([post](https://alvarezquanttrading.com/blog/adding-stops-and-scaling-out-to-a-mean-reversion-strategy/)).
- Kevin Davey (`BACKTEST#`, 567k exit backtests, 40 futures markets): all six *combination* exits were consistently worse than simple single exits ([kjtradingsystems.com](https://kjtradingsystems.com/algo-trading-exits.html)).
- The arithmetic identity: taking half off at +1R turns 2R winners into 1.5R; it only pays if edge *shrinks* as the move extends. Our book is the opposite regime — rare fat winners pay for everything (runner-grade forensics) — which makes the anti-scale-out math *stronger* for us. Warrior Trading's scale-out advice optimizes a discretionary human's psychology, not a bot's expectancy; they publish no backtest.

**Scale-IN (half-size probe at trigger + add on confirmation): theoretically right, empirically unpublished.** Kelly logic supports staged betting when confirmation genuinely raises P(win); trend-following pyramiding literature (Concretum, `BACKTEST#`, multi-decade futures) finds adds lower win rate but raise profit factor via 2–3× larger winners. **No credible published backtest of probe-then-add for intraday small-cap momentum exists** — either way. But it attacks exactly our measured problem: paying full risk on the 76–84% of fires that are unconfirmed pokes, while our own data says confirmation-within-10–15-min flips the subset to +0.42R.

**Testable rule (`probe_add`):** enter 0.5× normal size at trigger; add the remaining 0.5× only if within 10 min the position (a) has not touched the stop and (b) prints a new high above the trigger bar's high (or holds above VWAP). Compare blended R and net expectancy vs all-in baseline on the same fires. Trivially implementable with 1-min bars + two orders.

---

## 4. Stop placement for whipsaw-prone names

**Theory anchor (Kaminski & Lo, `ACADEMIC`, J. Financial Markets 2014):** under a random walk any stop-loss rule strictly lowers expected return; under positive serial correlation (momentum) stops add value ([SSRN 968338](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=968338)). So Alvarez's famous "stops hurt" results (`BACKTEST#`, mean-reversion daily bars: every 3–15% stop reduced CAR — [post](https://alvarezquanttrading.com/blog/maximum-loss-stops-do-you-really-need-them/)) do NOT transfer to our momentum entries; expect the opposite sign. Keep stops; the question is width and type.

**The best-evidenced production configuration is: tight volatility-fractional stop + NO profit target + end-of-day exit.**
- Stocks-in-play paper: stop = **10% of 14-day ATR**, no target, EoD exit, size = 1% risk ÷ stop distance with 4× leverage cap → Sharpe 2.81. Per-trade win rate is LOW (~17% in the replication); the system is a low-win-rate, fat-right-tail machine. EoD-exit beat every fixed R-target in both Zarattini papers.
- **The tight-stop artifact is published and author-admitted:** in the QQQ paper's sweep, the best cell was the tight end of the stop range (5% of ATR ≈ $0.08 on TQQQ, +9,350%) under **zero slippage**, and the authors themselves flag it verbatim as "unrealistic as our model assumed no slippage… will likely be exceeded." This is exactly the artifact we hit with the VR track. Any in-house tight-stop test must enforce a spread-aware floor: `stop_width ≥ max(k_ATR × ATR14, m × spread_proxy)` where spread_proxy for thin names can be estimated from 1-min high-low of quiet bars.
- Note the accounting identity: with fixed-fractional sizing, stop width and position size are the same knob until a leverage/notional cap binds. "Tight stop, small dollar loss, huge size" is what makes the Zarattini machine work on liquid names — and what slippage destroys on thin ones. A calibrated middle (e.g. 20–30% of ATR14 with the spread floor) is the honest test.

**Breakeven moves: near-noise in published tests** (0.3% of trades touched in one study; "heavily cuts profitability" in another — `BACKTEST#`/`GURU` mixed). On whipsaw small caps a BE@+5% will scratch far more often — our shipped BE rule is worth measuring, not assuming.

**Time stops: evidence gap.** No credible published study of "exit if no progress in N minutes" for intraday small caps. EoD exit is the only well-evidenced time stop. But Osler's fast poke-rejection window (~30 min) + SmallCapLab's HOD-in-first-15-min curve + our own +0.42R early-confirm subset all point the same direction, making this the highest-prior in-house experiment:

**Testable rules:**
1. `time_stop`: exit at market if the trade has not reached +0.5R (alt: has not printed a new post-entry high) within 10 min of fill. Measures: expectancy delta, avg loss on scratched trades vs their counterfactual stop-outs.
2. `atr_stop_floor`: stop = max(0.25 × ATR14_daily_fraction, 3 × spread_proxy), no profit target, trail-to-EoD — Zarattini structure adapted with an honest fill floor; pre-register against the tight-stop artifact (require the edge to survive +1 tick adverse fill on entry AND stop).
3. `eod_runner`: for confirmed trades (passed the 10-min gate), remove the profit target; hold with trailed structure stop into the last hour (Gao et al. intraday momentum: payoff concentrates late day — `ACADEMIC`, index-level caveat).

---

## 5. Why breakouts fail (microstructure) and what precedes the ones that hold

- **Reach-then-reject is the base case; real crossings trend** (Osler, `ACADEMIC`): stops cluster just beyond salient levels, take-profits sit on them. Prices that merely *reach* a level reverse abnormally often, with the rejection completing within ~30 min; prices that genuinely *cross* keep trending for hours (FX). The whipsaw is not noise — it is the statistically dominant outcome of touching a supply shelf, and the fast resolution window is why early confirmation is the +EV subset. (Note: an earlier read of this paper as "cascade fuel gone by 60 min" was checked against the source and inverted — post-crossing trends persist; it is the *rejection* that is fast.)
- **The level itself is a supply shelf**: take-profits cluster ON round numbers (Osler); in dilution-overhung small caps, ATM desks sell into spikes (DilutionTracker, `GURU`, unquantified but mechanistically sound — and EDGAR S-3/424B5 presence is checkable, ties to the existing EDGAR-dilution roadmap item); in pumped names the breakout print *is* the manipulator's exit liquidity (Korea order-book study, `ACADEMIC`, [SSRN 4173353](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4173353)); heavily retail-herded names average **−4.7%** 20-day abnormal returns (Barber et al. JF 2022, `ACADEMIC`).
- **Base rates say fade is the default** (SmallCapLab, `BACKTEST#`, vendor-biased, gap ≥45% universe): 64% of gappers close red vs open; only 41.5% ever break the premarket high; bigger gaps collapse harder; **higher premarket volume ⇒ harder fade** (56.2% → 65.6% → 71.5% across PM-volume buckets, small n in places) — externally corroborates our refuted RVOL/gap selection levers.
- **Conditions preceding holds, computable from our bars:** minutes-since-open (dominant), premarket-high break (binary level of real significance), distance of trigger to round numbers ($0.50/$1.00), gap size (inverse), price tier ($10+ most resilient, sub-$1 fades hardest). Conditions that do NOT predict holding: premarket volume, RVOL beyond the >100% floor, float — consistent with all seven in-house refutations.

**Testable rule (`round_number`):** log distance from trigger price to nearest $0.50/$1.00 increment on every fire; test whether fires triggering just *below* a round number (into the take-profit shelf) underperform fires triggering just *above* one (through the stop cluster). Free to log, zero risk.

---

## Ranked shortlist: 3 most promising, implementable on 1-min OHLCV

**1. Time-boxed confirmation: entries only in the opening window + a 10-minute time-stop on unconfirmed positions.**
Strongest convergence in the entire pass: our own +0.42R early-confirm subset, Osler's ~30-min poke-rejection resolution (`ACADEMIC`), SmallCapLab's 47%-of-HODs-in-first-15-min curve (`BACKTEST#`), and Zarattini's 5-min≫15-min≫30-min Sharpe decay (`ACADEMIC`). Rule: take ORB fires only ~9:30–9:50; exit at market any position that hasn't made a new post-entry high (or +0.5R) within 10 min. Directly converts the known 76–84% poke-then-die population from full-stop losses into small scratches. Cheapest to test: pure replay of logged fires.

**2. Probe-then-add sizing: 0.5× at trigger, add 0.5× only on 10-minute confirmation.**
The only technique that structurally exploits the discovered +EV confirm-subset without needing to *predict* it. Theory-consistent (Kelly staged betting, pyramiding profit-factor results); no published intraday small-cap evidence either way, so shadow-first with pre-registered bars. Halves risk on the ~80% dead pokes at the cost of a worse blended entry on winners — the trade-off is measurable on existing fire logs before any live change.

**3. Asymmetric exit restructure: volatility-fractional stop with a spread floor, NO profit target, trail to EoD; kill any scale-out.**
The published edge (Sharpe 2.81, replicated) lives in the exit asymmetry, not the entry: tight-ish stop, ~17–25% win rate, fat right tail runs to the close. Scale-out is refuted by every numeric source (Bandy/Alvarez/Davey). MUST be tested with an adverse-fill/spread floor (`stop ≥ max(0.25×ATR14, 3×spread_proxy)`, +1-tick adverse fills) — the tightest-stop-wins result is a documented zero-slippage artifact, the same artifact class that killed the VR and liq1 reads.

**Explicit do-nots from this pass:** no retest or first-pullback entry logic (refuted/evidence-free; matches our negative in-house test); no scale-out partials; no adoption of close-above-trigger or volume-multiple confirmation from folklore without an in-house pre-registered test.

---

## Source list

| Source | Type | URL |
|---|---|---|
| Zarattini, Barbon & Aziz — Profitable Day Trading Strategy (stocks-in-play ORB) | Academic WP | https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4729284 |
| Zarattini & Aziz — Can Day Trading Really Be Profitable? (QQQ ORB) | Academic WP | https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4416622 |
| QuantConnect replication of stocks-in-play ORB | Backtest | https://www.quantconnect.com/research/18444/opening-range-breakout-for-stocks-in-play/ |
| Mesfin — ORB/retest falsification study, MNQ | Academic preprint | https://arxiv.org/abs/2605.04004 |
| Osler — Stop-Loss Orders and Price Cascades (NY Fed SR150) | Academic | https://www.newyorkfed.org/medialibrary/media/research/staff_reports/sr150.pdf |
| Kaminski & Lo — When Do Stop-Loss Rules Stop Losses? | Academic | https://papers.ssrn.com/sol3/papers.cfm?abstract_id=968338 |
| Gao, Han, Li & Zhou — Intraday Momentum | Academic | https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2552752 |
| Heston, Korajczyk & Sadka — intraday periodicity | Academic | https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1107590 |
| Lo, Mamaysky & Wang — Foundations of Technical Analysis | Academic | https://papers.ssrn.com/sol3/papers.cfm?abstract_id=228099 |
| Gervais, Kaniel & Mingelgrin — High-Volume Return Premium | Academic | https://papers.ssrn.com/sol3/papers.cfm?abstract_id=146468 |
| Barber, Huang, Odean & Schwarz — Attention-Induced Trading (Robinhood herding) | Academic | https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3715077 |
| Lee/Lee/Kim — pump-and-dump order-book study (Korea) | Academic | https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4173353 |
| Bulkowski — Scaling Out (incl. Bandy study) | Backtest | https://thepatternsite.com/ScalingOut.html |
| Alvarez — Adding Stops and Scaling Out (MR) | Backtest | https://alvarezquanttrading.com/blog/adding-stops-and-scaling-out-to-a-mean-reversion-strategy/ |
| Alvarez — Maximum Loss Stops | Backtest | https://alvarezquanttrading.com/blog/maximum-loss-stops-do-you-really-need-them/ |
| Alvarez — Internal Bar Strength | Backtest | https://alvarezquanttrading.com/blog/internal-bar-strength-for-mean-reversion/ |
| Davey — exit study (567k backtests) | Backtest | https://kjtradingsystems.com/algo-trading-exits.html |
| Concretum — pyramiding vs vol targeting | Backtest | https://concretumgroup.com/position-sizing-in-trend-following-comparing-volatility-targeting-volatility-parity-and-pyramiding/ |
| SmallCapLab — gapper base-rate research | Vendor backtest | https://www.smallcaplab.com/research |
| Warrior Trading — verified earnings page | Guru (aggregate audited) | https://www.warriortrading.com/ross-camerons-verified-day-trading-earnings/ |
| CXO Advisory — ORB review | Review | https://www.cxoadvisory.com/technical-trading/day-trading-with-an-opening-range-breakout-strategy/ |
