# Next tasks: NIFTY hedged weekly option selling (edge search)

Written 2026-10-26. Read this first when planning. Each task has a goal, inputs, a done-when, and a gate. Do them in order; T1-T3 are blocked on quote-log data. Fund assumption: Rs 25,00,000, lot size 65, hedged (defined-risk) structures only, no naked futures or naked shorts.

## State at handoff
- Full-setup honest backtest (leave-one-year-out + walk-forward, 16 pre-registered rules, 2019-02-11..2026-10-16, EOD bhavcopy, no bid/ask) found **no validated edge**. Pass bar not met.
- Only candidate: IC09_200 (0.9 SD short strikes, 200-pt wings) when India VIX is 16-20. LOYO mean +409 Rs/lot/cycle at 1.5x slippage (CI +20..+1,001), picked in 7/8 folds. Walk-forward CI includes zero, BH q=0.144, worst-case marking of dropped cycles gives +108 (CI -380..+521). The VIX band was chosen post hoc.
- Trades ~87 of 396 cycles. Mean over all cycles ~Rs 90/lot/cycle; 12% CAGR at Rs 25L needs ~Rs 5,500 per cycle (~Rs 1,100/lot at 5 lots). At 20 lots CAGR is only 3.3%. Sizing cannot close the gap.
- Result sign depends on slippage: IC break-even ~0.4x the model, PCS ~2x. EOD data cannot resolve it. Only measured spreads can.
- 2024-26 data was reused by earlier scans, so it is not a clean holdout. The only clean test is forward paper-trading.
- Scratch work (not in git): `~/edge_explore/` (`fb_core.py`, `fb_run.py`, `fb_25l.py`, `prereg_full_backtest.md`, `results_full_backtest.md`, `_fb_results.pkl`). Copy what you need into the repo before relying on it.

## Standing rules
- Pre-register any new test before looking at outcomes. No new rules or thresholds after seeing results.
- Do not touch the holdout guard (`--allow-holdout`) without saying so.
- NSE archive (`~/nse_fo_archive`) is licence-restricted: never commit it.
- One machine logs quotes per day (dedupe key is per file). The systemd timer on the VM also logs.
- Subagent summaries are not evidence: the main agent re-runs numbers and tests before reporting.

## T0. Validate the quote logger's first sessions  (can start now)
- Goal: confirm the bid/ask mapping is right before trusting any spread.
- Do: after the first in-window runs, check `bid < ask` on most two-sided rows and `bid_qty`/`ask_qty` populated in `paper_trading/tracker/QUOTE_LOG.csv`. If blank or crossed, fix `parse_nse_chain` in `scripts/log_option_quotes.py` and add a test.
- Done when: a note in `paper_trading/QUOTE_LOGGING.md` says the mapping is confirmed on a live window snapshot.

## T1. Measure real spreads vs the cost model  (needs >= 5 trading dates in the log)
- Do: `.venv/bin/python scripts/summarize_quote_log.py`. Record the median and p90 of `half_spread_to_model` for structure legs, by moneyness bucket and VIX bucket (weight toward the strikes the 0.9 SD condor actually sells).
- Done when: a short report states the implied slippage multiplier with n, dates, and VIX regimes covered, against the break-evens (IC ~0.4x, PCS ~2x).
- Gate: below ~0.4x, condor survives costs; between 0.4x and 2x, only credit spreads can; above 2x, hedged weekly selling is closed out. Do not decide on fewer than 5 dates or a single VIX regime.

## T2. Re-run the backtest at measured cost  (needs T1)
- Do: pre-register, then re-run the same 16-rule LOYO + walk-forward harness with `m` set to the measured multiplier (plus 0.5x and 1.5x of it). No new rules.
- Done when: results state pass/fail per the original bar, including worst-case marking of dropped cycles, and the Rs 25L account simulation at 1, 2, 4, 5, 10, 20 lots.
- Gate: nothing passes -> record "no edge" and stop selling-only work. Only IC09_200|VIX 16-20 passes -> T3.

