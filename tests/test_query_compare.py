"""Tests for the test-only ``get_requires`` query comparison interface."""

import os
from os.path import abspath, dirname, isdir
from os.path import join as pjoin
from pathlib import Path

import pytest
from pyproject_hooks import (
    BuildBackendHookCaller,
    quiet_subprocess_runner,
)

from tests.query_compare import (
    GET_REQUIRES_HOOKS,
    Scenario,
    compare_get_requires,
)

SAMPLES_DIR = pjoin(dirname(abspath(__file__)), "samples")
BUILDSYS_PKGS = pjoin(SAMPLES_DIR, "buildsys_pkgs")

# A backend which contaminates its source tree while answering get_requires.
# The first wheel query in a fresh tree writes a marker file and returns the
# clean requirement list; a later query in the same tree notices the marker
# and returns a distorted list.  It also records files written before a
# configured failure, and its metadata/build hooks must never be invoked.
BACKEND_SOURCE = """
import os

MARKER = "generated-by-get-requires.txt"


def _apply_effects(config_settings):
    for item in config_settings.get("write", []):
        with open(item["path"], "w", encoding="utf-8") as f:
            f.write(item["data"])
    for path in config_settings.get("delete", []):
        os.remove(path)


def get_requires_for_build_wheel(config_settings):
    config_settings = config_settings or {}
    _apply_effects(config_settings)
    if config_settings.get("fail"):
        with open(MARKER, "w", encoding="utf-8") as f:
            f.write("written before failure\\n")
        raise RuntimeError("intentional backend failure")
    mode = config_settings.get("mode")
    if mode == "dups":
        return ["alpha", "beta", "alpha"]
    if mode == "reorder":
        return ["beta", "alpha", "alpha"]
    if mode == "members":
        return ["alpha", "gamma"]
    if os.path.exists(MARKER):
        return ["alpha", "beta", "contaminated"]
    with open(MARKER, "w", encoding="utf-8") as f:
        f.write("polluted\\n")
    return ["alpha", "beta"]


def _forbidden(*args, **kwargs):
    raise AssertionError("metadata/build hooks must not be called")


prepare_metadata_for_build_wheel = _forbidden
prepare_metadata_for_build_editable = _forbidden
build_wheel = _forbidden
build_editable = _forbidden
build_sdist = _forbidden
"""

PYPROJECT = """
[build-system]
requires = []
build-backend = "query_backend"
backend-path = ["backend"]
"""


