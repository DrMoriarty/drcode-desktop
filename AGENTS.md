# AGENTS.md

## What this repo is

PySide6 GUI shell over the drcode core, which lives in the git submodule
`./drcode` and is imported **in-process** as an ordinary Python library
(uv editable path dependency — no IPC, no sidecar). Shell code: `src/drcodedesktop/`.

## Setup & commands

```sh
git submodule update --init   # required: ./drcode is a uv path dependency
uv sync                       # dev deps
uv run drcode-desktop         # run the app (or: uv run python -m drcodedesktop)
uv run pytest                 # full suite (addopts already include -n 4)
uv run ruff check . && uv run pyright
```

- Lint gate = ruff + pyright, both run on the shell only (`src`, `tests`,
  `scripts`); the submodule is excluded and checked by its **own** tooling.
  Same for tests: the suite covers the shell, the TUI must stay untouched.
- `pyright` resolves `drcode.*` imports via `extraPaths = ["drcode/src"]`.
- PySide6 is pinned `>=6.8,<6.9` (Essentials): newer wheels fail to dlopen
  on macOS < 14.4. Don't bump past 6.9.

## Testing quirks

- `tests/conftest.py` sets `DRCODE_USER_CONFIG_DIR` / `DRCODE_DESKTOP_CONFIG_DIR`
  sandbox env **at import time, before any drcode import** — drcode resolves
  its config dir at import time; adding imports above that redirect breaks
  isolation.
- All Qt tests run with `QT_QPA_PLATFORM=offscreen` (set in conftest);
  the suite never opens real windows.
- No network: LLM responses are scripted via `FunctionModel` mocks ported
  from drcode's `tests/network_mocks.py` (`tests/mocks.py`) — reuse that
  reaction semantics, don't add HTTP.
- Run one test: `uv run pytest tests/test_core_shell.py::<name>` (xdist `-n 4`
  applies unless overridden).

## Architecture rules (hard conventions)

- **Never import `drcode.tui`** from `src/drcodedesktop/` or `tests/` —
  GUI parity is reproduced from the same data sources, not by reusing TUI
  code. Data sources: `drcode.core`, `usage_store`, `stats`, `settings`
  (+ `task_env.drcode_branch_state` for the git indicator). Audit: PARITY.md.
- Threading model = single asyncio loop riding the Qt event loop via
  `qasync` (same as the TUI): worker bodies, timers and UI callbacks all
  on one thread; blocking work (e.g. git snapshots) goes to a thread.
- Chat glyphs (`›`, `★`, `─`, …) render TUI output verbatim — they are
  whitelisted in ruff `allowed-confusables`; don't "fix" them.
- Event seam: the core emits `task_updated` / `stream_delta` /
  `service_event` / `notify` through the host callback protocol
  (`drcode.core`); GUI rebuilds views from these events (often debounced).

## Versioning & packaging

- The GUI version **is** the submodule's `drcode/version` file. A `v*` tag
  whose number disagrees with it fails the release build
  (`.github/workflows/release.yml` — the only workflow; it also runs pytest).
- Build: `uv sync --group build`, then
  `uv run python scripts/build_dist.py --target macos|appimage|deb [--smoke]`.
  `--smoke` boots the built app with `DRCODE_DESKTOP_SMOKE=1` (in-app
  smoke-exit hook in `app.py`) right after the build.

## Reference docs

- `PARITY.md` — TUI↔GUI parity checklist with explicit `[ ]` gaps; update
  it when porting or diverging from TUI behavior.
- `pyside6-gui-plan.md` — the original task-chain plan and ground rules
  (vertical slices, TUI untouched, submodule edits only as separate tasks).