## T3. Forward paper-trade one frozen rule  (needs T2 pass, or user override)
- Rule frozen before starting: IC09_200 (0.9 SD shorts rounded to 50, 200-pt wings), enter only at VIX 16-20, hold to expiry, size <= 2% of capital (Rs 50,000) max loss per trade (about 4-5 lots).
- Do: log each signal, intended and actual fills against the live bid/ask, and expiry P&L. Use the existing paper_trading tracker. Add nothing that can place orders.
- Done when: >= 30 qualifying trades (about 2-3 years at ~1 in 4 cycles, so expect to extend or loosen nothing) or a pre-set stop-loss on cumulative P&L is hit.
- Gate: compare realized net per lot to the T2 estimate; CI lower bound > 0 before any real capital.

## T4. Independent replication of the backtest  (any time)
- Do: a second agent re-implements the LOYO fold selection and the pass-bar tests from `prereg_full_backtest.md` alone, without reading `fb_core.py`, then compares fold choices and headline numbers (+409, 7/8 folds, +108 worst case, BH q 0.144). Report mismatches.
- Done when: numbers agree or discrepancies are explained.

## T5. 12% CAGR route  (decision for the user, not an agent default)
- Selling-only variants top out far below 12% at Rs 25L. Candidate directions, each needs its own pre-registration:
  - Collar or covered-call overlay on a NIFTY ETF holding (reduces vol and drawdown; did not beat buy-and-hold in dev).
  - A different edge source (futures data download is parked; user wants hedged positions only).
- Done when: the user picks a direction or accepts that 12% is not supported.

## Housekeeping
- Keep `git status` clean on the repo; scratch stays in `~/edge_explore`.
- If the logger is changed, rerun `tests/test_log_option_quotes.py` (36 tests).
- Commit locally after each task; ask before pushing.

## Update 2026-10-28: real spreads arrived, re-cost results (supersedes the T0-T2 gates above)
Source: `paper_trading/tracker/QUOTE_LOG.csv`, 13 snapshots, only ONE clean in-window day (2026-10-28, VIX ~16.5, DTE 6, one expiry). Scratch work in `~/edge_explore/` (not in git): `prereg_recost.md`, `results_recost_harness.md`, `results_recost_scans.md`, `indep_results.md`, `basis_gap.md`.

**Measured cost:** median half-spread / model slippage = 0.14x (short legs 0.18x, long 0.09x, ATM 0.46x, p90 structure legs 0.68x). Four-leg condor half-spread sum ~0.2 pts/unit vs ~2.2 in the model. Top-of-book depth at condor legs: median ~1,100 (short) / ~3,000-5,000 (long) units, min 65. T0 (field mapping) is confirmed.

**Re-cost (frozen grid 0.15 / 0.30 primary / 0.60; 490 cells counted):**
- Unconditional dev cadence net Rs/lot at m=0.15: IC09_200 +35, PCS09_200 +114, CCS09_200 -65. IC break-even ~0.4x, so the condor now survives costs; the filter matters less.
- 16-rule LOYO + walk-forward harness (reproduces the original exactly at 1.5x; independently re-implemented and matched): drop variant passes 7 of 8 bar items at every m; the walk-forward CI includes zero (lower bound about -170 to -220) so it still FAILS the bar. Worst-case marking of dropped cycles passes 2 of 8 and selects a different rule (CCS09_200|F3). Selected rule in drop variant: IC09_200|VIX 16-20, 8 of 8 LOYO folds, BH q 0.03-0.06, shuffle-null false-pass 0. LOYO mean +616..+665 per traded cycle (87 of 396 cycles); entry-basis haircut of 1-2 pts/unit leaves +584 / +519 at m=0.30.
- Scan re-run: 91 cells flip from negative at 1.5x to positive at 0.30/0.15, 0 pass the full bar. One monthly cell (DTE30 PCS11_400) passes dev but is -638 on 2024-26. Sixteen scan cells pass dev only; 2024-26 was not run for scans, and most were already positive at 1.5x and rejected for other reasons (OI/PCR entry-day OI is look-ahead; trend gates and holiday-shifted-expiry failed 2024-26 earlier).
- Sizing at Rs 25L (m=0.30): 1 lot 0.29% CAGR, 20 lots ~5.0%. Achieved ~Rs 142 per lot per cycle vs ~Rs 5,500 per cycle needed for 12%. The 2% risk cap allows ~3 lots at ~Rs 12,940 max loss per lot. 12% CAGR is still not supported.

