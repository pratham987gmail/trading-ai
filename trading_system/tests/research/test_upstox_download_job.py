"""Tests for research.data.upstox.download_job."""

from __future__ import annotations

import json
from datetime import date

import pytest

from research.data.upstox.download_job import (
    DownloadJob,
    DownloadJobStore,
    partition_date_range,
)


def make_job(**overrides) -> DownloadJob:
    fields = {
        "job_id": "JOB-000001",
        "provider": "upstox_v3",
        "instrument_key": "NSE_INDEX|Nifty 50",
        "underlying": "NIFTY",
        "expiry": None,
        "strike": None,
        "option_type": None,
        "instrument_type": "INDEX",
        "interval_unit": "minutes",
        "interval_value": 1,
        "from_date": "2026-01-01",
        "to_date": "2026-01-31",
        "status": "PENDING",
    }
    fields.update(overrides)
    return DownloadJob(**fields)


class TestDownloadJobRoundTrip:
    def test_as_dict_from_dict_round_trip(self) -> None:
        job = make_job(
            status="RUNNING",
            attempt_count=2,
            last_error="timeout",
            rows_received=100,
            rows_valid=95,
            rows_invalid=5,
            coverage_pct=0.95,
        )
        restored = DownloadJob.from_dict(job.as_dict())
        assert restored == job

    def test_from_dict_tolerates_missing_optional_fields(self) -> None:
        minimal = {
            "job_id": "JOB-000002",
            "provider": "upstox_v3",
            "instrument_key": "X",
            "underlying": "NIFTY",
            "instrument_type": "OPTION",
            "interval_unit": "minutes",
            "interval_value": 5,
            "from_date": "2026-01-01",
            "to_date": "2026-01-02",
            "status": "PENDING",
        }
        job = DownloadJob.from_dict(minimal)
        assert job.attempt_count == 0
        assert job.rows_received == 0
        assert job.expiry is None


class TestDownloadJobStoreAtomicSave:
    def test_round_trips_through_disk(self, tmp_path) -> None:
        store = DownloadJobStore(tmp_path / "jobs.json")
        job = make_job()
        store.save([job])
        loaded = store.load()
        assert len(loaded) == 1
        assert loaded[0].job_id == job.job_id

    def test_missing_file_returns_empty_list_not_an_error(self, tmp_path) -> None:
        store = DownloadJobStore(tmp_path / "does_not_exist.json")
        assert store.load() == []

    def test_save_is_atomic_no_partial_file_on_crash(self, tmp_path, monkeypatch) -> None:
        path = tmp_path / "jobs.json"
        store = DownloadJobStore(path)
        store.save([make_job()])
        original_content = path.read_text()

        def boom(*args, **kwargs):
            raise OSError("simulated disk failure")

        monkeypatch.setattr(json, "dump", boom)
        with pytest.raises(OSError):
            store.save([make_job(job_id="JOB-000002")])

        assert path.read_text() == original_content
        leftovers = list(tmp_path.glob("*.tmp"))
        assert leftovers == []


class TestNextJobId:
    def test_sequential_generation(self, tmp_path) -> None:
        store = DownloadJobStore(tmp_path / "jobs.json")
        assert store.next_job_id() == "JOB-000001"
        store.upsert(make_job(job_id="JOB-000001"))
        assert store.next_job_id() == "JOB-000002"
        store.upsert(make_job(job_id="JOB-000002"))
        assert store.next_job_id() == "JOB-000003"

    def test_survives_gaps(self, tmp_path) -> None:
        store = DownloadJobStore(tmp_path / "jobs.json")
        store.upsert(make_job(job_id="JOB-000001"))
        store.upsert(make_job(job_id="JOB-000005"))
        assert store.next_job_id() == "JOB-000006"


class TestUpsert:
    def test_inserts_new_job(self, tmp_path) -> None:
        store = DownloadJobStore(tmp_path / "jobs.json")
        store.upsert(make_job())
        assert len(store.load()) == 1

    def test_replaces_existing_job_with_same_id(self, tmp_path) -> None:
        store = DownloadJobStore(tmp_path / "jobs.json")
        store.upsert(make_job(status="PENDING"))
        store.upsert(make_job(status="RUNNING"))
        jobs = store.load()
        assert len(jobs) == 1
        assert jobs[0].status == "RUNNING"


class TestPendingOrRetryable:
    def test_excludes_complete_invalid_blocked(self, tmp_path) -> None:
        store = DownloadJobStore(tmp_path / "jobs.json")
        statuses = ["PENDING", "RUNNING", "COMPLETE", "PARTIAL", "FAILED", "RETRYABLE", "BLOCKED", "INVALID"]
        for i, status in enumerate(statuses, start=1):
            store.upsert(make_job(job_id=f"JOB-{i:06d}", status=status))

        remaining = {job.status for job in store.pending_or_retryable()}
        assert remaining == {"PENDING", "RUNNING", "PARTIAL", "FAILED", "RETRYABLE"}

    def test_empty_store_returns_empty_list(self, tmp_path) -> None:
        store = DownloadJobStore(tmp_path / "jobs.json")
        assert store.pending_or_retryable() == []


