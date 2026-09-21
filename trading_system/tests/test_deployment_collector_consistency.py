"""Phase 3.6 Section G divergence guard.

Phase 3.5 hardened ``scripts/record_chains.py`` (checkpointing via the
``cycles`` table, startup gap reporting, partial-failure logging, credential
redaction before any broker session is constructed) but Render was actually
deploying ``scripts/simple_data_collector.py`` via ``start_data_collector.sh``
-- a completely different, unhardened collector that writes to a different
database (``data/option_chains.db``, the ``recorder/storage.py`` schema)
and never calls ``secure_third_party_loggers()``. See
``PHASE_3_6_SECTION_G_COLLECTOR_RECONCILIATION.md`` for the full comparison.

This test parses the real deployment script and fails loudly if the
data-collection worker is ever pointed at anything other than the
explicitly-declared canonical hardened collector. If someone deliberately
wants to change the deployed collector, they must update
``EXPECTED_COLLECTOR_SCRIPT`` below in the same change -- that is the point:
a conscious decision, not a silent regression back to the unhardened path.

No real broker connection, no credentials, no live trading code touched.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_SCRIPT = REPO_ROOT / "start_data_collector.sh"
RENDER_YAML = REPO_ROOT / "render.yaml"

# The one script Render's `data-collection-system` worker is allowed to run,
# as of Phase 3.6 Section G's reconciliation. Changing this constant is the
# "conscious decision" gate: it must be a deliberate edit made alongside a
# real re-assessment, not an incidental drift.
EXPECTED_COLLECTOR_SCRIPT = "scripts/record_chains.py"

# Matches a `python scripts/<name>.py ...` invocation line, capturing the
# script path. Deliberately tolerant of leading whitespace/`python3`/args.
_PYTHON_INVOCATION_RE = re.compile(
    r"python3?\s+(scripts/[A-Za-z0-9_./-]+\.py)(?:\s|$)"
)


def _extract_invoked_script(shell_script_text: str) -> str:
    """Return the last `scripts/*.py` path a shell script hands to python.

    Ignores comment lines (starting with ``#`` once leading whitespace is
    stripped) so a mention of the old script inside an explanatory comment
    does not get mistaken for the actual invocation.

    Args:
        shell_script_text: Full contents of a startup shell script.

    Returns:
        The script path (e.g. ``"scripts/record_chains.py"``) actually
        invoked.

    Raises:
        AssertionError: If no python invocation of a ``scripts/*.py`` file
            is found at all -- that itself is a deployment-surface problem
            worth failing loudly on, not silently ignoring.
    """
    matches: list[str] = []
    for line in shell_script_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        match = _PYTHON_INVOCATION_RE.search(line)
        if match:
            matches.append(match.group(1))

    assert matches, (
        f"No `python scripts/<name>.py` invocation found in "
        f"{DEPLOY_SCRIPT.name}. The deployment surface changed shape; "
        f"update this test to match how the collector is now launched."
    )
    # The real invocation is the one actually executed, not any earlier
    # mention; take the last match in the (non-comment) script body.
    return matches[-1]


class TestDeployedCollectorMatchesHardenedScript:
    """Guards against `render.yaml`/`start_data_collector.sh` silently
    reverting to the unhardened `simple_data_collector.py` path."""

    def test_start_data_collector_sh_invokes_the_canonical_hardened_collector(self) -> None:
        assert DEPLOY_SCRIPT.exists(), f"{DEPLOY_SCRIPT} not found"
        text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        invoked = _extract_invoked_script(text)
        assert invoked == EXPECTED_COLLECTOR_SCRIPT, (
            f"start_data_collector.sh invokes '{invoked}', but the "
            f"canonical hardened collector is '{EXPECTED_COLLECTOR_SCRIPT}' "
            f"(core/chain_recorder.py::ChainRecorder, with checkpointing, "
            f"startup-gap reporting, partial-failure logging, and "
            f"credential redaction). If this divergence is deliberate, "
            f"update EXPECTED_COLLECTOR_SCRIPT in this test as part of the "
            f"same change, with a documented reason -- do not just let "
            f"this test start failing."
        )

    def test_render_yaml_data_collection_worker_still_delegates_to_start_data_collector_sh(
        self,
    ) -> None:
        """`render.yaml` itself only names the shell script, not the Python
        entry point -- but if that ever changes (e.g. someone points
        `startCommand` straight at a python invocation), this test should
        still be checking the *actual* command Render will run, not a
        stale assumption. This asserts the current indirection remains in
        place so the test above is still checking the right file."""
        assert RENDER_YAML.exists(), f"{RENDER_YAML} not found"
        text = RENDER_YAML.read_text(encoding="utf-8")

        # Find the data-collection-system service block and its startCommand.
        service_match = re.search(
            r"name:\s*data-collection-system.*?startCommand:\s*(.+)",
            text,
            re.DOTALL,
        )
        assert service_match, (
            "Could not find a `data-collection-system` worker with a "
            "startCommand in render.yaml; the deployment surface changed "
            "shape and this test (and the one above) need updating."
        )
        start_command = service_match.group(1).splitlines()[0].strip()
        assert "start_data_collector.sh" in start_command, (
            f"render.yaml's data-collection-system worker now runs "
            f"'{start_command}', not start_data_collector.sh. "
            f"test_start_data_collector_sh_invokes_the_canonical_hardened_collector "
            f"is checking the wrong file if this is true -- update both."
        )


class TestDivergenceGuardActuallyCatchesRegressions:
    """Proves the guard above is not a tautology: it must fail when the
    deployed script points at the old unhardened collector, and pass when
    it points at the hardened one. Both directions are exercised here,
    against scratch shell-script text, so the guard's own correctness does
    not depend on trusting a single assertion."""

    @pytest.mark.parametrize(
        "invocation_line",
        [
            "python scripts/simple_data_collector.py --symbol NIFTY --interval 60",
            "python3 scripts/simple_data_collector.py --symbol NIFTY --interval 60",
            "  python scripts/simple_data_collector.py --symbol NIFTY --interval 60",
        ],
    )
    def test_regressing_to_the_unhardened_script_is_detected(self, invocation_line: str) -> None:
        fake_script = (
            "#!/bin/bash\n"
            "echo starting\n"
            f"{invocation_line}\n"
            "exit $?\n"
        )
        invoked = _extract_invoked_script(fake_script)
        assert invoked != EXPECTED_COLLECTOR_SCRIPT
        assert invoked == "scripts/simple_data_collector.py"

    def test_the_hardened_script_is_accepted(self) -> None:
        fake_script = (
            "#!/bin/bash\n"
            "echo starting\n"
            "python scripts/record_chains.py --symbols NIFTY BANKNIFTY --interval 60\n"
            "exit $?\n"
        )
        invoked = _extract_invoked_script(fake_script)
        assert invoked == EXPECTED_COLLECTOR_SCRIPT

    def test_a_comment_mentioning_the_old_script_is_not_mistaken_for_the_invocation(
        self,
    ) -> None:
        fake_script = (
            "#!/bin/bash\n"
            "# used to run: python scripts/simple_data_collector.py\n"
            "python scripts/record_chains.py --symbols NIFTY BANKNIFTY --interval 60\n"
        )
        invoked = _extract_invoked_script(fake_script)
        assert invoked == EXPECTED_COLLECTOR_SCRIPT

    def test_missing_invocation_entirely_fails_loudly_rather_than_silently(self) -> None:
        fake_script = "#!/bin/bash\necho 'no python invocation here'\n"
        with pytest.raises(AssertionError):
            _extract_invoked_script(fake_script)