**Verdict:** cost is no longer the blocker. The remaining blockers are statistical (walk-forward CI, dropped-cycle sensitivity, post-hoc VIX band, 2024-26 reused) and the unmeasured EOD-vs-intraday entry basis.

**Next tasks (replace T1-T3):**
1. Keep logging until >= 10 clean in-window dates across VIX <13, 13-20, >20 and both Monday/Tuesday entries; re-run `summarize_quote_log.py` per VIX bucket. The VIX >= 25 loss bucket has no coverage.
2. (First pass done 2026-10-29, see Update (b); repeat as more logged days get published.) Fetch bhavcopy for 2026-10-19, 21, 22, 23, 26, 27 and later logged days into `~/nse_fo_archive/daily/` (licence-restricted, never commit), then compute EOD-settle credit minus intraday mid credit (`basis_gap.md` lists what is needed). Intraday mid credit varies 1.1-1.4 pts/unit (condor) and 3.2-3.4 (PCS), more than the spread.
3. Run 2024-26 for the 16 dev-pass scan cells that have a frozen definition, as one pre-registered batch, before anyone relies on them.
4. Depth check: share of snapshots with bid/ask qty >= lots x 65 at the short strikes, to cap lots (2% cap is ~3 lots).
5. Forward paper-trade IC09_200 at VIX 16-20 (frozen 2026-10-29, see T3-final), log intended vs achieved fills. This is the only clean holdout.
6. 12% route remains a user decision.

## Update 2026-10-29: out-of-sample test of the repo's weekly ML entry + exit gates (NO DEMONSTRATED EDGE)

Pre-registered (`~/edge_explore/prereg_ml_gate.md`, scratch) and run as walk-forward on real archive prices: fold Y = `WeeklyEntryLearner` + `ExitStrategyEngine` retrained to Dec 31 of Y-1 (`scripts/train_ml_cutoff.py`), test years 2022-2026 (to 2026-10-16), lots=10, same rule exits and strategy chooser. Gates are `ChainConfig.entry_gate` / `exit_gate` (default off, byte-identical output without them); adapters in `scripts/ml_gate_adapters.py` lag every series that closes after 15:30 IST by one row and raise on missing features. Run: `scripts/run_ml_gate_wf.py`. Preset seed pickles were not used (no recorded training window, so they leak).

Primary cell EX (entry 0.55 + exit prob>=0.75 and pnl<-15%) at m=0.30: n=80 of 222 signals, mean +222 vs baseline +73 per trade, but paired delta per signal +5.7, year-clustered CI [-205, +354], positive in 2 of 5 years (2022 and 2026 only), 80th percentile of a random gate that skips as many signals, entry-score AUC against real win/loss 0.499 mean (0.449 pooled, years 0.37/0.38/0.53/0.75/0.46). Items passed: gated mean >0 at m=0.60, beats F3. Failed: CI, years, random-gate p95, AUC, worst-case marking. An independent re-computation (from the prereg only) reproduced the delta, CI, per-year signs and AUCs.
- Entry gate alone (E 0.55): +30 per signal, CI [-141, +361], 91st percentile of the random null, AUC as above. The gain comes from taking fewer trades and a higher iron-condor share, not from ranking wins.
- Exit gate alone (X): -7 per trade vs +73 baseline, paired delta about -109, CI [-224, +18]; it exits recoverable losers (the model learned its own "short strike within 2% while losing" labelling rule).
- Caveats: trained on Black-Scholes simulator labels; exit model is out of domain (trained on 14-21 day verticals, scored on 5-session condors/PCS; `theta_estimate`, `dte_remaining` not faithful); trained on same-day macro but scored on lagged; `nifty_1d_return` is a constant 0 in train and inference (Amendment 1b); only 5 years and about 220 trades; 2024-26 was seen by earlier scans. Pass-bar item 6 (skips marked at the median loss) is unpassable by any filter that skips signals, a prereg design flaw that does not change the verdict (items 1, 2, 3, 7 fail on their own).
- Sensitivities (0.50, 0.60 thresholds) are exploratory and also fail item 1.

