# TradingAgents (TauricResearch) as a second opinion on the momentum top 15: pre-registered 17 Oct 2026, before any rating exists

**Question.** Among the stocks our momentum rule picks, do the stocks that the TradingAgents LLM team rates Buy/Overweight go on to beat the ones it rates Hold/Underweight/Sell?

## Why this is forward-only
- An LLM has read years of news and prices. Asking it to "decide as of 2019" leaks what it already knows, so a back-test of it would look good for the wrong reason.
- Our sandbox leak test cannot cover a model whose training data is the leak.
- So **no historical run counts as evidence.** Only ratings logged before the outcome existed are scored.

## Setup
- **Shortlist:** the top 15 from the bot's momentum ranking (`rank_universe`), taken at each month-end decision. Nothing else changes in the bot.
- **Advisor:** `bot_v1/advisor_tauric.py` runs TradingAgents once per shortlisted name, one debate round each, and logs `Buy / Overweight / Hold / Underweight / Sell` to `data/botv1_live/advisor_tauric.jsonl`.
- **It never touches an order.** The momentum book and the real-money decision are unchanged while this runs.
- **Scored by:** `advisor_tauric.score()`. For each month-end cohort, the mean 21-session return (from the next session) of Buy/Overweight names minus the mean of the others. The statistic is the mean spread over cohorts; P is a cohort-level bootstrap (4,000 draws, one-sided: P that the spread is ≤ 0).
- `ERROR` and `REVIEW` rows are excluded and counted, not guessed.

## Pass mark to earn any influence
- At least **6 monthly cohorts** and **90 rated names**, and
- mean spread **> 0**, and bootstrap **P < 0.10**.
- With 15 names a month, the earliest possible read is **6 months after the first rating**.

## What passing would unlock (and nothing more)
- A **veto only**: drop names rated Sell/Underweight and take the next-ranked name instead. It never adds names the momentum rule did not pick, and never changes size.
- Even then it starts as a paper shadow book beside the plain top 15, judged on the same rule again.

## What failing means
- The advisor stays a log. It costs LLM credits, so if after 12 cohorts it has not reached P < 0.10, it is switched off.

## Known limits (stated before we look)
- TradingAgents' data vendors (Yahoo news, sentiment) are thinner for Indian small and mid caps than for US large caps. A weak result may reflect poor data, not poor reasoning.
- Ratings vary run to run even at the same inputs. We take one run per name per date and do not re-roll until it looks good.
- The model's own training may include the very period being scored once it is released. This only matters for dates after its cutoff, which is the case we score.
- Research framework, not advice (its own disclaimer). Licence: Apache-2.0, used as an optional dependency, not copied into this repo.
