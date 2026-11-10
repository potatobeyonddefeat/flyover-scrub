"""flyover-scrub desktop app: drop a recording in, get a scrubbed copy next to it.

    flyover-scrub-gui            # needs the "gui" extra: pip install "flyover-scrub[gui]"

Everything runs on this computer; nothing is uploaded. The original file is never
changed: the result is written beside it as ``<name>-scrubbed.<ext>``.
"""

from __future__ import annotations

import subprocess
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .pipeline import MEDIA, FileResult, ScrubError, scrub_file
from .scrub import ScrubConfig

STYLE = """
#drop { border: 2px dashed palette(mid); border-radius: 14px; }
#drop[hover="true"] { border-color: palette(highlight); background: palette(alternate-base); }
#dropTitle { font-size: 20px; font-weight: 600; }
#hint, #detail { color: palette(placeholder-text); }
#row { border: 1px solid palette(midlight); border-radius: 10px; background: palette(base); }
#name { font-weight: 600; }
#detail[state="done"] { color: #2e9d5b; }
#detail[state="error"] { color: #d0453a; }
"""


def expand(paths: list[Path]) -> list[Path]:
    """Files as given; folders become the media files directly inside them."""
    out: list[Path] = []
    for p in paths:
        if p.is_dir():
            out += sorted(c for c in p.iterdir() if c.is_file() and c.suffix.lower() in MEDIA)
        elif p.is_file():
            out.append(p)
    return out


def summarise(r: FileResult, keep_hum: bool) -> str:
    """One line, in plain words, of what was done to the file."""
    n = r.ridges_found
    parts = [f"{n} steady tone{'s' if n != 1 else ''} removed" if n else "background re-synthesised"]
    if r.enf_before.present:
        gone = r.enf_after is not None and not r.enf_after.present
        parts.append("grid hum removed" if gone else "grid hum kept" if keep_hum else "grid hum reduced")
    if r.metadata_stripped:
        parts.append("metadata stripped")
    return "Scrubbed · " + " · ".join(parts)


def reveal(path: Path) -> None:
    """Show the file in Explorer / Finder, selected."""
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(path)])
            return
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
            return
    except OSError:
        pass
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))


class Worker(QObject):
    """Scrubs files one at a time on its own thread (queued signals form the queue)."""

    started = Signal(int)
    status = Signal(int, str)
    done = Signal(int, object)
    failed = Signal(int, str)

    @Slot(int, str, bool, bool)
    def run(self, job: int, path: str, keep_hum: bool, keep_metadata: bool) -> None:
        self.started.emit(job)
        try:
            cfg = ScrubConfig(keep_stationary_tones=keep_hum)
            res = scrub_file(Path(path), cfg=cfg, keep_metadata=keep_metadata,
                             status=lambda msg: self.status.emit(job, msg))
            self.done.emit(job, res)
        except ScrubError as e:
            self.failed.emit(job, str(e))
        except MemoryError:
            self.failed.emit(job, "not enough memory for a recording this long; try a shorter clip")
        except Exception as e:  # never let one bad file take the app down
            traceback.print_exc()
            self.failed.emit(job, f"unexpected error: {e}")


class DropZone(QFrame):
    dropped = Signal(list)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("drop")
        self.setAcceptDrops(True)
        self.setMinimumHeight(190)
        title = QLabel("Drop audio or video here")
        title.setObjectName("dropTitle")
        hint = QLabel("A scrubbed copy is saved next to the original. Nothing leaves this computer.")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        self.button = QPushButton("Choose files…")
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.button.clicked.connect(self._choose)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.setSpacing(10)
        lay.addStretch()
        for w in (title, hint):
            w.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(w)
        lay.addSpacing(6)
        lay.addWidget(self.button, alignment=Qt.AlignmentFlag.AlignCenter)
        lay.addStretch()

    def _choose(self) -> None:
        exts = " ".join(f"*{e}" for e in sorted(MEDIA))
        files, _ = QFileDialog.getOpenFileNames(self, "Choose recordings to scrub", "",
                                                f"Audio and video ({exts});;All files (*)")
        if files:
            self.dropped.emit([Path(f) for f in files])

    def _hover(self, on: bool) -> None:
        self.setProperty("hover", on)
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, e: QDragEnterEvent) -> None:
        if e.mimeData().hasUrls() and any(u.isLocalFile() for u in e.mimeData().urls()):
            e.acceptProposedAction()
            self._hover(True)

    def dragLeaveEvent(self, e) -> None:
        self._hover(False)

    def dropEvent(self, e: QDropEvent) -> None:
        self._hover(False)
        paths = [Path(u.toLocalFile()) for u in e.mimeData().urls() if u.isLocalFile()]
        e.acceptProposedAction()
        self.dropped.emit(paths)