**Verdict:** the ML gates add no demonstrated edge over the rule-based weekly system; do not wire them into live decisions. The rule-based replay (+73..+133 per lot per trade at 0.30x, CI includes zero) is unchanged by this test.

## Update 2026-10-29 (b): EOD-settle vs intraday-mid entry basis, first measurement
- Archive extended to 2026-10-28 from the NSE new-format F&O bhavcopy (converted to the archive layout; mapping checked on 2026-10-16: 1882 of 1882 rows identical). 2026-10-20 is a holiday; 2026-10-29 was not yet published.
- Gap = same-day EOD settle credit minus intraday quote-log mid credit, same strikes, per unit (pts). Two usable days only (10-26 one forced snapshot, 10-28 twelve in-window snapshots), so this is a first look, not a measurement.

| Structure | Expiry | Day | n | EOD minus mid (pts) |
|---|---|---|---|---|
| IC | 03-Nov | 10-26 / 10-28 | 1 / 12 | +3.2 / +0.1 (range -6.5..+3.8) |
| IC | 09-Nov | 10-26 / 10-28 | 1 / 12 | +3.1 / +2.4 (range -4.7..+5.5) |
| PCS | both | 10-26 / 10-28 | 1 / 12 | -4.4..-5.9 / -7.6..-8.0 |

- Condor: EOD credit is above the intraday mid by about +1.3 pts/unit on average (about Rs 85 per lot at 65 units, within the 1-2 pt haircut assumed earlier). The backtest therefore overstates the achievable condor credit, and this is on top of the 0.30x slippage, which models only the half-spread to mid.
- PCS: the negative gap is directional, not a bias. Spot rose 60-90 pts between the snapshot and the close on both days and a short put spread loses credit as spot rises; a condor is close to delta-neutral, so its gap is the cleaner number.
- Not yet known: whether the condor gap persists on days when spot falls (should flip PCS sign, leave IC near +1), and the IC range is wide (-6.5..+5.5) so n=2 days cannot give a CI. Keep logging.

## T3-final. Forward paper-trade, pre-registered 2026-10-29 (before any qualifying trade)
Only IC09_200 | VIX 16-20 survived the cost-adjusted harness (walk-forward CI included zero; VIX band chosen post hoc; 2024-26 reused). This is the only clean holdout.
- Rule (frozen): weekly NIFTY iron condor, shorts at 0.9 SD rounded to 50, 200-pt wings, entry on the first in-window quote after the signal day when India VIX is 16-20, expiry at the nearest weekly with 3-8 DTE, hold to expiry (no stop, no early exit), 4 lots (max loss about Rs 50,000 or less, 2% of Rs 25L). No parameter, band, size or exit change once the first qualifying trade is logged.
- Fills logged per trade: intended (quote-log mid) and achieved-proxy (natural: sell at bid, buy at ask), plus expiry settle from the bhavcopy archive. Paper only; nothing places orders.
- Primary metric: net Rs per lot per trade using the natural-fill proxy and the repo fee model, hold to expiry at intrinsic.
- Horizon: stop at 30 qualifying trades or 2028-10-31, whichever is first. Cumulative-loss stop: paper loss of Rs 75,000 (3% of the fund) ends the test as FAIL.
- Interim look only at 12 trades, and only for the loss stop; no early PASS.
- PASS (all): mean net per lot > 0; 95% bootstrap CI lower bound > 0 over the trades; win rate >= 55%; achieved-proxy credit is within 2 pts/unit of the EOD-settle credit for the same strikes on average (else the backtest basis is rejected).
- Worst-case line: no single trade loses more than the structural max loss (Rs 13,000 per lot at 200-pt wings minus credit); a larger loss means a pricing or fill bug and voids that trade for review, not for exclusion.
- Anything short = no demonstrated edge; do not scale. If fewer than 30 trades by the end date, report as inconclusive, not as pass.
- Expected pace: about 1 qualifying cycle in 4, so 30 trades takes about 2 years; expect to end on the date, not the count. The honest read in 6 months is "too early".
- Freeze check each week: `git log` on this section must show no edit after the first logged trade.
