"""Minimal StreamBlock stub for the skeleton stage (plan task 1).

The pipeline grows live-streamed chat blocks through the
``drcode.core.host.StreamBlock`` protocol. Task 1 has no chat view yet:
this stub accumulates the content and routes every delta into the
event hub (``stream_delta`` signal + the file log), so streaming is
observable and the accumulated text is ready for task 3's real chat
renderer.
"""

from __future__ import annotations

import datetime

from .event_hub import EventHub


class StreamBlockStub:
    """Headless chat block: accumulate, log, keep for later."""

    def __init__(
        self,
        hub: EventHub,
        task_id: str,
        kind: str,
        at: datetime.datetime | None = None,
    ) -> None:
        self.hub = hub
        self.task_id = task_id
        self.kind = kind
        self.at = at or datetime.datetime.now()
        # The pipeline reads/writes this on orphaned-block checks (see
        # pipeline_stream): the skeleton has one never-bumped epoch.
        self.stream_epoch = 0
        self._content = ""
        self._streaming = False

    def begin(self) -> None:
        self._streaming = True
        self.hub.append_log(f"stream_begin {self.task_id} [{self.kind}]")

    def append(self, text: str) -> None:
        self._content += text
        self.hub.emit_stream_delta(self.task_id, text)

    def thinking(self, on: bool) -> None:
        self.hub.append_log(f"stream_thinking {self.task_id}: {on}")

    def finish(self) -> None:
        self._streaming = False
        self.hub.append_log(
            f"stream_finish {self.task_id} [{self.kind}] ({len(self._content)} chars)"
        )

    def set_content(self, text: str) -> None:
        self._content = text

    def get_content(self) -> str:
        return self._content
