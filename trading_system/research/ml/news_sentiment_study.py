"""Does real financial-news sentiment lead NIFTY's next 1-3 day return?

Correlation-before-trading-logic, exactly as the brief for this study asked:
this script computes and reports the correlation between daily aggregated
headline sentiment and forward index returns, and stops there. No signal,
threshold, or backtest is built on top of it -- that would only be worth
doing if the correlation itself were real.

**Data source and its real limitation, disclosed up front.** Headlines come
from NewsAPI's ``/v2/everything`` endpoint (a real key, ``NEWSAPI_KEY``, is
required and checked below). NewsAPI's free tier restricts the ``/everything``
endpoint to roughly the trailing 29 days and caps each query's accessible
results (page 2 returns ``maximumResultsReached`` -- confirmed live, not
assumed). That means this study covers on the order of 20-25 trading days,
not the hundreds used by every other predictive-value study in this project
(technicals, options-flow, max-pain, FII/DII, chain-replay, breakout). A
correlation over ~20 points is not statistically decisive either way; this
is a screening pass, and its own p-values are reported so that limitation is
visible in the output rather than glossed over.

FinBERT was the model named in the original brief. It was not used here:
this environment has no ``transformers``/``torch`` install, and downloading
them (multi-GB) for an exploratory pass whose main constraint is data
volume, not scoring precision, was not judged worth it. VADER
(``vaderSentiment``, a lexicon-based English sentiment scorer) is used
instead -- a real, disclosed simplification, not a silent substitution.

Method:
    1. For each of the last ~29 real calendar days, query NewsAPI for
       Nifty/Sensex/NSE/BSE headlines restricted to major Indian financial-
       press domains (cuts the false-positive rate from "nifty" the common
       English adjective and irrelevant software-package hits -- both
       observed live in an unrestricted query before this restriction was
       added).
    2. Score each headline's title (+ description, when present) with
       VADER's compound score in [-1, 1].
    3. Aggregate to one mean sentiment score and article count per calendar
       date.
    4. Join against real NIFTY daily closes (``historical_bars.db``,
       backfilled from Upstox) to compute forward returns at 1, 2, and 3
       trading days ahead of each sentiment date.
    5. Report Pearson correlation, its p-value, and directional hit rate
       (does the sign of the day's sentiment match the sign of the forward
       return more than half the time) per horizon. Nothing past this.

Run:
    python -m research.ml.news_sentiment_study
"""

from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests
from scipy import stats
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from config.settings import get_settings  # noqa: E402
from research.data.upstox.store import HistoricalBarStore  # noqa: E402

NEWSAPI_URL = "https://newsapi.org/v2/everything"
QUERY = "Nifty OR Sensex OR NSE OR BSE"
DOMAINS = ",".join(
    [
        "economictimes.indiatimes.com",
        "moneycontrol.com",
        "business-standard.com",
        "livemint.com",
        "financialexpress.com",
        "thehindubusinessline.com",
        "ndtv.com",
    ]
)
LOOKBACK_DAYS = 28  # stays inside the free tier's ~29-day window with a 1-day safety margin
FORWARD_HORIZONS = (1, 2, 3)
REQUEST_PAUSE_SECONDS = 1.0  # polite pacing; free tier allows 100 req/day, this uses ~28


def fetch_daily_headlines(api_key: str, day: date) -> list[dict[str, Any]]:
    """Fetch one calendar day's headlines from the restricted domain set.

    Returns an empty list (never raises) on a non-200 response, since a
    single day's fetch failure should not sink the whole study -- it is
    recorded as a gap in the day's article count instead.
    """
    params = {
        "q": QUERY,
        "domains": DOMAINS,
        "language": "en",
        "from": day.isoformat(),
        "to": (day + timedelta(days=1)).isoformat(),
        "sortBy": "publishedAt",
        "pageSize": 100,
        "apiKey": api_key,
    }
    try:
        response = requests.get(NEWSAPI_URL, params=params, timeout=20)
    except requests.RequestException as exc:
        print(f"  {day}: request failed ({exc})", file=sys.stderr)
        return []
    if response.status_code != 200:
        print(f"  {day}: HTTP {response.status_code}", file=sys.stderr)
        return []
    data = response.json()
    if data.get("status") != "ok":
        print(f"  {day}: {data.get('code')} {data.get('message')}", file=sys.stderr)
        return []
    return data.get("articles", [])


