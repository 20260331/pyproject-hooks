from contextlib import contextmanager
import inspect
import json
import os
import subprocess
import sys
import tarfile
import uuid
import zipfile
from os.path import abspath, dirname
from os.path import join as pjoin
from unittest.mock import Mock

import pytest
from testpath import assert_isfile, modified_env
from testpath.tempdir import TemporaryDirectory, TemporaryWorkingDirectory

import pyproject_hooks._impl as impl
from pyproject_hooks import (
    BackendUnavailable,
    BuildBackendWarning,
    BuildBackendHookCaller,
    CorrelationError,
    HookMissing,
    UnsupportedOperation,
    default_subprocess_runner,
)
from pyproject_hooks._in_process import _in_process as inproc
from pyproject_hooks._in_process import _in_proc_script_path as in_proc_script_path
from tests.compat import tomllib

SAMPLES_DIR = pjoin(dirname(abspath(__file__)), "samples")
BUILDSYS_PKGS = pjoin(SAMPLES_DIR, "buildsys_pkgs")


def get_hooks(pkg, **kwargs):
    source_dir = pjoin(SAMPLES_DIR, pkg)
    with open(pjoin(source_dir, "pyproject.toml"), "rb") as f:
        data = tomllib.load(f)
    return BuildBackendHookCaller(
        source_dir, data["build-system"]["build-backend"], **kwargs
    )


def test_missing_backend_gives_exception():
    hooks = get_hooks("pkg1")
    with modified_env({"PYTHONPATH": ""}):
        msg = "Cannot import 'buildsys'"
        with pytest.raises(BackendUnavailable, match=msg) as exc:
            hooks.get_requires_for_build_wheel({})
        assert exc.value.backend_name == "buildsys"


def test_get_requires_for_build_wheel():
    hooks = get_hooks("pkg1")
    with modified_env({"PYTHONPATH": BUILDSYS_PKGS}):
        res = hooks.get_requires_for_build_wheel({})
    assert res == ["wheelwright"]


def test_get_requires_for_build_editable():
    hooks = get_hooks("pkg1")
    with modified_env({"PYTHONPATH": BUILDSYS_PKGS}):
        res = hooks.get_requires_for_build_editable({})
    assert res == ["wheelwright", "editables"]


def test_get_requires_for_build_sdist():
    hooks = get_hooks("pkg1")
    with modified_env({"PYTHONPATH": BUILDSYS_PKGS}):
        res = hooks.get_requires_for_build_sdist({})
    assert res == ["frog"]


def test_prepare_metadata_for_build_wheel():
    hooks = get_hooks("pkg1")
    with TemporaryDirectory() as metadatadir:
        with modified_env({"PYTHONPATH": BUILDSYS_PKGS}):
            hooks.prepare_metadata_for_build_wheel(metadatadir, {})

        assert_isfile(pjoin(metadatadir, "pkg1-0.5.dist-info", "METADATA"))


def test_prepare_metadata_for_build_editable():
    hooks = get_hooks("pkg1")
    with TemporaryDirectory() as metadatadir:
        with modified_env({"PYTHONPATH": BUILDSYS_PKGS}):
            hooks.prepare_metadata_for_build_editable(metadatadir, {})

        assert_isfile(pjoin(metadatadir, "pkg1-0.5.dist-info", "METADATA"))


def test_build_wheel():
    hooks = get_hooks("pkg1")
    with TemporaryDirectory() as builddir:
        with modified_env({"PYTHONPATH": BUILDSYS_PKGS}):
            whl_file = hooks.build_wheel(builddir, {})

        assert whl_file.endswith(".whl")
        assert os.sep not in whl_file

        whl_file = pjoin(builddir, whl_file)
        assert_isfile(whl_file)
        assert zipfile.is_zipfile(whl_file)


def test_build_editable():
    hooks = get_hooks("pkg1")
    with TemporaryDirectory() as builddir:
        with modified_env({"PYTHONPATH": BUILDSYS_PKGS}):
            whl_file = hooks.build_editable(builddir, {})

        assert whl_file.endswith(".whl")
        assert os.sep not in whl_file

        whl_file = pjoin(builddir, whl_file)
        assert_isfile(whl_file)
        assert zipfile.is_zipfile(whl_file)


