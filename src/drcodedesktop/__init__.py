"""DrCode Desktop — a PySide6 GUI shell over the in-process drcode core.

The drcode repository is a git submodule (``./drcode``) and is imported
as an ordinary Python library: the GUI implements the same callback
seams the Textual TUI implements (``drcode.core.host.PipelineHost`` and
the UI-layer protocol of ``drcode.core.state.CoreState``) on Qt
primitives, without a single change inside the submodule.
"""

__version__ = "0.1.0"
