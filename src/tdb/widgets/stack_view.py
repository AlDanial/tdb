"""Stack view: displays call stack frames."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from textual.message import Message
from textual.widgets import DataTable

if TYPE_CHECKING:
    from tdb.dap.types import StackFrame


class StackView(DataTable):
    """Table showing the call stack for the current stopped thread."""

    DEFAULT_CSS = """
    StackView {
        height: 1fr;
    }
    """

    class FrameSelected(Message):
        def __init__(self, frame_id: int, source_path: str | None, line: int) -> None:
            self.frame_id = frame_id
            self.source_path = source_path
            self.line = line
            super().__init__()

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.border_title = "Stack"
        self.cursor_type = "row"
        self.zebra_stripes = True
        self._frame_ids: list[int] = []
        self._frames: list[StackFrame] = []
        self._updating: bool = False
        self._current_frame_id: int | None = None

    def on_mount(self) -> None:
        self.add_columns("#", "Function", "Location")

    def update_frames(self, frames: list[StackFrame], current_frame_id: int | None = None) -> None:
        self._current_frame_id = current_frame_id
        self._updating = True
        try:
            self.clear()
            self._frame_ids.clear()
            self._frames = frames

            for i, frame in enumerate(frames):
                source_name = ""
                if frame.source and frame.source.path:
                    source_name = f"{Path(frame.source.path).name}:{frame.line}"
                elif frame.source and frame.source.name:
                    source_name = f"{frame.source.name}:{frame.line}"

                self.add_row(str(i), frame.name, source_name, key=str(frame.id))
                self._frame_ids.append(frame.id)

            # Select current frame
            if current_frame_id is not None and frames:
                for i, frame in enumerate(frames):
                    if frame.id == current_frame_id:
                        self.move_cursor(row=i)
                        break
        finally:
            self._updating = False

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if self._updating:
            return
        row_idx = event.cursor_row
        if 0 <= row_idx < len(self._frames):
            frame = self._frames[row_idx]
            # Skip if this frame is already selected (avoids re-fetch on click)
            if frame.id == self._current_frame_id:
                return
            source_path = frame.source.path if frame.source else None
            self.post_message(self.FrameSelected(frame.id, source_path, frame.line))
