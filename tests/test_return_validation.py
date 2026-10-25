"""Tests for the optional validation of build backend hook return values."""
from os.path import abspath, dirname, isdir, isfile
from os.path import join as pjoin

import pytest
from testpath import modified_env
from testpath.tempdir import TemporaryDirectory

import pyproject_hooks._impl as impl
from pyproject_hooks import (
    BackendUnavailable,
    BuildBackendHookCaller,
    HookMissing,
    InvalidHookReturn,
    UnsupportedOperation,
)
from tests.compat import tomllib

SAMPLES_DIR = pjoin(dirname(abspath(__file__)), "samples")
BUILDSYS_PKGS = pjoin(SAMPLES_DIR, "buildsys_pkgs")
# Any real source tree works; the misbehaving backend is imported from
# PYTHONPATH and only writes into the directories handed to the hooks.
INVALID_SOURCE_DIR = pjoin(SAMPLES_DIR, "pkg1")

REQUIRES_HOOKS = [
    "get_requires_for_build_wheel",
    "get_requires_for_build_editable",
    "get_requires_for_build_sdist",
]

ARTIFACT_HOOKS = [
    "build_wheel",
    "build_editable",
    "build_sdist",
    "prepare_metadata_for_build_wheel",
    "prepare_metadata_for_build_editable",
]

# Every kind of contract violation the sample backend can produce, apart
# from "bad_member" which only applies to hooks returning lists.
INVALID_MODES = [
    "wrong_type",
    "empty",
    "wrong_suffix",
    "not_basename",
    "missing",
]


def get_invalid_hooks(validate_results=False):
    return BuildBackendHookCaller(
        INVALID_SOURCE_DIR,
        "buildsys_invalid",
        validate_results=validate_results,
    )


def get_sample_hooks(pkg, validate_results=False):
    source_dir = pjoin(SAMPLES_DIR, pkg)
    with open(pjoin(source_dir, "pyproject.toml"), "rb") as f:
        data = tomllib.load(f)
    return BuildBackendHookCaller(
        source_dir,
        data["build-system"]["build-backend"],
        validate_results=validate_results,
    )


def test_invalid_hook_return_is_value_error():
    assert issubclass(InvalidHookReturn, ValueError)


@pytest.mark.parametrize("hook_name", REQUIRES_HOOKS)
def test_requires_hook_valid_result(hook_name):
    hooks = get_invalid_hooks(validate_results=True)
    with modified_env({"PYTHONPATH": BUILDSYS_PKGS}):
        result = getattr(hooks, hook_name)(None)
    assert isinstance(result, list)
    assert all(isinstance(item, str) for item in result)


@pytest.mark.parametrize("hook_name", REQUIRES_HOOKS)
@pytest.mark.parametrize("mode", ["wrong_type", "bad_member"])
def test_requires_hook_invalid_result(hook_name, mode):
    hooks = get_invalid_hooks(validate_results=True)
    with modified_env({"PYTHONPATH": BUILDSYS_PKGS}):
        with pytest.raises(InvalidHookReturn) as exc_info:
            getattr(hooks, hook_name)({"invalid": mode})
    assert exc_info.value.hook_name == hook_name


@pytest.mark.parametrize("hook_name", ARTIFACT_HOOKS)
def test_artifact_hook_valid_result(hook_name):
    hooks = get_invalid_hooks(validate_results=True)
    with TemporaryDirectory() as outdir:
        with modified_env({"PYTHONPATH": BUILDSYS_PKGS}):
            name = getattr(hooks, hook_name)(outdir, None)
        path = pjoin(outdir, name)
        if hook_name.startswith("prepare"):
            assert isdir(path)
        else:
            assert isfile(path)


@pytest.mark.parametrize("hook_name", ARTIFACT_HOOKS)
@pytest.mark.parametrize("mode", INVALID_MODES)
def test_artifact_hook_invalid_result(hook_name, mode):
    hooks = get_invalid_hooks(validate_results=True)
    with TemporaryDirectory() as outdir:
        with modified_env({"PYTHONPATH": BUILDSYS_PKGS}):
            with pytest.raises(InvalidHookReturn) as exc_info:
                getattr(hooks, hook_name)(outdir, {"invalid": mode})
    assert exc_info.value.hook_name == hook_name