class TestMarkTransitions:
    def test_mark_running_bumps_attempt_count_and_sets_started_at(self, tmp_path) -> None:
        store = DownloadJobStore(tmp_path / "jobs.json")
        store.upsert(make_job())
        store.mark_running("JOB-000001")
        job = store.load()[0]
        assert job.status == "RUNNING"
        assert job.attempt_count == 1
        assert job.started_at is not None

    def test_mark_complete_sets_fields(self, tmp_path) -> None:
        store = DownloadJobStore(tmp_path / "jobs.json")
        store.upsert(make_job())
        store.mark_complete(
            "JOB-000001", rows_received=10, rows_valid=10, rows_invalid=0, coverage_pct=1.0
        )
        job = store.load()[0]
        assert job.status == "COMPLETE"
        assert job.rows_received == 10
        assert job.coverage_pct == 1.0
        assert job.completed_at is not None

    def test_mark_failed_retryable_true_sets_retryable_not_failed(self, tmp_path) -> None:
        store = DownloadJobStore(tmp_path / "jobs.json")
        store.upsert(make_job())
        store.mark_failed("JOB-000001", "rate limited", retryable=True)
        job = store.load()[0]
        assert job.status == "RETRYABLE"
        assert job.last_error == "rate limited"
        assert job.attempt_count == 1

    def test_mark_failed_retryable_false_sets_failed(self, tmp_path) -> None:
        store = DownloadJobStore(tmp_path / "jobs.json")
        store.upsert(make_job())
        store.mark_failed("JOB-000001", "bad instrument key", retryable=False)
        job = store.load()[0]
        assert job.status == "FAILED"

    def test_mark_on_unknown_job_raises(self, tmp_path) -> None:
        store = DownloadJobStore(tmp_path / "jobs.json")
        with pytest.raises(KeyError):
            store.mark_running("JOB-999999")


class TestPartitionDateRange:
    def test_worked_example_400ish_day_range(self) -> None:
        parts = partition_date_range(date(2024, 1, 1), date(2025, 2, 4), 30)
        assert len(parts) == 14
        assert parts[0] == (date(2024, 1, 1), date(2024, 1, 30))
        assert parts[-1] == (date(2025, 1, 25), date(2025, 2, 4))

    def test_from_equals_to(self) -> None:
        parts = partition_date_range(date(2026, 1, 1), date(2026, 1, 1), 30)
        assert parts == [(date(2026, 1, 1), date(2026, 1, 1))]

    def test_range_shorter_than_max_span(self) -> None:
        parts = partition_date_range(date(2026, 1, 1), date(2026, 1, 5), 30)
        assert parts == [(date(2026, 1, 1), date(2026, 1, 5))]

    def test_max_span_of_one_day_produces_one_subrange_per_day(self) -> None:
        parts = partition_date_range(date(2026, 1, 1), date(2026, 1, 3), 1)
        assert parts == [
            (date(2026, 1, 1), date(2026, 1, 1)),
            (date(2026, 1, 2), date(2026, 1, 2)),
            (date(2026, 1, 3), date(2026, 1, 3)),
        ]

    def test_invalid_range_raises(self) -> None:
        with pytest.raises(ValueError):
            partition_date_range(date(2026, 1, 5), date(2026, 1, 1), 30)

    def test_invalid_max_span_raises(self) -> None:
        with pytest.raises(ValueError):
            partition_date_range(date(2026, 1, 1), date(2026, 1, 5), 0)

    @pytest.mark.parametrize(
        "start,end,max_span",
        [
            (date(2026, 1, 1), date(2026, 12, 31), 7),
            (date(2020, 1, 1), date(2026, 1, 1), 90),
            (date(2026, 6, 15), date(2026, 6, 15), 1),
            (date(2026, 1, 1), date(2026, 1, 2), 1),
            (date(2026, 1, 1), date(2026, 3, 1), 365),
        ],
    )
    def test_full_coverage_no_gaps_no_overlaps(self, start, end, max_span) -> None:
        parts = partition_date_range(start, end, max_span)
        assert parts[0][0] == start
        assert parts[-1][1] == end
        for s, e in parts:
            assert s <= e
            assert (e - s).days <= max_span - 1
        for (_, prev_end), (next_start, _) in zip(parts, parts[1:]):
            assert (next_start - prev_end).days == 1
