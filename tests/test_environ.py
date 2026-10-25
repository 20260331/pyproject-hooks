"""Tests for the per-hook environment envelope.

The envelope lets a hook call explicitly inherit, set and delete environment
variables without mutating the frontend process' ``os.environ``, and ensures
that backend import, hook execution and any fallback all see one frozen
snapshot.
"""
import importlib.util
import json
import os
import subprocess
from os.path import abspath
from os.path import join as pjoin
from types import MappingProxyType
from unittest.mock import Mock

import pytest
from testpath.tempdir import TemporaryDirectory

from pyproject_hooks import BuildBackendHookCaller, quiet_subprocess_runner
from pyproject_hooks._in_process import _in_proc_script_path

SET_VAR = "ENVELOPE_TEST_VAR"
INHERITED_VAR = "ENVELOPE_TEST_INHERITED"
ABSENT = "<absent>"

PYPROJECT_TOML = """
[build-system]
requires = []
build-backend = "envbackend"
backend-path = ["."]
"""

# A tiny in-tree backend which records the environment both while it is being
# imported and while its hooks run. It deliberately does not define
# prepare_metadata_for_build_wheel, so the wheel fallback can be exercised.
ENV_BACKEND = r"""
import json
import os
import zipfile

SET_VAR = "ENVELOPE_TEST_VAR"
INHERITED_VAR = "ENVELOPE_TEST_INHERITED"
ABSENT = "<absent>"

# Captured at import time, before any hook has a chance to run.
seen_at_import = os.environ.get(SET_VAR, ABSENT)
inherited_at_import = os.environ.get(INHERITED_VAR, ABSENT)


def env_snapshot():
    return {
        "set_import": seen_at_import,
        "set_hook": os.environ.get(SET_VAR, ABSENT),
        "inherited_import": inherited_at_import,
        "inherited_hook": os.environ.get(INHERITED_VAR, ABSENT),
    }


def get_requires_for_build_wheel(config_settings):
    return env_snapshot()


get_requires_for_build_editable = get_requires_for_build_wheel
get_requires_for_build_sdist = get_requires_for_build_wheel


def build_wheel(wheel_directory, config_settings, metadata_directory=None):
    name = "envpkg-0.5-py3-none-any.whl"
    distinfo = "envpkg-0.5.dist-info"
    with zipfile.ZipFile(os.path.join(wheel_directory, name), "w") as zf:
        zf.writestr(distinfo + "/METADATA", "")
        zf.writestr(distinfo + "/WHEEL", "")
    with open(
        os.path.join(wheel_directory, "env-at-build.json"), "w", encoding="utf-8"
    ) as f:
        json.dump(env_snapshot(), f)
    return name


build_editable = build_wheel


def build_sdist(sdist_directory, config_settings):
    return "envpkg-0.5.tar.gz"
"""


@pytest.fixture
def env_project(tmp_path):
    (tmp_path / "pyproject.toml").write_text(PYPROJECT_TOML, encoding="utf-8")
    (tmp_path / "envbackend.py").write_text(ENV_BACKEND, encoding="utf-8")
    return tmp_path


def make_caller(project_dir, **kwargs):
    kwargs.setdefault("backend_path", ["."])
    return BuildBackendHookCaller(str(project_dir), "envbackend", **kwargs)


def assert_expected_snapshot(data, set_value, inherited_value):
    assert data == {
        "set_import": set_value,
        "set_hook": set_value,
        "inherited_import": inherited_value,
        "inherited_hook": inherited_value,
    }


def test_environ_set_at_construction(env_project, monkeypatch):
    monkeypatch.setenv(INHERITED_VAR, "inherited-value")
    hooks = make_caller(env_project, environ={SET_VAR: "set-value"})

    data = hooks.get_requires_for_build_wheel({})

    assert_expected_snapshot(data, "set-value", "inherited-value")


def test_environ_inherits_variables_absent_at_construction_time(
    env_project, monkeypatch
):
    # The inheritance base is sampled when the hook is *called*, not when the
    # caller is constructed.
    monkeypatch.delenv(INHERITED_VAR, raising=False)
    hooks = make_caller(env_project)

    monkeypatch.setenv(INHERITED_VAR, "set-later")
    data = hooks.get_requires_for_build_wheel({})

    assert data["inherited_hook"] == "set-later"