class Row(QFrame):
    """One file: its name, what is happening to it, and where the result went."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.setObjectName("row")
        self.output: Path | None = None
        self.name = QLabel(path.name)
        self.name.setObjectName("name")
        self.name.setToolTip(str(path))
        self.detail = QLabel("Waiting")
        self.detail.setObjectName("detail")
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)  # the scrub is one pass with no meaningful percentage
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        self.bar.hide()
        self.show_btn = QPushButton("Show file")
        self.show_btn.clicked.connect(lambda: self.output and reveal(self.output))
        self.show_btn.hide()
        text = QVBoxLayout()
        text.setSpacing(4)
        text.addWidget(self.name)
        text.addWidget(self.detail)
        text.addWidget(self.bar)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.addLayout(text, 1)
        lay.addWidget(self.show_btn, alignment=Qt.AlignmentFlag.AlignVCenter)

    def set_detail(self, text: str, state: str = "") -> None:
        self.detail.setText(text)
        self.detail.setProperty("state", state)
        self.detail.style().unpolish(self.detail)
        self.detail.style().polish(self.detail)

    def working(self, text: str) -> None:
        self.bar.show()
        self.set_detail(text + "…")

    def finish(self, text: str, state: str, output: Path | None = None) -> None:
        self.bar.hide()
        self.output = output
        self.show_btn.setVisible(output is not None)
        self.set_detail(text, state)


class Window(QWidget):
    submit = Signal(int, str, bool, bool)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("flyover-scrub")
        self.resize(560, 620)
        self.setMinimumSize(420, 420)
        self.setStyleSheet(STYLE)
        self.rows: dict[int, Row] = {}
        self.pending = 0

        self.drop = DropZone()
        self.drop.dropped.connect(self.add)
        self.keep_hum = QCheckBox("Keep steady hum (fridge, mains)")
        self.keep_hum.setToolTip("Off is safer: mains hum can date a recording and show which power grid it was on.")
        self.keep_meta = QCheckBox("Keep metadata (GPS, dates)")
        self.keep_meta.setToolTip("Off is safer: a GPS tag gives the location away directly.")
        opts = QHBoxLayout()
        opts.addWidget(self.keep_hum)
        opts.addWidget(self.keep_meta)
        opts.addStretch()

        self.list = QVBoxLayout()
        self.list.setSpacing(8)
        self.list.setContentsMargins(0, 0, 0, 0)
        self.list.addStretch()
        inner = QWidget()
        inner.setLayout(self.list)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(inner)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 18, 18, 18)
        lay.setSpacing(12)
        lay.addWidget(self.drop)
        lay.addLayout(opts)
        lay.addWidget(scroll, 1)

        self.thread = QThread(self)
        self.worker = Worker()
        self.worker.moveToThread(self.thread)
        self.submit.connect(self.worker.run)
        self.worker.started.connect(lambda j: self.rows[j].working("Starting"))
        self.worker.status.connect(lambda j, msg: self.rows[j].working(msg))
        self.worker.done.connect(self._done)
        self.worker.failed.connect(self._failed)
        self.thread.start()

    def add(self, paths: list[Path]) -> None:
        for p in expand(paths):
            job = len(self.rows)
            row = self.rows[job] = Row(p)
            row.keep_hum = self.keep_hum.isChecked()
            self.list.insertWidget(0, row)  # newest on top, right under the options
            self.pending += 1
            # Options are read now, so changing a checkbox doesn't alter files already queued.
            self.submit.emit(job, str(p), row.keep_hum, self.keep_meta.isChecked())

    def _done(self, job: int, res: FileResult) -> None:
        self.pending -= 1
        row = self.rows[job]
        row.finish(f"{summarise(res, row.keep_hum)}\nSaved as {res.output.name}", "done", res.output)

    def _failed(self, job: int, msg: str) -> None:
        self.pending -= 1
        self.rows[job].finish(f"Couldn't scrub this file: {msg}", "error")

    def closeEvent(self, e) -> None:
        if self.pending:
            n = self.pending
            ask = QMessageBox.question(
                self, "Still scrubbing",
                f"{n} file{'s are' if n != 1 else ' is'} not finished. Quit anyway?\n"
                "An unfinished file may be left behind incomplete.")
            if ask != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
        self.thread.quit()
        if not self.thread.wait(200):
            # A scrub can't be interrupted mid-pass; don't leave a hidden process grinding on.
            self.thread.terminate()
            self.thread.wait(1000)
        e.accept()


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("flyover-scrub")
    win = Window()
    win.show()
    win.add([Path(a) for a in sys.argv[1:]])  # files dropped onto the app icon / "Open with"
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
