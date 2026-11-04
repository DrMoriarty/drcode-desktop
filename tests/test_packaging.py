"""Tests for the packaging helpers (plan task 8).

The build driver (scripts/build_dist.py) runs PyInstaller/dpkg-deb/
appimagetool — not unit testable. What IS testable: the version
contract (GUI version = the drcode submodule version), the pure text
generators behind the AppImage and .deb payloads, and the notarytool
credential plumbing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import scripts.build_dist as build


def test_submodule_version_is_the_version_file() -> None:
    assert build.submodule_version() == build.VERSION_FILE.read_text(
        encoding="utf-8"
    ).strip()


def test_submodule_version_matches_the_core_runtime_version() -> None:
    """The frozen app reports the same version the core's own machinery
    does (drcode.version reads the same `version` file; in the dev tree
    it resolves via the pyproject.toml guard next to it)."""
    from drcode.version import APP_VERSION

    assert build.submodule_version() == APP_VERSION


def test_desktop_entry_text_fields() -> None:
    text = build.desktop_entry_text(
        exec_cmd="/usr/bin/drcode-desktop", icon_name="drcode-desktop"
    )
    assert f"Name={build.APP_NAME}" in text
    assert "Exec=/usr/bin/drcode-desktop" in text
    assert "Icon=drcode-desktop" in text
    assert f"StartupWMClass={build.EXE_NAME}" in text
    assert "Terminal=false" in text


def test_apprun_execs_the_frozen_binary() -> None:
    text = build.apprun_text("DrCodeDesktop")
    assert text.startswith("#!/bin/sh")
    assert 'exec "$HERE/usr/bin/DrCodeDesktop/DrCodeDesktop" "$@"' in text


def test_deb_control_text_fields() -> None:
    text = build.deb_control_text("1.2.3", "amd64", 12345)
    assert "Package: drcode-desktop" in text
    assert "Version: 1.2.3" in text
    assert "Architecture: amd64" in text
    assert "Installed-Size: 12345" in text
    # The Qt runtime the frozen PySide6 needs from the distro.
    for lib in ("libgl1", "libxkbcommon0", "libxcb-cursor0"):
        assert lib in build.DEB_DEPENDS


def test_spec_bundles_the_version_file_and_prompts() -> None:
    """The spec's data contract, checked statically: the frozen version
    file lands at the bundle root (drcode.version reads it from
    sys._MEIPASS) and the core's prompts ship with the package."""
    spec = (Path(build.PACKAGING) / "desktop.spec").read_text(encoding="utf-8")
    assert '(str(SUBMODULE / "version"), ".")' in spec
    assert '"drcode/prompts"' in spec


def test_notarytool_args_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "DRCODE_NOTARY_PROFILE",
        "DRCODE_NOTARY_KEY",
        "DRCODE_NOTARY_KEY_ID",
        "DRCODE_NOTARY_ISSUER",
    ):
        monkeypatch.delenv(var, raising=False)
    assert build.notarytool_args() is None


def test_notarytool_args_from_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRCODE_NOTARY_PROFILE", "drcode-notary")
    assert build.notarytool_args() == ["--keychain-profile", "drcode-notary"]


def test_notarytool_args_from_api_key_trio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DRCODE_NOTARY_PROFILE", raising=False)
    monkeypatch.setenv("DRCODE_NOTARY_KEY", "/keys/AuthKey.p8")
    monkeypatch.setenv("DRCODE_NOTARY_KEY_ID", "ABC123")
    monkeypatch.setenv("DRCODE_NOTARY_ISSUER", "iss-uuid")
    assert build.notarytool_args() == [
        "--key",
        "/keys/AuthKey.p8",
        "--key-id",
        "ABC123",
        "--issuer",
        "iss-uuid",
    ]
