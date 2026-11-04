# DrCode Desktop

PySide6 GUI shell over the in-process drcode core (git submodule `./drcode`).

Screens: the live task queue (TUI sidebar parity) with the selected
task's chat pane — the transcript rendered through a Rich→Qt adapter
(markdown, fenced code, highlighted tool-call JSON, collapsible
reasoning/tool-call blocks) plus live streaming of the active run.

## Run

```sh
uv sync
uv run drcode-desktop            # or: uv run python -m drcodedesktop
```

## Development

```sh
uv run pytest                    # GUI test suite
uv run ruff check . && uv run pyright
```
