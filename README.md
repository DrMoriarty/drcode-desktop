# DrCode Desktop

PySide6 GUI shell over the in-process drcode core (git submodule `./drcode`).

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