def test_environ_per_call_overrides(env_project):
    hooks = make_caller(env_project)

    first = hooks.get_requires_for_build_wheel({}, environ={SET_VAR: "one"})
    second = hooks.get_requires_for_build_wheel({}, environ={SET_VAR: "two"})

    assert first["set_hook"] == "one"
    assert second["set_hook"] == "two"


def test_environ_per_call_takes_precedence_over_constructor(env_project):
    hooks = make_caller(env_project, environ={SET_VAR: "from-caller"})

    data = hooks.get_requires_for_build_wheel({}, environ={SET_VAR: "from-call"})

    assert data["set_hook"] == "from-call"


def test_environ_none_deletes_inherited_variable(env_project, monkeypatch):
    monkeypatch.setenv(SET_VAR, "should-not-leak")
    hooks = make_caller(env_project, environ={SET_VAR: None})

    data = hooks.get_requires_for_build_wheel({})

    assert_expected_snapshot(data, ABSENT, ABSENT)


def test_environ_does_not_mutate_parent_process(env_project, monkeypatch):
    monkeypatch.setenv(SET_VAR, "parent-value")
    monkeypatch.setenv("ENVELOPE_TEST_DROP", "parent-only")
    hooks = make_caller(
        env_project,
        environ={SET_VAR: "child-value", "ENVELOPE_TEST_DROP": None},
    )
    before = dict(os.environ)

    hooks.get_requires_for_build_wheel({})

    after = dict(os.environ)
    assert before == after
    assert os.environ[SET_VAR] == "parent-value"
    assert os.environ["ENVELOPE_TEST_DROP"] == "parent-only"


def test_environ_isolated_between_parallel_callers(env_project, tmp_path_factory):
    other = tmp_path_factory.mktemp("other-project")
    (other / "pyproject.toml").write_text(PYPROJECT_TOML, encoding="utf-8")
    (other / "envbackend.py").write_text(ENV_BACKEND, encoding="utf-8")

    first_caller = make_caller(env_project, environ={SET_VAR: "first"})
    second_caller = make_caller(other, environ={SET_VAR: "second"})

    first = first_caller.get_requires_for_build_wheel({})
    second = second_caller.get_requires_for_build_wheel({})
    first_again = first_caller.get_requires_for_build_sdist({})

    assert first["set_hook"] == "first"
    assert second["set_hook"] == "second"
    assert first_again["set_hook"] == "first"


def test_environ_applies_to_sdist_and_editable(env_project):
    hooks = make_caller(env_project, environ={SET_VAR: "editable"})

    assert hooks.get_requires_for_build_editable({})["set_hook"] == "editable"
    assert hooks.get_requires_for_build_sdist({})["set_hook"] == "editable"


def test_environ_fallback_uses_same_snapshot(env_project, monkeypatch):
    monkeypatch.setenv(INHERITED_VAR, "inherited-value")
    hooks = make_caller(env_project, environ={SET_VAR: "fallback-value"})

    with TemporaryDirectory() as metadatadir:
        distinfo = hooks.prepare_metadata_for_build_wheel(metadatadir, {})
        assert distinfo == "envpkg-0.5.dist-info"
        with open(pjoin(metadatadir, "env-at-build.json"), encoding="utf-8") as f:
            data = json.load(f)

    # Import, hook and the build_wheel fallback share one snapshot.
    assert_expected_snapshot(data, "fallback-value", "inherited-value")


def test_environ_fallback_honours_deleted_variable(env_project, monkeypatch):
    monkeypatch.setenv(SET_VAR, "should-not-leak")
    hooks = make_caller(env_project, environ={SET_VAR: None})

    with TemporaryDirectory() as metadatadir:
        hooks.prepare_metadata_for_build_wheel(metadatadir, {})
        with open(pjoin(metadatadir, "env-at-build.json"), encoding="utf-8") as f:
            data = json.load(f)

    assert data["set_import"] == ABSENT
    assert data["set_hook"] == ABSENT