def test_build_wheel_relpath():
    hooks = get_hooks("pkg1")
    with TemporaryWorkingDirectory() as builddir:
        with modified_env({"PYTHONPATH": BUILDSYS_PKGS}):
            whl_file = hooks.build_wheel(".", {})

        assert whl_file.endswith(".whl")
        assert os.sep not in whl_file

        whl_file = pjoin(builddir, whl_file)
        assert_isfile(whl_file)
        assert zipfile.is_zipfile(whl_file)


def test_build_sdist():
    hooks = get_hooks("pkg1")
    with TemporaryDirectory() as sdistdir:
        with modified_env({"PYTHONPATH": BUILDSYS_PKGS}):
            sdist = hooks.build_sdist(sdistdir, {})

        assert sdist.endswith(".tar.gz")
        assert os.sep not in sdist

        sdist = pjoin(sdistdir, sdist)
        assert_isfile(sdist)
        assert tarfile.is_tarfile(sdist)

        with tarfile.open(sdist) as tf:
            contents = tf.getnames()
        assert "pkg1-0.5/pyproject.toml" in contents


def test_build_sdist_unsupported():
    hooks = get_hooks("pkg1")
    with TemporaryDirectory() as sdistdir:
        with modified_env({"PYTHONPATH": BUILDSYS_PKGS}):
            with pytest.raises(UnsupportedOperation):
                hooks.build_sdist(sdistdir, {"test_unsupported": True})


