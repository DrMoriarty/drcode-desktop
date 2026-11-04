# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the frozen DrCode Desktop GUI shell.

Payload = the PySide6 shell (``drcodedesktop``) + the drcode submodule
imported in-process as an ordinary library. There is no pre-built drcode
onedir payload here (that is the pywebview sample's approach — drcode
there ships as a sidecar TUI binary): PyInstaller analyzes
``packaging/entry.py`` and pulls the ``drcode`` package from the venv's
editable path dependency, together with pydantic-ai, qasync and friends.

Extra data the frozen build needs:

  - ``version`` (the submodule's version file) at the bundle root —
    ``drcode.version._frozen_version`` reads it from ``sys._MEIPASS``;
    the GUI version IS the submodule version.
  - ``drcode/prompts`` — the core reads prompt templates from
    ``Path(drcode.prompts.__file__).parent / "prompts"`` at runtime.

The app is windowed (console=False): the shell owns the GUI, core logs
go to the project's ``.drcode`` log file, tracebacks surface in the
crash dialog / Console.app.
"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

ROOT = Path(SPECPATH).resolve().parent  # packaging/, repo root is its parent
SUBMODULE = ROOT / "drcode"
VERSION = (SUBMODULE / "version").read_text(encoding="utf-8").strip()

datas = [
    # Single source of truth for the frozen version (see drcode.version).
    (str(SUBMODULE / "version"), "."),
    # Prompt templates the core loads relative to its own package dir.
    (str(SUBMODULE / "src" / "drcode" / "prompts"), "drcode/prompts"),
    # Packages that read their own dist metadata at runtime:
    # genai_prices.version() and the pydantic_ai family (via
    # pydantic_ai), drcode/drcodedesktop importlib.metadata fallbacks
    # (drcode.version, package_watch).
    *copy_metadata("genai_prices"),
    *copy_metadata("drcode"),
    *copy_metadata("drcode-desktop"),
    *copy_metadata("pydantic_ai"),
    *copy_metadata("pydantic_ai_slim"),
    *copy_metadata("pydantic_graph"),
    *copy_metadata("pydantic_evals"),
    *copy_metadata("pydantic_settings"),
    *copy_metadata("pydantic"),
]

hiddenimports = [
    # Insurance for the core's function-level imports: PyInstaller's
    # bytecode scan covers lazy `import` statements, but the core is the
    # payload — make the whole UI-neutral layer explicit. The TUI is NOT
    # collected: the GUI never imports drcode.tui (plan rule).
    *collect_submodules("drcode.core"),
]

excludes = [
    # logfire rides in as a transitive dependency and hooks pydantic
    # through the `pydantic` entry-point group; its schema-validator
    # patch calls inspect.getsource() — impossible in a frozen (source-
    # less) bundle. The GUI never uses logfire: cut the `logfire`
    # package (the lazy logfire_api stub keeps working — pydantic_graph
    # imports it unconditionally), then the plugin discovery finds
    # nothing and pydantic runs unpatched. logfire_api itself must stay.
    "logfire",
]

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT / "src"), str(SUBMODULE / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DrCodeDesktop",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="DrCodeDesktop",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="DrCode Desktop.app",
        info_plist={
            "CFBundleName": "DrCode Desktop",
            "CFBundleDisplayName": "DrCode Desktop",
            "CFBundleIdentifier": "com.drmoriarty.drcode-desktop",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "CFBundlePackageType": "APPL",
            # PySide6 6.8 wheels target macOS 12+.
            "LSMinimumSystemVersion": "12.0",
            "NSHighResolutionCapable": True,
            "NSPrincipalClass": "NSApplication",
            "NSSupportsAutomaticGraphicsSwitching": True,
        },
    )