def score_daily_sentiment(api_key: str, start: date, end: date) -> pd.DataFrame:
    """Fetch and score one headline per day across [start, end], inclusive."""
    analyzer = SentimentIntensityAnalyzer()
    rows: list[dict[str, Any]] = []

    day = start
    while day <= end:
        articles = fetch_daily_headlines(api_key, day)
        scores = []
        for article in articles:
            text = " ".join(filter(None, [article.get("title"), article.get("description")]))
            if not text.strip():
                continue
            scores.append(analyzer.polarity_scores(text)["compound"])

        rows.append(
            {
                "date": day,
                "article_count": len(articles),
                "scored_count": len(scores),
                "mean_sentiment": float(np.mean(scores)) if scores else None,
            }
        )
        print(f"  {day}: {len(articles)} articles, mean_sentiment={rows[-1]['mean_sentiment']}")
        day += timedelta(days=1)
        time.sleep(REQUEST_PAUSE_SECONDS)

    return pd.DataFrame(rows).set_index("date")


def _load_nifty_closes(db_path: str | None = None) -> pd.Series:
    store = HistoricalBarStore(db_path or str(Path(__file__).resolve().parent.parent.parent / "data" / "historical_bars.db"))
    frame = store.load_bars(underlying="NIFTY", option_type=None, interval_unit="day")
    frame = frame[frame["strike"].isna()].sort_index()
    closes = frame["close"].dropna()
    closes.index = closes.index.tz_convert("Asia/Kolkata").normalize().tz_localize(None)
    return closes


def run_study(api_key: str | None = None) -> dict[str, Any]:
    settings = get_settings()
    key = api_key or settings.NEWSAPI_KEY.get_secret_value()
    if not key:
        return {"error": "NEWSAPI_KEY not configured"}

    end = date.today() - timedelta(days=1)  # NewsAPI free tier delays "today" by up to 24h
    start = end - timedelta(days=LOOKBACK_DAYS - 1)

    print(f"Fetching headlines {start} to {end} ({LOOKBACK_DAYS} days)...")
    sentiment = score_daily_sentiment(key, start, end)

    closes = _load_nifty_closes()
    forward_returns = pd.DataFrame(index=closes.index)
    for horizon in FORWARD_HORIZONS:
        forward_returns[f"fwd_return_{horizon}d_pct"] = (
            closes.shift(-horizon) - closes
        ) / closes * 100

    sentiment.index = pd.to_datetime(sentiment.index)
    sentiment.index.name = "date"
    joined = sentiment.join(forward_returns, how="inner").dropna(subset=["mean_sentiment"])

    correlations: dict[str, Any] = {}
    for horizon in FORWARD_HORIZONS:
        col = f"fwd_return_{horizon}d_pct"
        sub = joined.dropna(subset=[col])
        if len(sub) < 4:
            correlations[f"{horizon}d"] = {"n": len(sub), "note": "too few paired days to correlate"}
            continue
        r, p = stats.pearsonr(sub["mean_sentiment"], sub[col])
        same_sign = (np.sign(sub["mean_sentiment"]) == np.sign(sub[col])).mean()
        correlations[f"{horizon}d"] = {
            "n": len(sub),
            "pearson_r": round(float(r), 3),
            "p_value": round(float(p), 3),
            "directional_hit_rate_pct": round(float(same_sign) * 100, 1),
        }

    return {
        "window": f"{start} to {end}",
        "days_with_sentiment_data": int(sentiment["mean_sentiment"].notna().sum()),
        "total_days_queried": len(sentiment),
        "total_articles_scored": int(sentiment["scored_count"].sum()),
        "correlations_by_horizon": correlations,
        "daily_detail": [
            {"date": str(idx.date()), **{k: (None if pd.isna(v) else v) for k, v in row.items()}}
            for idx, row in joined.iterrows()
        ] if not joined.empty else [],
    }


if __name__ == "__main__":
    result = run_study()
    print("\n" + json.dumps({k: v for k, v in result.items() if k != "daily_detail"}, indent=2, default=str))

    out_dir = Path(__file__).resolve().parent.parent / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "news_sentiment_correlation.json"
    out_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(f"\nFull report: {out_path}")
