from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
RELEASE = yaml.load((WORKFLOWS / "release.yml").read_text(), Loader=yaml.BaseLoader)
PUBLISH = RELEASE["jobs"]["publish-images"]
GUARD = next(step for step in PUBLISH["steps"] if step.get("id") == "current")
OLD, NEW = "a" * 40, "b" * 40


def test_failed_or_skipped_required_checks_cannot_be_bypassed():
    # GitHub's default needs semantics skip publishing on any dependency failure/skip.
    required = {"backend-ci", "frontend-ci", "docker-ci"}
    assert set(PUBLISH["needs"]) == required
    assert "if" not in PUBLISH
    for name in required:
        caller = RELEASE["jobs"][name]
        assert caller == {"uses": f"./.github/workflows/{name}.yml"}
        workflow = yaml.load((WORKFLOWS / f"{name}.yml").read_text(), Loader=yaml.BaseLoader)
        assert "workflow_call" in workflow["on"]
        assert "push" not in workflow["on"]
        for job in workflow["jobs"].values():
            for item in [job, *job["steps"]]:
                assert "continue-on-error" not in item
                assert "if" not in item
                if item.get("uses", "").startswith("actions/checkout@"):
                    assert "ref" not in item.get("with", {})


def test_tag_writes_are_serialized_and_guarded():
    assert PUBLISH["concurrency"] == {
        "group": "release-publish-main",
        "queue": "max",
        "cancel-in-progress": "false",
    }
    assert PUBLISH["steps"][1] == GUARD
    assert GUARD["shell"] == "bash"
    for step in PUBLISH["steps"][2:]:
        assert step["if"] == "steps.current.outputs.publish == 'true'"


@pytest.fixture
def run_guard(tmp_path, monkeypatch):
    git = tmp_path / "git"
    git.write_text(
        "#!/bin/sh\n"
        "test \"$*\" = 'ls-remote --exit-code origin refs/heads/main' || exit 2\n"
        'test "$TEST_MAIN_SHA" != unavailable || exit 128\n'
        "printf '%s\\trefs/heads/main\\n' \"$TEST_MAIN_SHA\"\n"
    )
    git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary"))

    def run(revision, main):
        output.write_text("")
        monkeypatch.setenv("GITHUB_SHA", revision)
        monkeypatch.setenv("TEST_MAIN_SHA", main)
        result = subprocess.run(
            ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", GUARD["run"]],
            check=False,
            capture_output=True,
            text=True,
        )
        return result.returncode, output.read_text().strip() == "publish=true"

    return run


@pytest.mark.parametrize(
    ("attempts", "expected"),
    [
        ([(OLD, OLD), (NEW, NEW)], [OLD, NEW]),
        ([(NEW, NEW), (OLD, NEW)], [NEW]),
        ([(OLD, NEW), (NEW, NEW)], [NEW]),
        ([(NEW, NEW), (OLD, NEW), (NEW, NEW)], [NEW, NEW]),
    ],
)
def test_publication_order_never_moves_latest_backwards(run_guard, attempts, expected):
    published = []
    for revision, main in attempts:
        returncode, publish = run_guard(revision, main)
        assert returncode == 0
        if publish:
            published.append(revision)
    assert published == expected


def test_main_lookup_failure_prevents_publication(run_guard):
    returncode, publish = run_guard(OLD, "unavailable")
    assert returncode != 0
    assert not publish