def test_validation_disabled_by_default_passes_values_through():
    # Without opting in, a backend returning a non-existent artifact or a
    # value of the wrong type keeps its historical, unchecked behaviour.
    hooks = get_invalid_hooks()
    with TemporaryDirectory() as outdir:
        with modified_env({"PYTHONPATH": BUILDSYS_PKGS}):
            assert (
                hooks.build_wheel(outdir, {"invalid": "missing"})
                == "ghost-0.1-py3-none-any.whl"
            )
            assert (
                hooks.prepare_metadata_for_build_wheel(outdir, {"invalid": "missing"})
                == "ghost-0.1.dist-info"
            )
            assert (
                hooks.get_requires_for_build_wheel({"invalid": "wrong_type"})
                == "not-a-list"
            )


def test_fallback_result_is_validated_against_metadata_directory():
    # pkg2 has no prepare_metadata_for_build_wheel hook, so the wheel-based
    # fallback runs inside the subprocess; its extracted dist-info name must
    # still pass validation in the frontend.
    hooks = get_sample_hooks("pkg2", validate_results=True)
    with TemporaryDirectory() as metadatadir:
        with modified_env({"PYTHONPATH": BUILDSYS_PKGS}):
            name = hooks.prepare_metadata_for_build_wheel(metadatadir, {})
        assert name == "pkg2-0.5.dist-info"
        assert isdir(pjoin(metadatadir, name))


def test_backend_unavailable_semantics_preserved():
    hooks = get_sample_hooks("pkg1", validate_results=True)
    with modified_env({"PYTHONPATH": ""}):
        with pytest.raises(BackendUnavailable):
            hooks.get_requires_for_build_wheel({})


def test_hook_missing_semantics_preserved():
    hooks = get_sample_hooks("pkg2", validate_results=True)
    with TemporaryDirectory() as metadatadir:
        with modified_env({"PYTHONPATH": BUILDSYS_PKGS}):
            with pytest.raises(HookMissing) as exc_info:
                hooks.prepare_metadata_for_build_wheel(
                    metadatadir, {}, _allow_fallback=False
                )
    assert exc_info.value.hook_name == "prepare_metadata_for_build_wheel"


def test_unsupported_operation_semantics_preserved():
    hooks = get_sample_hooks("pkg1", validate_results=True)
    with TemporaryDirectory() as sdistdir:
        with modified_env({"PYTHONPATH": BUILDSYS_PKGS}):
            with pytest.raises(UnsupportedOperation):
                hooks.build_sdist(sdistdir, {"test_unsupported": True})


def test_supported_features_validated():
    hooks = get_invalid_hooks(validate_results=True)
    with modified_env({"PYTHONPATH": BUILDSYS_PKGS}):
        # The sample backend aliases build_editable, so the feature is
        # reported as supported; the returned list must pass validation.
        assert hooks._supported_features() == ["build_editable"]


def test_wheel_artifact_must_be_a_file(tmp_path):
    (tmp_path / "evil-1.0-py3-none-any.whl").mkdir()
    with pytest.raises(InvalidHookReturn, match="file does not exist"):
        impl._validate_hook_result(
            "build_wheel",
            "evil-1.0-py3-none-any.whl",
            {"wheel_directory": str(tmp_path)},
        )


def test_metadata_artifact_must_be_a_directory(tmp_path):
    (tmp_path / "evil-1.0.dist-info").write_text("")
    with pytest.raises(InvalidHookReturn, match="directory does not exist"):
        impl._validate_hook_result(
            "prepare_metadata_for_build_wheel",
            "evil-1.0.dist-info",
            {"metadata_directory": str(tmp_path)},
        )


def test_artifact_resolving_outside_directory_is_rejected(tmp_path):
    outdir = tmp_path / "out"
    outdir.mkdir()
    (tmp_path / "evil-1.0-py3-none-any.whl").write_text("")
    (outdir / "evil-1.0-py3-none-any.whl").symlink_to(
        tmp_path / "evil-1.0-py3-none-any.whl"
    )
    with pytest.raises(InvalidHookReturn, match="resolves outside"):
        impl._validate_hook_result(
            "build_wheel",
            "evil-1.0-py3-none-any.whl",
            {"wheel_directory": str(outdir)},
        )


def test_conforming_artifact_passes_direct_validation(tmp_path):
    (tmp_path / "good-1.0.tar.gz").write_text("")
    impl._validate_hook_result(
        "build_sdist",
        "good-1.0.tar.gz",
        {"sdist_directory": str(tmp_path)},
    )


def test_hook_without_contract_is_not_validated():
    # An unknown/future hook name must never be rejected by validation.
    impl._validate_hook_result("some_future_hook", object(), {})
