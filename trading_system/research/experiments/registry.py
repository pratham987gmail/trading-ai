"""Append-only experiment tracking.

A failed experiment is exactly as valuable as a successful one for the
question this phase exists to answer ("does a robust edge exist"), so this
registry never deletes a record and never provides an update-in-place that
could be used to quietly rewrite a past result. The only mutation permitted
is appending a new experiment, which may reference an earlier one as its
baseline.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

Decision = Literal["ACCEPT", "REJECT", "INCONCLUSIVE"]


@dataclass(frozen=True)
class Experiment:
    """One recorded research experiment.

    Attributes:
        experiment_id: ``EXP-00001`` style, assigned by the registry.
        hypothesis: What is being tested, in one sentence.
        baseline: Description or experiment_id of what the candidate is
            compared against.
        candidate: Description of what changed.
        dataset_version: References
            ``research.data.models.DatasetVersion.version_id``.
        parameters: Every parameter needed to reproduce the run.
        model: Model name/version, when ML was involved. ``None`` for a
            purely rule-based experiment.
        cost_model_version: Identifies which cost assumptions were used.
        walk_forward_config: The walk-forward window configuration, when used.
        result: The metrics payload (typically
            ``TradingMetrics.as_dict()`` or a leaderboard row), stored
            verbatim so the record is self-contained without a live database
            lookup.
        decision: ``ACCEPT``, ``REJECT``, or ``INCONCLUSIVE``. Never inferred
            silently — the caller states it, and states why in ``notes``.
        notes: Free-form rationale for the decision.
        created_at: When this was recorded.
        random_seed: For reproducibility, when the experiment involved
            randomness (Monte Carlo, a train/test split, a model's own RNG).
    """

    experiment_id: str
    hypothesis: str
    baseline: str
    candidate: str
    dataset_version: str
    parameters: dict[str, Any]
    result: dict[str, Any]
    decision: Decision
    notes: str = ""
    model: str | None = None
    cost_model_version: str = "v1"
    walk_forward_config: dict[str, Any] | None = None
    random_seed: int | None = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def as_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "hypothesis": self.hypothesis,
            "baseline": self.baseline,
            "candidate": self.candidate,
            "dataset_version": self.dataset_version,
            "parameters": self.parameters,
            "model": self.model,
            "cost_model_version": self.cost_model_version,
            "walk_forward_config": self.walk_forward_config,
            "result": self.result,
            "decision": self.decision,
            "notes": self.notes,
            "random_seed": self.random_seed,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Experiment":
        return cls(
            experiment_id=data["experiment_id"],
            hypothesis=data["hypothesis"],
            baseline=data["baseline"],
            candidate=data["candidate"],
            dataset_version=data["dataset_version"],
            parameters=data.get("parameters", {}),
            result=data.get("result", {}),
            decision=data["decision"],
            notes=data.get("notes", ""),
            model=data.get("model"),
            cost_model_version=data.get("cost_model_version", "v1"),
            walk_forward_config=data.get("walk_forward_config"),
            random_seed=data.get("random_seed"),
            created_at=data.get("created_at", datetime.now().isoformat()),
        )


class ExperimentRegistry:
    """JSON-backed, append-only store of experiments.

    Args:
        path: File to persist to. Created on first append.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def load(self) -> list[Experiment]:
        """Return every recorded experiment, oldest first."""
        if not self.path.exists():
            return []
        with open(self.path, encoding="utf-8") as f:
            data = json.load(f)
        return [Experiment.from_dict(e) for e in data.get("experiments", [])]

    def next_id(self) -> str:
        """Return the next sequential experiment id, e.g. ``EXP-00007``."""
        existing = self.load()
        numbers = []
        for exp in existing:
            try:
                numbers.append(int(exp.experiment_id.split("-")[-1]))
            except (ValueError, IndexError):
                continue
        next_number = (max(numbers) + 1) if numbers else 1
        return f"EXP-{next_number:05d}"

    def append(
        self,
        *,
        hypothesis: str,
        baseline: str,
        candidate: str,
        dataset_version: str,
        parameters: dict[str, Any],
        result: dict[str, Any],
        decision: Decision,
        notes: str = "",
        model: str | None = None,
        cost_model_version: str = "v1",
        walk_forward_config: dict[str, Any] | None = None,
        random_seed: int | None = None,
        experiment_id: str | None = None,
    ) -> Experiment:
        """Record a new experiment. Never overwrites or removes an existing one.

        Args:
            experiment_id: Explicit id, or auto-assigned via :meth:`next_id`
                when omitted.
            (remaining args): See :class:`Experiment`.

        Returns:
            The recorded experiment.

        Raises:
            ValueError: When ``experiment_id`` collides with an existing one.
        """
        existing = self.load()
        eid = experiment_id or self.next_id()
        if any(e.experiment_id == eid for e in existing):
            raise ValueError(
                f"Experiment '{eid}' already exists. The registry is append-only; use a "
                "new id rather than overwriting a past result."
            )

        experiment = Experiment(
            experiment_id=eid,
            hypothesis=hypothesis,
            baseline=baseline,
            candidate=candidate,
            dataset_version=dataset_version,
            parameters=parameters,
            result=result,
            decision=decision,
            notes=notes,
            model=model,
            cost_model_version=cost_model_version,
            walk_forward_config=walk_forward_config,
            random_seed=random_seed,
        )
        existing.append(experiment)
        self._save(existing)
        return experiment

    def get(self, experiment_id: str) -> Experiment | None:
        for exp in self.load():
            if exp.experiment_id == experiment_id:
                return exp
        return None

    def by_decision(self, decision: Decision) -> list[Experiment]:
        return [e for e in self.load() if e.decision == decision]

    def _save(self, experiments: list[Experiment]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"experiments": [e.as_dict() for e in experiments]}
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