def test_runner_receives_full_envelope_snapshot(env_project, monkeypatch):
    monkeypatch.setenv("ENVELOPE_TEST_KEEP", "kept")
    monkeypatch.setenv("ENVELOPE_TEST_DROP", "dropped")
    runner = Mock()
    hooks = make_caller(
        env_project,
        runner=runner,
        environ={SET_VAR: "snapshot", "ENVELOPE_TEST_DROP": None},
    )

    with pytest.raises(FileNotFoundError):
        # The mock never executes the subprocess, so output.json is missing.
        hooks.get_requires_for_build_wheel({})

    assert runner.call_args.kwargs["cwd"] == abspath(str(env_project))
    env = runner.call_args.kwargs["env"]
    assert isinstance(env, MappingProxyType)
    assert env[SET_VAR] == "snapshot"
    assert env["ENVELOPE_TEST_KEEP"] == "kept"
    assert "ENVELOPE_TEST_DROP" not in env
    assert env["_PYPROJECT_HOOKS_BUILD_BACKEND"] == "envbackend"
    # Unrelated inherited environment is passed through unchanged.
    assert env["PATH"] == os.environ["PATH"]
    assert runner.call_args.kwargs["extra_environ"] is None


def test_quiet_runner_uses_envelope(env_project):
    hooks = make_caller(
        env_project,
        runner=quiet_subprocess_runner,
        environ={SET_VAR: "quiet"},
    )

    data = hooks.get_requires_for_build_wheel({})

    assert data["set_hook"] == "quiet"


def test_legacy_runner_without_env_parameter(env_project):
    captured = {}

    def legacy_runner(cmd, cwd=None, extra_environ=None):
        # The old calling convention: copy os.environ and overlay extras.
        env = os.environ.copy()
        env.update(extra_environ or {})
        captured["extra_environ"] = dict(extra_environ or {})
        subprocess.run(cmd, cwd=cwd, env=env, check=True)

    hooks = make_caller(env_project, runner=legacy_runner, environ={SET_VAR: "legacy"})

    data = hooks.get_requires_for_build_wheel({})

    assert data["set_hook"] == "legacy"
    assert captured["extra_environ"][SET_VAR] == "legacy"


def test_legacy_runner_deletion_still_installed_in_hook_process(
    env_project, monkeypatch
):
    # A legacy runner cannot remove the variable at process start, but the
    # in-process script installs the authoritative snapshot from input.json
    # before importing the backend.
    monkeypatch.setenv(SET_VAR, "should-not-leak")

    def legacy_runner(cmd, cwd=None, extra_environ=None):
        env = os.environ.copy()
        env.update(extra_environ or {})
        subprocess.run(cmd, cwd=cwd, env=env, check=True)

    hooks = make_caller(env_project, runner=legacy_runner, environ={SET_VAR: None})

    data = hooks.get_requires_for_build_wheel({})

    assert data["set_import"] == ABSENT
    assert data["set_hook"] == ABSENT


def _load_in_process_module():
    with _in_proc_script_path() as script:
        spec = importlib.util.spec_from_file_location(
            "pph_in_process_environ_test", script
        )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_in_process_prepare_environ_installs_and_freezes(monkeypatch):
    mod = _load_in_process_module()
    monkeypatch.setenv("ENVELOPE_TEST_PARENT_ONLY", "parent")

    snapshot = mod._prepare_environ({"ENVELOPE_TEST_FRESH": "fresh"})

    assert isinstance(snapshot, MappingProxyType)
    assert snapshot["ENVELOPE_TEST_FRESH"] == "fresh"
    assert "ENVELOPE_TEST_PARENT_ONLY" not in snapshot
    assert os.environ["ENVELOPE_TEST_FRESH"] == "fresh"
    assert "ENVELOPE_TEST_PARENT_ONLY" not in os.environ
    with pytest.raises(TypeError):
        snapshot["ENVELOPE_TEST_FRESH"] = "tampered"

    # Without an envelope the current process environment is simply frozen.
    fallback_snapshot = mod._prepare_environ(None)
    assert fallback_snapshot["ENVELOPE_TEST_FRESH"] == "fresh"


def test_in_process_build_backend_uses_passed_snapshot(monkeypatch):
    mod = _load_in_process_module()
    monkeypatch.delenv("_PYPROJECT_HOOKS_BUILD_BACKEND", raising=False)

    # The backend spec is read from the passed mapping, not os.environ.
    backend = mod._build_backend({"_PYPROJECT_HOOKS_BUILD_BACKEND": "os"})
    assert backend.__name__ == "os"

    with pytest.raises(KeyError):
        mod._build_backend({})


def test_in_process_sys_path_cleanup_still_runs(env_project):
    # Sanity check that threading the envelope did not break the normal flow.
    hooks = make_caller(env_project, environ={SET_VAR: "sanity"})
    assert hooks._supported_features() == ["build_editable"]
