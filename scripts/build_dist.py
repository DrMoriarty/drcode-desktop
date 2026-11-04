"""Build driver for DrCode Desktop distributables (plan task 8).

Runs PyInstaller (``packaging/desktop.spec``) around the GUI shell +
the in-process drcode submodule and produces per-platform
distributables:

  macos      dist/DrCode-Desktop-<version>-<arch>.zip — the .app
             (Info.plist, icon) inside a ditto zip. Signing: Developer
             ID via DRCODE_CODESIGN_IDENTITY, ad-hoc otherwise (then
             right-click → Open on first launch). Notarization runs
             automatically when notarytool credentials are configured
             (DRCODE_NOTARY_PROFILE or the DRCODE_NOTARY_KEY* trio).
  appimage   dist/DrCode-Desktop-<version>-linux-<arch>.AppImage — the
             PyInstaller onedir as the AppDir payload (PySide6 bundles
             its Qt; only the glibc-family system libs stay external).
  deb        dist/DrCode-Desktop-<version>-linux-<arch>.deb — /opt
             install + /usr/bin/drcode-desktop symlink; Depends on the
             distro Qt runtime packages instead of bundling them.

The version is the drcode submodule's ``version`` file (single source of
truth — the GUI version IS the core version, enforced by CI on ``v*``
tags).

Usage (from the repo root)::

    uv sync --group build
    uv run python scripts/build_dist.py --target macos     [--smoke]
    uv run python scripts/build_dist.py --target appimage
    uv run python scripts/build_dist.py --target deb
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
SUBMODULE = ROOT / "drcode"
VERSION_FILE = SUBMODULE / "version"

APP_NAME = "DrCode Desktop"
BUNDLE_ID = "com.drmoriarty.drcode-desktop"
# The frozen binary name (no spaces — it ends up in shell scripts,
# AppImage paths and the deb symlink).
EXE_NAME = "DrCodeDesktop"

DEB_PACKAGE = "drcode-desktop"
# Qt xcb/egl runtime the frozen PySide6 needs from the distro on a
# clean Debian/Ubuntu machine (the Qt libraries themselves ship in the
# PyInstaller output).
DEB_DEPENDS = ", ".join(
    [
        "libgl1",
        "libegl1",
        "libglib2.0-0",
        "libfontconfig1",
        "libdbus-1-3",
        "libxkbcommon0",
        "libxkbcommon-x11-0",
        "libxcb-cursor0",
        "libxcb-icccm4",
        "libxcb-image0",
        "libxcb-keysyms1",
        "libxcb-render-util0",
        "libxcb-shape0",
        "libxcb-xinerama0",
        "libxcb-xkb1",
    ]
)


def submodule_version() -> str:
    """The GUI version = the drcode submodule version (``version`` file)."""
    return VERSION_FILE.read_text(encoding="utf-8").strip()


def run(cmd: list[str | Path], **kwargs: object) -> subprocess.CompletedProcess:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run([str(c) for c in cmd], check=True, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# icon (generated, pure build-time artifact)
# ---------------------------------------------------------------------------


def generate_icon(path: Path, size: int = 512) -> Path:
    """Render the placeholder app icon with Pillow (prompt glyph on tile)."""
    try:
        # Pillow is a build-group dependency, not required in the dev venv;
        # the guarded import degrades to a friendly error below.
        from PIL import Image, ImageDraw  # pyright: ignore[reportMissingImports]
    except ImportError as exc:
        raise SystemExit(
            "Pillow is required for icon generation (uv sync --group build)"
        ) from exc

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    radius = size // 5
    accent = (79, 156, 249, 255)
    # rounded dark tile with an accent outline
    draw.rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=radius, fill=(22, 24, 29, 255)
    )
    draw.rounded_rectangle(
        [0, 0, size - 1, size - 1],
        radius=radius,
        outline=accent,
        width=size // 40,
    )
    # ">_" glyph drawn as thick lines (no font dependency)
    w = size // 14  # stroke width
    pad = size // 4
    x0, y0 = pad, size // 3
    x1, y1 = pad + size // 5, size // 2
    draw.line([(x0, y0), (x1, y1)], fill=accent, width=w)
    draw.line([(x1, y1), (x0, y0 + size // 3)], fill=accent, width=w)
    ux0, ux1 = pad + size // 3, size - pad
    uy = size - pad - size // 10
    draw.line([(ux0, uy), (ux1, uy)], fill=accent, width=w)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path


def ensure_icns(png_path: Path, icns_path: Path) -> Path:
    """macOS icns via iconutil (ships with macOS)."""
    if icns_path.exists():
        return icns_path
    iconset = icns_path.parent / f"{EXE_NAME}.iconset"
    iconset.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image  # pyright: ignore[reportMissingImports]

        img = Image.open(png_path)
        for size in (16, 32, 128, 256, 512):
            resized = img.resize((size, size), Image.Resampling.LANCZOS)
            resized.save(iconset / f"icon_{size}x{size}.png")
            if size <= 256:
                img.resize((size * 2, size * 2), Image.Resampling.LANCZOS).save(
                    iconset / f"icon_{size}x{size}@2x.png"
                )
        run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns_path)])
    finally:
        shutil.rmtree(iconset, ignore_errors=True)
    return icns_path


# ---------------------------------------------------------------------------
# shell build (PyInstaller, packaging/desktop.spec)
# ---------------------------------------------------------------------------


def shell_output(dist_dir: Path) -> Path:
    """Where PyInstaller puts the frozen shell for this platform."""
    name = f"{APP_NAME}.app" if sys.platform == "darwin" else EXE_NAME
    return dist_dir / name


def build_shell(dist_dir: Path) -> Path:
    """Run PyInstaller for the shell; returns the onedir/.app output.

    The frozen version comes from the submodule's ``version`` file via
    the spec itself, so no env plumbing is needed.
    """
    version = submodule_version()
    if not (SUBMODULE / "src" / "drcode" / "__init__.py").is_file():
        raise SystemExit(
            "the drcode submodule is not checked out — run "
            "`git submodule update --init` first"
        )

    build_dir = ROOT / "build"
    png = build_dir / "icon.png"
    generate_icon(png)
    env = dict(os.environ)
    if sys.platform == "darwin":
        icns = build_dir / "icon.icns"
        ensure_icns(png, icns)
        env["DRCODE_DESKTOP_ICNS"] = str(icns)

    run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            dist_dir,
            "--workpath",
            build_dir / "pyinstaller",
            str(PACKAGING / "desktop.spec"),
        ],
        cwd=ROOT,
        env=env,
    )
    out = shell_output(dist_dir)
    if not out.exists():
        raise SystemExit(f"expected build output missing: {out}")
    print(f"shell built: {out} (version {version})")
    return out


def frozen_binary(shell_dir: Path) -> Path:
    """The executable inside the onedir/.app output."""
    if sys.platform == "darwin":
        return shell_dir / "Contents" / "MacOS" / EXE_NAME
    return shell_dir / EXE_NAME


def smoke_run(shell_dir: Path, timeout: int = 60) -> None:
    """Boot the frozen shell with the smoke-exit hook (acceptance: the
    artifact starts on a machine without the dev environment)."""
    binary = frozen_binary(shell_dir)
    env = dict(os.environ, DRCODE_DESKTOP_SMOKE="1")
    run([binary], env=env, timeout=timeout)
    print("smoke run ok: the frozen shell booted and exited cleanly")


# ---------------------------------------------------------------------------
# macOS: signing, notarization, zip
# ---------------------------------------------------------------------------


def codesign_app(app_path: Path) -> None:
    identity = os.environ.get("DRCODE_CODESIGN_IDENTITY", "")
    if identity:
        run(
            [
                "codesign",
                "--deep",
                "--force",
                "--options",
                "runtime",
                "-s",
                identity,
                app_path,
            ]
        )
        print(f"Signed with identity: {identity}")
    else:
        run(["codesign", "--deep", "--force", "-s", "-", app_path])
        print(
            "Ad-hoc signed (no DRCODE_CODESIGN_IDENTITY set): on a clean "
            "machine, right-click the app → Open on first launch. For "
            "distribution: Developer ID identity + notarization."
        )


def notarytool_args() -> list[str] | None:
    """notarytool authentication args from the environment, if configured.

    Either a stored keychain profile (DRCODE_NOTARY_PROFILE — the usual
    local setup: ``xcrun notarytool store-credentials``) or the App Store
    Connect API key trio (DRCODE_NOTARY_KEY = path to .p8,
    DRCODE_NOTARY_KEY_ID, DRCODE_NOTARY_ISSUER — the CI setup).
    """
    profile = os.environ.get("DRCODE_NOTARY_PROFILE", "")
    if profile:
        return ["--keychain-profile", profile]
    key, key_id, issuer = (
        os.environ.get("DRCODE_NOTARY_KEY", ""),
        os.environ.get("DRCODE_NOTARY_KEY_ID", ""),
        os.environ.get("DRCODE_NOTARY_ISSUER", ""),
    )
    if key and key_id and issuer:
        return ["--key", key, "--key-id", key_id, "--issuer", issuer]
    return None


def notarize_app(app_path: Path) -> None:
    """Submit the zipped .app to notarytool and staple the ticket on.

    Skipped with a message when no credentials are configured (local
    ad-hoc builds: right-click → Open).
    """
    args = notarytool_args()
    if args is None:
        print("Notarization skipped: no notarytool credentials configured.")
        return
    zip_path = app_path.with_suffix(".app.zip")
    run(["ditto", "-c", "-k", "--keepParent", str(app_path), str(zip_path)])
    run(["xcrun", "notarytool", "submit", str(zip_path), "--wait", *args])
    run(["xcrun", "stapler", "staple", str(app_path)])
    zip_path.unlink(missing_ok=True)
    print("Notarized and stapled.")


def macos_arch() -> str:
    machine = platform.machine().lower()
    return "macos-arm64" if machine == "arm64" else "macos-x86_64"


def zip_app(app_path: Path, out: Path) -> Path:
    """ditto preserves the mach-o signatures and symlinks a plain zip mangles."""
    out.parent.mkdir(parents=True, exist_ok=True)
    run(["ditto", "-c", "-k", "--keepParent", str(app_path), str(out)])
    return out


def build_macos(dist_out: Path, smoke: bool) -> Path:
    if sys.platform != "darwin":
        raise SystemExit("target macos must run on macOS")
    app_path = build_shell(ROOT / "dist-desktop")
    codesign_app(app_path)
    notarize_app(app_path)
    if smoke:
        smoke_run(app_path)
    return zip_app(
        app_path, dist_out / f"DrCode-Desktop-{submodule_version()}-{macos_arch()}.zip"
    )


# ---------------------------------------------------------------------------
# Linux: shared helpers
# ---------------------------------------------------------------------------


def linux_arch() -> str:
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x86_64"
    if machine in ("aarch64", "arm64"):
        return "aarch64"
    raise SystemExit(f"Unsupported Linux architecture: {machine}")


def desktop_entry_text(exec_cmd: str, icon_name: str) -> str:
    return "\n".join(
        [
            "[Desktop Entry]",
            "Type=Application",
            f"Name={APP_NAME}",
            "Comment=PySide6 GUI shell over the drcode coding agent",
            f"Exec={exec_cmd}",
            f"Icon={icon_name}",
            "Terminal=false",
            "Categories=Development;IDE;Utility;",
            f"StartupWMClass={EXE_NAME}",
            "NoDisplay=false",
            "",
        ]
    )


def write_desktop_file(path: Path, exec_cmd: str, icon_name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(desktop_entry_text(exec_cmd, icon_name), encoding="utf-8")


# ---------------------------------------------------------------------------
# Linux: AppImage
# ---------------------------------------------------------------------------


def apprun_text(payload_dir: str) -> str:
    """AppRun script: exec the PyInstaller onedir binary.

    PySide6 ships its Qt (including the xcb platform plugin and its
    xcb-helper libs) inside the frozen output, so — unlike the pywebview
    sample, which had to bundle the WebKitGTK closure — no extra
    LD_LIBRARY_PATH/GI typelib wiring is needed.
    """
    return "\n".join(
        [
            "#!/bin/sh",
            'HERE="$(dirname "$(readlink -f "$0")")"',
            f'exec "$HERE/usr/bin/{payload_dir}/{EXE_NAME}" "$@"',
            "",
        ]
    )


def build_appimage(shell_dir: Path, dist_out: Path) -> Path:
    if not sys.platform.startswith("linux"):
        raise SystemExit("target appimage must run on Linux")
    arch = linux_arch()
    app_dir = ROOT / "build" / f"{EXE_NAME}.AppDir"
    if app_dir.exists():
        shutil.rmtree(app_dir)
    (app_dir / "usr" / "bin").mkdir(parents=True)

    # The PyInstaller onedir output becomes the app payload.
    shutil.copytree(shell_dir, app_dir / "usr" / "bin" / EXE_NAME, symlinks=True)

    generate_icon(app_dir / f"{EXE_NAME}.png", 512)
    write_desktop_file(
        app_dir / f"{EXE_NAME}.desktop",
        exec_cmd=f"{EXE_NAME}.AppDir/AppRun",
        icon_name=EXE_NAME,
    )

    apprun = app_dir / "AppRun"
    apprun.write_text(apprun_text(EXE_NAME), encoding="utf-8")
    apprun.chmod(0o755)

    dist_out.mkdir(parents=True, exist_ok=True)
    tool = _appimage_tool(arch)
    out_file = dist_out / f"DrCode-Desktop-{submodule_version()}-linux-{arch}.AppImage"
    run([tool, "--appimage-extract-and-run", str(app_dir), str(out_file)])
    return out_file


def _appimage_tool(arch: str) -> Path:
    """Download (and cache) appimagetool for the current arch."""
    tools_dir = ROOT / "build" / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    tool = tools_dir / f"appimagetool-{arch}.AppImage"
    if not tool.exists():
        url = (
            "https://github.com/AppImage/appimagetool/releases/download/"
            f"continuous/appimagetool-{arch}.AppImage"
        )
        run(["curl", "-sfL", "--retry", "3", "-o", str(tool), url])
        tool.chmod(0o755)
    return tool


# ---------------------------------------------------------------------------
# Linux: .deb
# ---------------------------------------------------------------------------


def deb_control_text(version: str, deb_arch: str, installed_size_kb: int) -> str:
    return "\n".join(
        [
            f"Package: {DEB_PACKAGE}",
            f"Version: {version}",
            f"Architecture: {deb_arch}",
            "Maintainer: DrMoriarty <drmoriarty.0@gmail.com>",
            f"Depends: {DEB_DEPENDS}",
            f"Installed-Size: {installed_size_kb}",
            "Section: devel",
            "Priority: optional",
            f"Description: {APP_NAME} — PySide6 GUI shell over the drcode core",
            " Runs the drcode coding agent in-process (git submodule) behind",
            " a Qt interface: live task queue, chat timeline with streaming,",
            " task management, settings and parity reports.",
            "",
        ]
    )


def _tree_size_kb(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        if p.is_file() and not p.is_symlink():
            total += p.stat().st_size
    return total // 1024


def build_deb(shell_dir: Path, dist_out: Path) -> Path:
    if not sys.platform.startswith("linux"):
        raise SystemExit("target deb must run on Linux")
    arch_map = {"x86_64": "amd64", "aarch64": "arm64"}
    arch, deb_arch = linux_arch(), arch_map[linux_arch()]
    version = submodule_version()
    root = ROOT / "build" / "deb-root"
    if root.exists():
        shutil.rmtree(root)

    opt = root / "opt" / DEB_PACKAGE
    shutil.copytree(shell_dir, opt / EXE_NAME, symlinks=True)

    bin_dir = root / "usr" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / DEB_PACKAGE).symlink_to(f"/opt/{DEB_PACKAGE}/{EXE_NAME}/{EXE_NAME}")

    icon_path = (
        root / "usr" / "share" / "icons" / "hicolor" / "512x512" / "apps"
    ) / f"{DEB_PACKAGE}.png"
    generate_icon(icon_path, 512)
    write_desktop_file(
        root / "usr" / "share" / "applications" / f"{DEB_PACKAGE}.desktop",
        exec_cmd=f"/usr/bin/{DEB_PACKAGE}",
        icon_name=DEB_PACKAGE,
    )

    control = root / "DEBIAN" / "control"
    control.parent.mkdir(parents=True, exist_ok=True)
    control.write_text(
        deb_control_text(version, deb_arch, _tree_size_kb(opt / EXE_NAME)),
        encoding="utf-8",
    )

    dist_out.mkdir(parents=True, exist_ok=True)
    out_file = dist_out / f"DrCode-Desktop-{version}-linux-{arch}.deb"
    run(["dpkg-deb", "--build", "--root-owner-group", str(root), str(out_file)])
    return out_file


# ---------------------------------------------------------------------------
# top-level
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build DrCode Desktop distributables"
    )
    parser.add_argument(
        "--target", choices=["macos", "appimage", "deb"], required=True
    )
    parser.add_argument(
        "--out", default=str(ROOT / "dist"), help="output dir for distributables"
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="after the build, boot the frozen shell with the smoke-exit hook",
    )
    args = parser.parse_args(argv)

    dist_out = Path(args.out)
    if args.target == "macos":
        out = build_macos(dist_out, args.smoke)
    elif args.target == "appimage":
        shell = build_shell(ROOT / "dist-desktop")
        out = build_appimage(shell, dist_out)
    else:
        shell = build_shell(ROOT / "dist-desktop")
        out = build_deb(shell, dist_out)
    print(f"\nReady: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