def test_runner_replaced_on_exception(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", BUILDSYS_PKGS)

    runner = Mock(wraps=default_subprocess_runner)
    hooks = get_hooks("pkg1", runner=runner)

    hooks.get_requires_for_build_wheel()
    runner.assert_called_once()
    runner.reset_mock()

    runner2 = Mock(wraps=default_subprocess_runner)
    try:
        with hooks.subprocess_runner(runner2):
            hooks.get_requires_for_build_wheel()
            runner2.assert_called_once()
            runner2.reset_mock()
            raise RuntimeError()
    except RuntimeError:
        pass

    hooks.get_requires_for_build_wheel()
    runner.assert_called_once()


def test_custom_python_executable(monkeypatch, tmpdir):
    monkeypatch.setenv("PYTHONPATH", BUILDSYS_PKGS)

    runner = Mock(autospec=default_subprocess_runner)
    hooks = get_hooks("pkg1", runner=runner, python_executable="some-python")

    with hooks.subprocess_runner(runner):
        with pytest.raises(FileNotFoundError):
            # output.json is missing because we didn't actually run the hook
            hooks.get_requires_for_build_wheel()
        runner.assert_called_once()
        assert runner.call_args[0][0][0] == "some-python"


def test_path_pollution():
    hooks = get_hooks("path-pollution")
    with TemporaryDirectory() as outdir:
        with modified_env(
            {
                "PYTHONPATH": BUILDSYS_PKGS,
                "TEST_POLLUTION_OUTDIR": outdir,
            }
        ):
            hooks.get_requires_for_build_wheel({})
        with open(pjoin(outdir, "out.json")) as f:
            captured_sys_path = json.load(f)

    with in_proc_script_path() as path:
        assert os.path.dirname(path) not in captured_sys_path
    assert captured_sys_path[0] == BUILDSYS_PKGS


def test_path_pollution_symlinked_in_process(monkeypatch):
    hooks = get_hooks("path-pollution")
    with TemporaryDirectory() as link_root:
        with in_proc_script_path() as path:
            link_parent = pjoin(link_root, "alias")
            os.symlink(os.path.dirname(path), link_parent)

        @contextmanager
        def symlinked_in_proc_script_path():
            yield pjoin(link_parent, os.path.basename(path))

        monkeypatch.setattr(impl, "_in_proc_script_path", symlinked_in_proc_script_path)

        with TemporaryDirectory() as outdir:
            with modified_env(
                {
                    "PYTHONPATH": BUILDSYS_PKGS,
                    "TEST_POLLUTION_OUTDIR": outdir,
                }
            ):
                hooks.get_requires_for_build_wheel({})
            with open(pjoin(outdir, "out.json")) as f:
                captured_sys_path = json.load(f)

    assert link_parent not in captured_sys_path
    assert os.path.realpath(link_parent) not in captured_sys_path
    assert captured_sys_path[0] == BUILDSYS_PKGS


def test_setup_py():
    hooks = get_hooks("setup-py")
    with modified_env({"PYTHONPATH": BUILDSYS_PKGS}):
        res = set(hooks.get_requires_for_build_wheel({}))
    # Depending on the version of setuptools, it may be both, just wheel, or neither
    assert res.issubset({"setuptools", "wheel"})


@pytest.mark.parametrize(
    ("pkg", "expected"),
    [
        ("pkg1", ["build_editable"]),
        ("pkg2", []),
        ("pkg3", ["build_editable"]),
    ],
)
def test__supported_features(pkg, expected):
    hooks = get_hooks(pkg)
    with modified_env({"PYTHONPATH": BUILDSYS_PKGS}):
        res = hooks._supported_features()
    assert res == expected


def test_warnings():
    hooks = get_hooks("pkg-with-warnings")
    with modified_env({"PYTHONPATH": BUILDSYS_PKGS}):
        with pytest.warns(BuildBackendWarning, match="this is my example warning"):
            hooks.get_requires_for_build_wheel({})


# ---------------------------------------------------------------------------
# Request/response correlation checks (verify_correlation)
# ---------------------------------------------------------------------------


def _raw_output_runner(raw_text):
    """A runner which never executes the subprocess, writing output itself."""

    def runner(cmd, cwd=None, extra_environ=None):
        with open(pjoin(cmd[3], "output.json"), "w", encoding="utf-8") as f:
            f.write(raw_text)

    return runner


def _forged_output_runner(forged):
    """A runner forging a correlated response that echoes the request id."""

    def runner(cmd, cwd=None, extra_environ=None):
        td = cmd[3]
        with open(pjoin(td, "input.json"), encoding="utf-8") as f:
            request = json.load(f)
        payload = dict(forged)
        payload["request_id"] = request["request_id"]
        with open(pjoin(td, "output.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f)

    return runner


def _runner_then_mutate_output(mutate):
    """A runner which executes normally, then rewrites output.json raw text."""

    def runner(cmd, cwd=None, extra_environ=None):
        default_subprocess_runner(cmd, cwd=cwd, extra_environ=extra_environ)
        out_path = pjoin(cmd[3], "output.json")
        with open(out_path, encoding="utf-8") as f:
            text = f.read()
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(mutate(text))

    return runner


def _replaying_runner(saved):
    """Run honestly once; afterwards replay the first response verbatim."""

    def runner(cmd, cwd=None, extra_environ=None):
        out_path = pjoin(cmd[3], "output.json")
        if "bytes" not in saved:
            default_subprocess_runner(cmd, cwd=cwd, extra_environ=extra_environ)
            with open(out_path, "rb") as f:
                saved["bytes"] = f.read()
        else:
            with open(out_path, "wb") as f:
                f.write(saved["bytes"])

    return runner


def _capturing_runner(captured, slot):
    def runner(cmd, cwd=None, extra_environ=None):
        in_path = pjoin(cmd[3], "input.json")
        with open(in_path, encoding="utf-8") as f:
            captured[slot] = json.load(f)
        default_subprocess_runner(cmd, cwd=cwd, extra_environ=extra_environ)

    return runner


def test_correlation_is_off_by_default():
    hooks = get_hooks("pkg1")
    assert hooks.verify_correlation is False


def test_correlation_envelope_only_when_enabled(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", BUILDSYS_PKGS)
    captured = {}

    hooks = get_hooks("pkg1", runner=_capturing_runner(captured, "legacy"))
    hooks.get_requires_for_build_wheel()
    assert set(captured["legacy"]) == {"kwargs"}

    hooks = get_hooks(
        "pkg1", verify_correlation=True, runner=_capturing_runner(captured, "v1")
    )
    hooks.get_requires_for_build_wheel()
    request = captured["v1"]
    assert set(request) == {"kwargs", "protocol", "request_id"}
    assert request["protocol"] == impl.CORRELATION_PROTOCOL_VERSION
    # The call identifier must be a fresh, valid UUID for every call.
    uuid.UUID(request["request_id"])
    hooks.get_requires_for_build_wheel()
    second_request = captured["v1"]
    assert second_request["request_id"] != request["request_id"]


def test_correlated_hook_call_returns_its_result(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", BUILDSYS_PKGS)
    hooks = get_hooks("pkg1", verify_correlation=True)
    assert hooks.get_requires_for_build_wheel({}) == ["wheelwright"]
    assert hooks.get_requires_for_build_sdist({}) == ["frog"]


def test_correlated_call_delivers_warnings(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", BUILDSYS_PKGS)
    hooks = get_hooks("pkg-with-warnings", verify_correlation=True)
    with pytest.warns(BuildBackendWarning, match="this is my example warning"):
        hooks.get_requires_for_build_wheel({})


def test_correlated_call_preserves_original_exceptions(monkeypatch, tmpdir):
    hooks = get_hooks("pkg1", verify_correlation=True)
    with modified_env({"PYTHONPATH": ""}):
        with pytest.raises(BackendUnavailable, match="Cannot import"):
            hooks.get_requires_for_build_wheel({})

    with modified_env({"PYTHONPATH": BUILDSYS_PKGS}):
        with pytest.raises(UnsupportedOperation):
            hooks.build_sdist(str(tmpdir), {"test_unsupported": True})

        # buildsys_minimal defines no build_editable hook.
        hooks = get_hooks("pkg2", verify_correlation=True)
        with pytest.raises(HookMissing, match="build_editable"):
            hooks.build_editable(str(tmpdir), {})


def test_correlated_replayed_response_is_rejected(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", BUILDSYS_PKGS)
    hooks = get_hooks("pkg1", verify_correlation=True, runner=_replaying_runner({}))
    # First call is honest and succeeds.
    assert hooks.get_requires_for_build_wheel() == ["wheelwright"]
    # Second call gets the first response replayed into a new control dir.
    with pytest.raises(CorrelationError, match="answers call"):
        hooks.get_requires_for_build_wheel()
    # The failed call cleaned up after itself; a later call works again.
    with hooks.subprocess_runner(default_subprocess_runner):
        assert hooks.get_requires_for_build_wheel() == ["wheelwright"]


def test_correlated_legacy_format_response_is_rejected(monkeypatch):
    # Simulates an old hook process (or a runner running some other command):
    # output.json carries no correlation fields at all.
    hooks = get_hooks(
        "pkg1",
        verify_correlation=True,
        runner=_raw_output_runner(json.dumps({"return_val": ["stale"]})),
    )
    with pytest.raises(CorrelationError, match="missing field") as exc_info:
        hooks.get_requires_for_build_wheel()
    assert "request_id" in str(exc_info.value)


def test_correlated_missing_output_is_rejected(monkeypatch):
    # A runner which returns without running anything leaves no output.json.
    hooks = get_hooks(
        "pkg1", verify_correlation=True, runner=Mock(autospec=default_subprocess_runner)
    )
    with pytest.raises(CorrelationError, match="did not write output.json"):
        hooks.get_requires_for_build_wheel()
    # And the caller remains usable.
    monkeypatch.setenv("PYTHONPATH", BUILDSYS_PKGS)
    with hooks.subprocess_runner(default_subprocess_runner):
        assert hooks.get_requires_for_build_wheel() == ["wheelwright"]


def test_mismatched_response_delivers_neither_result_nor_warnings(recwarn):
    # Forged response which honestly echoes this call's id but certifies
    # different request content, with an attacker controlled return value and
    # warning. Nothing of it must surface.
    forged = {
        "unsupported": False,
        "return_val": ["totally-wrong"],
        "warnings": [
            {"message": "forged warning", "filename": "forged.py", "lineno": 1}
        ],
        "protocol": impl.CORRELATION_PROTOCOL_VERSION,
        "request_digest": "0" * 64,
    }
    hooks = get_hooks(
        "pkg1", verify_correlation=True, runner=_forged_output_runner(forged)
    )
    with pytest.raises(CorrelationError, match="request content"):
        hooks.get_requires_for_build_wheel()
    assert recwarn.list == []


@pytest.mark.parametrize(
    ("name", "mutate", "reason"),
    [
        (
            "wrong_protocol",
            lambda d: d.update(protocol=impl.CORRELATION_PROTOCOL_VERSION + 1),
            "protocol version mismatch",
        ),
        (
            "foreign_call_id",
            lambda d: d.update(request_id=str(uuid.uuid4())),
            "answers call",
        ),
        (
            "wrong_digest",
            lambda d: d.update(request_digest="0" * 64),
            "request content",
        ),
        ("missing_field", lambda d: d.pop("request_id"), "missing field"),
        ("unknown_field", lambda d: d.update(surprise=True), "unexpected field"),
        (
            "warnings_not_a_list",
            lambda d: d.update(warnings="nope"),
            "'warnings' field must be a list",
        ),
    ],
)
def test_tampered_output_is_rejected(name, mutate, reason, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", BUILDSYS_PKGS)

    def mutate_text(text):
        data = json.loads(text)
        mutate(data)
        return json.dumps(data)

    hooks = get_hooks(
        "pkg1",
        verify_correlation=True,
        runner=_runner_then_mutate_output(mutate_text),
    )
    with pytest.raises(CorrelationError, match=reason):
        hooks.get_requires_for_build_wheel()


def test_output_with_duplicate_json_keys_is_rejected(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", BUILDSYS_PKGS)

    def duplicate_key(text):
        return text.replace(
            '"request_id":',
            '"request_id": "' + str(uuid.uuid4()) + '", "request_id":',
            1,
        )

    hooks = get_hooks(
        "pkg1",
        verify_correlation=True,
        runner=_runner_then_mutate_output(duplicate_key),
    )
    with pytest.raises(CorrelationError, match="duplicate JSON key"):
        hooks.get_requires_for_build_wheel()


def _runner_rewriting_input(rewrite):
    """Run honestly, but rewrite input.json (in place or via a return value)."""

    def runner(cmd, cwd=None, extra_environ=None):
        in_path = pjoin(cmd[3], "input.json")
        with open(in_path, encoding="utf-8") as f:
            data = json.load(f)
        result = rewrite(data)
        if result is not None:
            data = result
        with open(in_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        default_subprocess_runner(cmd, cwd=cwd, extra_environ=extra_environ)

    return runner


def test_dict_key_reordering_in_request_is_equivalent(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", BUILDSYS_PKGS)
    config_settings = {"z": 1, "a": [1, 2], "nested": {"y": 2, "b": 3}}

    def reorder(data):
        # Reverse the insertion order of every dict (including nested ones),
        # but keep lists in their original order, which is significant.
        def reversed_dict(obj):
            if isinstance(obj, dict):
                return {k: reversed_dict(v) for k, v in reversed(list(obj.items()))}
            if isinstance(obj, list):
                return [reversed_dict(v) for v in obj]
            return obj

        return reversed_dict(data)

    hooks = get_hooks(
        "pkg1",
        verify_correlation=True,
        runner=_runner_rewriting_input(reorder),
    )
    assert hooks.get_requires_for_build_wheel(config_settings) == ["wheelwright"]


@pytest.mark.parametrize(
    ("name", "tamper"),
    [
        # List order is significant.
        ("list_order", lambda d: d["kwargs"]["config_settings"].update(v=[2, 1])),
        # An explicit null is not the same as a missing key.
        ("null_vs_missing", lambda d: d["kwargs"]["config_settings"].pop("keep")),
    ],
)
def test_changed_request_content_is_rejected(name, tamper, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", BUILDSYS_PKGS)
    config_settings = {"v": [1, 2], "keep": None}
    hooks = get_hooks(
        "pkg1", verify_correlation=True, runner=_runner_rewriting_input(tamper)
    )
    with pytest.raises(CorrelationError, match="request content"):
        hooks.get_requires_for_build_wheel(config_settings)


def test_canonical_digest_semantics():
    # Key reordering at any level produces the same digest ...
    ordered = {"hook": "h", "kwargs": {"a": 1, "b": [1, 2, None]}}
    reordered = {"kwargs": {"b": [1, 2, None], "a": 1}, "hook": "h"}
    assert impl.canonical_json_digest(ordered) == impl.canonical_json_digest(reordered)

    # ... while list order and explicit nulls must not be conflated.
    assert impl.canonical_json_digest([1, 2]) != impl.canonical_json_digest([2, 1])
    assert impl.canonical_json_digest({"x": None}) != impl.canonical_json_digest({})
    assert impl.canonical_json_digest([None]) != impl.canonical_json_digest([])
    # Booleans are not integers in the canonical encoding.
    assert impl.canonical_json_digest(True) != impl.canonical_json_digest(1)


def test_child_and_frontend_digest_implementations_agree():
    payload = {"hook": "build_wheel", "kwargs": {"a": [1, 2], "b": None}}
    assert inproc.canonical_json_digest(payload) == impl.canonical_json_digest(payload)


def _run_child_straight(control_dir, input_text):
    with open(pjoin(control_dir, "input.json"), "w", encoding="utf-8") as f:
        f.write(input_text)
    env = os.environ.copy()
    env["PYTHONPATH"] = BUILDSYS_PKGS
    env["_PYPROJECT_HOOKS_BUILD_BACKEND"] = "buildsys"
    with in_proc_script_path() as script:
        return subprocess.run(
            [sys.executable, str(script), "get_requires_for_build_wheel", control_dir],
            cwd=control_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )


def _correlated_input_text(mutate=None):
    request = {
        "kwargs": {"config_settings": {}},
        "protocol": impl.CORRELATION_PROTOCOL_VERSION,
        "request_id": str(uuid.uuid4()),
    }
    if mutate is not None:
        mutate(request)
    return json.dumps(request)


def test_child_rejects_bad_correlated_input_before_importing_backend(tmpdir):
    control_dir = str(tmpdir)
    bad_inputs = [
        (_correlated_input_text(lambda d: d.pop("request_id")), "missing field"),
        (_correlated_input_text(lambda d: d.update(extra=1)), "unexpected field"),
        (_correlated_input_text(lambda d: d.update(protocol=2)), "protocol version"),
        (
            _correlated_input_text(lambda d: d.update(request_id="not-a-uuid")),
            "valid UUID",
        ),
        (
            _correlated_input_text(lambda d: d.update(kwargs=[])),
            "kwargs field must be a JSON object",
        ),
        (
            _correlated_input_text(lambda d: d.update(protocol="1")),
            "protocol version must be an integer",
        ),
    ]
    for input_text, reason in bad_inputs:
        result = _run_child_straight(control_dir, input_text)
        assert result.returncode != 0
        assert reason in result.stderr
        # The hook was never called: no output.json and no backend traceback.
        assert not os.path.exists(pjoin(control_dir, "output.json"))
        assert "Traceback" not in result.stderr


def test_child_rejects_duplicate_input_keys_before_importing_backend(tmpdir):
    control_dir = str(tmpdir)
    raw = (
        '{"kwargs": {}, "kwargs": {}, "protocol": '
        f"{impl.CORRELATION_PROTOCOL_VERSION}"
        ', "request_id": "' + str(uuid.uuid4()) + '"}'
    )
    result = _run_child_straight(control_dir, raw)
    assert result.returncode != 0
    assert "duplicate JSON key" in result.stderr
    assert not os.path.exists(pjoin(control_dir, "output.json"))


def test_child_accepts_valid_correlated_input(tmpdir):
    control_dir = str(tmpdir)
    result = _run_child_straight(control_dir, _correlated_input_text())
    assert result.returncode == 0, result.stderr
    with open(pjoin(control_dir, "output.json"), encoding="utf-8") as f:
        output = json.load(f)
    assert output["protocol"] == impl.CORRELATION_PROTOCOL_VERSION
    assert output["return_val"] == ["wheelwright"]
    uuid.UUID(output["request_id"])
    assert output["request_digest"] == inproc._request_digest(
        "get_requires_for_build_wheel", {"config_settings": {}}
    )


def test_subprocess_runner_signature_is_unchanged():
    parameters = inspect.signature(default_subprocess_runner).parameters
    assert list(parameters) == ["cmd", "cwd", "extra_environ"]
    assert parameters["cwd"].default is None
    assert parameters["extra_environ"].default is None
    # The quiet runner keeps the same contract.
    from pyproject_hooks import quiet_subprocess_runner

    assert list(inspect.signature(quiet_subprocess_runner).parameters) == list(
        parameters
    )


def test_uncorrelated_caller_still_trusts_runner_output(monkeypatch):
    # Documents the default behaviour: without the opt-in checks an old-style
    # response delivered by a custom runner is accepted exactly as before.
    hooks = get_hooks(
        "pkg1", runner=_raw_output_runner(json.dumps({"return_val": ["forged"]}))
    )
    assert hooks.get_requires_for_build_wheel() == ["forged"]
