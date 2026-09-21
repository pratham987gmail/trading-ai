"""Tests for research.data.upstox.capability_matrix."""

from __future__ import annotations

import json

from research.data.upstox.capability_matrix import (
    UPSTOX_CAPABILITY_MATRIX,
    ProviderCapability,
    capability_report,
)


def _find(feature: str) -> ProviderCapability:
    match = next((c for c in UPSTOX_CAPABILITY_MATRIX if c.feature == feature), None)
    assert match is not None, f"no capability entry for {feature!r}"
    return match


def test_never_supported_true_without_verification():
    # The literal "never assume YES" test: any unverified entry must be
    # marked unsupported.
    for cap in UPSTOX_CAPABILITY_MATRIX:
        if not cap.verified:
            assert cap.supported is False, (
                f"{cap.feature} is marked supported=True but verified=False -- "
                "this is exactly the fabrication the spec forbids"
            )


def test_expired_options_verified_supported():
    cap = _find("expired_options")
    assert cap.supported is True
    assert cap.verified is True


def test_expired_futures_verified_supported():
    cap = _find("expired_futures")
    assert cap.supported is True
    assert cap.verified is True


def test_regular_candles_verified_supported():
    cap = _find("regular_candles")
    assert cap.supported is True
    assert cap.verified is True


def test_historical_volume_and_oi_verified_supported():
    for feature in ("historical_volume", "historical_oi"):
        cap = _find(feature)
        assert cap.supported is True
        assert cap.verified is True


def test_expired_5minute_verified_unsupported():
    cap = _find("expired_5minute")
    assert cap.supported is False
    assert cap.verified is True  # confirmed absent from the documented expired intervals


def test_historical_bid_ask_unverified_unsupported():
    cap = _find("historical_bid_ask")
    assert cap.supported is False
    assert cap.verified is False


def test_historical_greeks_unverified_unsupported():
    cap = _find("historical_greeks")
    assert cap.supported is False
    assert cap.verified is False


def test_historical_chains_unverified_unsupported():
    cap = _find("historical_chains")
    assert cap.supported is False
    assert cap.verified is False


def test_capability_report_json_safe():
    report = capability_report()
    json.dumps(report)  # must not raise
    assert report["provider"] == "upstox"
    assert len(report["capabilities"]) == len(UPSTOX_CAPABILITY_MATRIX)


def test_capability_report_has_readable_table():
    report = capability_report()
    table = report["summary_table"]
    assert isinstance(table, str)
    assert "feature" in table
    assert "expired_options" in table


def test_every_entry_has_notes():
    for cap in UPSTOX_CAPABILITY_MATRIX:
        assert cap.notes.strip() != ""


def test_as_dict_matches_fields():
    cap = _find("regular_candles")
    d = cap.as_dict()
    assert d == {
        "feature": "regular_candles",
        "supported": True,
        "verified": True,
        "notes": cap.notes,
    }
