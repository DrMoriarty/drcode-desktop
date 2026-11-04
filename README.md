# DrCode Desktop

PySide6 GUI shell over the in-process drcode core (git submodule `./drcode`).

Screens: the live task queue (TUI sidebar parity) with the selected
task's chat pane — the transcript rendered through a Rich→Qt adapter
(markdown, fenced code, highlighted tool-call JSON, collapsible
reasoning/tool-call blocks) plus live streaming of the active run. The
chat pane carries the read-only todo checklist strip; the queue pane
bottom shows the run-activity indicator (fresh/stale, stall countdown,
session stats, git unmerged-work counter) driven by the shell's stall
watchdog. Reports → Token usage / Statistics (Ctrl+G) read the same
stores the TUI reads; Settings → Notifications writes the same project
settings file. Parity state and explicit gaps: PARITY.md.

## Run

```sh
uv sync
uv run drcode-desktop            # or: uv run python -m drcodedesktop
```


## Packaging (plan task 8)


The payload is the frozen GUI shell **plus the drcode submodule**: the
version of every distributable is the submodule's `version` file (the
GUI version IS the core version).

```sh
git submodule update --init     # the payload needs the submodule checked out
uv sync --group build           # pyinstaller + pillow

uv run python scripts/build_dist.py --target macos     # DrCode-Desktop-<v>-<arch>.zip
uv run python scripts/build_dist.py --target appimage  # .AppImage (Linux)
uv run python scripts/build_dist.py --target deb       # .deb (Linux)
```

- **macOS**: `DrCode Desktop.app` (Info.plist, generated icon) inside a
  ditto zip. Ad-hoc signed locally — right-click → Open on first
  launch; set `DRCODE_CODESIGN_IDENTITY` for a Developer ID signature
  and provide notarytool credentials (`DRCODE_NOTARY_PROFILE`, or
  `DRCODE_NOTARY_KEY`/`DRCODE_NOTARY_KEY_ID`/`DRCODE_NOTARY_ISSUER`)
  to notarize + staple.
- **AppImage**: the PyInstaller onedir as the AppDir payload; PySide6
  ships its own Qt, only the glibc family stays external.
- **.deb**: installs to `/opt/drcode-desktop` with a
  `/usr/bin/drcode-desktop` symlink; depends on the distro Qt runtime
  (`libgl1`, `libxcb-*`, …).

Add `--smoke` to boot the freshly built shell with the smoke-exit hook
(`DRCODE_DESKTOP_SMOKE=1`) right after the build.

CI (`.github/workflows/release.yml`) builds both platforms
(macos-14, ubuntu-22.04) and attaches the artifacts to the GitHub
release on `v*` tags; a tag whose number disagrees with
`drcode/version` fails the build.

## Development

```sh
uv run pytest                    # GUI test suite
uv run ruff check . && uv run pyright
```