def make_project(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(PYPROJECT.lstrip(), encoding="utf-8")
    (root / "SOURCE.md").write_text("original source\n", encoding="utf-8")
    backend_dir = root / "backend"
    backend_dir.mkdir()
    (backend_dir / "query_backend.py").write_text(
        BACKEND_SOURCE.lstrip(), encoding="utf-8"
    )
    return str(root)


def recording_runner(copy_cwds, interrupt_on=None):
    """Runner remembering copy cwds, optionally raising KeyboardInterrupt."""

    def runner(cmd, cwd=None, extra_environ=None):
        copy_cwds.append(cwd)
        if interrupt_on is not None and len(copy_cwds) == interrupt_on:
            raise KeyboardInterrupt
        quiet_subprocess_runner(cmd, cwd=cwd, extra_environ=extra_environ)

    return runner


def test_independent_copies_keep_scenarios_pristine(tmp_path):
    project = make_project(tmp_path / "project")
    copy_cwds = []

    report = compare_get_requires(
        project,
        "query_backend",
        [Scenario("one"), Scenario("two")],
        backend_path=["backend"],
        runner=recording_runner(copy_cwds),
    )

    # Each scenario ran in its own copy and saw the tree as first query.
    assert len(copy_cwds) == 2
    assert len(set(copy_cwds)) == 2
    for name in ("one", "two"):
        result = report.result(name)
        assert result.ok
        assert result.requirements == ["alpha", "beta"]
        # The marker the backend wrote was confined to the scenario copy.
        assert result.changes.added["generated-by-get-requires.txt"] == b"polluted\n"

    # The original tree was never used as the subprocess working directory.
    assert project not in copy_cwds
    assert not os.path.exists(pjoin(project, "generated-by-get-requires.txt"))

    # Copies are gone, but the captured report content is still viewable.
    assert all(not isdir(cwd) for cwd in copy_cwds)
    text = report.to_text()
    assert "Scenario 'one'" in text
    assert "added: generated-by-get-requires.txt" in text
    assert "| polluted" in text


def test_repeated_queries_without_copies_do_contaminate(tmp_path):
    # Documents the hazard: two queries in one tree distort each other.
    project = make_project(tmp_path / "contaminated")
    hooks = BuildBackendHookCaller(project, "query_backend", ["backend"])

    assert hooks.get_requires_for_build_wheel({}) == ["alpha", "beta"]
    assert os.path.exists(pjoin(project, "generated-by-get-requires.txt"))
    assert hooks.get_requires_for_build_wheel({}) == [
        "alpha",
        "beta",
        "contaminated",
    ]


def test_members_duplicates_order_and_membership_differences(tmp_path):
    project = make_project(tmp_path / "project")

    report = compare_get_requires(
        project,
        "query_backend",
        [
            Scenario("plain"),
            Scenario("dups", config_settings={"mode": "dups"}),
            Scenario("reorder", config_settings={"mode": "reorder"}),
            Scenario("members", config_settings={"mode": "members"}),
        ],
        backend_path=["backend"],
        runner=recording_runner([]),
    )

    plain = report.result("plain")
    assert plain.requirements == ["alpha", "beta"]
    assert plain.members == ["alpha", "beta"]
    assert plain.duplicates == {}

    dups = report.result("dups")
    # The original list is preserved, alongside member and repeat analysis.
    assert dups.requirements == ["alpha", "beta", "alpha"]
    assert dups.members == ["alpha", "beta"]
    assert dups.duplicates == {"alpha": 1}
    assert dups.changes.is_clean()

    reorder = report.result("reorder")
    assert reorder.requirements == ["beta", "alpha", "alpha"]
    assert reorder.duplicates == {"alpha": 1}
    assert reorder.members == ["beta", "alpha"]

    assert len(report.order_differences) == 1
    difference = report.order_differences[0]
    assert difference.reference == "plain"
    assert difference.scenario == "reorder"
    assert difference.reference_order == ["alpha", "beta"]
    assert difference.scenario_order == ["beta", "alpha"]
    assert difference.reordered == ["alpha", "beta"]

    assert len(report.membership_differences) == 1
    membership = report.membership_differences[0]
    assert membership.reference == "plain"
    assert membership.scenario == "members"
    assert membership.only_in_reference == ["beta"]
    assert membership.only_in_scenario == ["gamma"]

    assert not report.interrupted
    assert all(result.ok for result in report.scenarios)
    assert "order differs: 'reorder'" in report.to_text()
    assert "membership differs: 'members'" in report.to_text()


def test_file_add_modify_delete_captured_with_content(tmp_path):
    project = make_project(tmp_path / "project")

    report = compare_get_requires(
        project,
        "query_backend",
        [
            Scenario(
                "add_and_modify",
                config_settings={
                    "write": [
                        {"path": "extra.txt", "data": "hello\n"},
                        {"path": "SOURCE.md", "data": "changed source\n"},
                    ]
                },
            ),
            Scenario("delete", config_settings={"delete": ["SOURCE.md"]}),
        ],
        backend_path=["backend"],
        runner=recording_runner([]),
    )

    add_modify = report.result("add_and_modify")
    assert add_modify.changes.added["extra.txt"] == b"hello\n"
    assert add_modify.changes.modified["SOURCE.md"] == {
        "before": b"original source\n",
        "after": b"changed source\n",
    }

    deleted = report.result("delete")
    assert deleted.changes.deleted["SOURCE.md"] == b"original source\n"


def test_scenario_failure_recorded_and_run_continues(tmp_path):
    project = make_project(tmp_path / "project")

    report = compare_get_requires(
        project,
        "query_backend",
        [
            Scenario("before"),
            Scenario("boom", config_settings={"fail": True}),
            Scenario("after"),
        ],
        backend_path=["backend"],
        runner=recording_runner([]),
    )

    boom = report.result("boom")
    assert not boom.ok
    assert boom.requirements is None
    assert boom.members is None
    assert boom.error["type"] == "CalledProcessError"
    # Changes made before the failure are still captured on the copy.
    assert (
        boom.changes.added["generated-by-get-requires.txt"]
        == b"written before failure\n"
    )

    assert report.result("before").requirements == ["alpha", "beta"]
    assert report.result("after").requirements == ["alpha", "beta"]
    assert not report.interrupted


def test_keyboard_interrupt_stops_cleans_up_and_keeps_results(tmp_path):
    project = make_project(tmp_path / "project")
    copy_cwds = []

    report = compare_get_requires(
        project,
        "query_backend",
        [Scenario("one"), Scenario("two"), Scenario("three")],
        backend_path=["backend"],
        runner=recording_runner(copy_cwds, interrupt_on=2),
    )

    assert report.interrupted is True
    # The third scenario never ran.
    assert [result.name for result in report.scenarios] == ["one", "two"]
    assert report.result("one").requirements == ["alpha", "beta"]
    in_flight = report.result("two")
    assert in_flight.interrupted
    assert not in_flight.ok

    # Every copy, including the interrupted one, has been removed.
    assert len(copy_cwds) == 2
    assert all(not isdir(cwd) for cwd in copy_cwds)

    # Results gathered before the interruption are still viewable.
    text = report.to_text()
    assert "interrupted: yes" in text
    assert (
        report.result("one").changes.added["generated-by-get-requires.txt"]
        == b"polluted\n"
    )
    assert "interrupted before it finished" in text


def test_only_get_requires_hooks_are_allowed(tmp_path):
    project = make_project(tmp_path / "project")
    assert GET_REQUIRES_HOOKS == {
        "get_requires_for_build_wheel",
        "get_requires_for_build_sdist",
        "get_requires_for_build_editable",
    }
    with pytest.raises(ValueError, match="disallowed hook"):
        compare_get_requires(
            project,
            "query_backend",
            [Scenario("bad", hook="build_wheel")],
            backend_path=["backend"],
        )


@pytest.mark.parametrize(
    ("scenarios", "message"),
    [
        ([], "at least one scenario"),
        (
            [Scenario("same"), Scenario("same")],
            "duplicate scenario name",
        ),
    ],
)
def test_preflight_rejects_bad_scenarios(tmp_path, scenarios, message):
    project = make_project(tmp_path / "project")
    with pytest.raises(ValueError, match=message):
        compare_get_requires(project, "query_backend", scenarios)


def test_preflight_rejects_missing_source_and_reference(tmp_path):
    project = make_project(tmp_path / "project")
    with pytest.raises(NotADirectoryError):
        compare_get_requires(
            str(tmp_path / "missing"),
            "query_backend",
            [Scenario("one")],
        )
    with pytest.raises(ValueError, match="unknown reference scenario"):
        compare_get_requires(
            project,
            "query_backend",
            [Scenario("one")],
            backend_path=["backend"],
            reference="nonexistent",
        )


def test_preflight_runs_before_any_copy(tmp_path):
    project = make_project(tmp_path / "project")
    copy_cwds = []
    with pytest.raises(ValueError):
        compare_get_requires(
            project,
            "query_backend",
            [Scenario("one"), Scenario("one")],
            backend_path=["backend"],
            runner=recording_runner(copy_cwds),
        )
    assert copy_cwds == []


def test_compare_real_sample_pkg1(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", BUILDSYS_PKGS)
    source = pjoin(SAMPLES_DIR, "pkg1")

    report = compare_get_requires(
        source,
        "buildsys",
        [
            Scenario("wheel"),
            Scenario("sdist", hook="get_requires_for_build_sdist"),
            Scenario("editable", hook="get_requires_for_build_editable"),
        ],
        runner=quiet_subprocess_runner,
    )

    assert report.result("wheel").requirements == ["wheelwright"]
    assert report.result("sdist").requirements == ["frog"]
    assert report.result("editable").requirements == [
        "wheelwright",
        "editables",
    ]
    assert all(result.changes.is_clean() for result in report.scenarios)
    assert not report.interrupted
