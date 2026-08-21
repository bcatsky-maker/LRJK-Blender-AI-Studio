import hashlib
import http.server
import json
import os
import re
import secrets
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QRect, QSettings, QSize, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QIcon, QPixmap, QTextCursor
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from src.core.generator import AIGenerator

from src.core.ai_provider import (
    AIProviderError,
    request_scene_program,
    test_provider_connection,
    validate_scene_program,
)
from src.core.asset_manager import StudioAssetManager
from src.core.memory_db import ExecutionMemoryDB
from src.core.text_to_3d import Tripo3DError, generate_mesh_from_text


def get_resource_path(relative_path: str) -> Path:
    if getattr(sys, "frozen", False):
        base_path = Path(sys.executable).parent / "_internal"
        if not base_path.exists():
            base_path = Path(sys.executable).parent
    else:
        base_path = Path(__file__).parent.parent.parent

    return base_path / relative_path


class FlowLayout(QLayout):
    """
    A layout that arranges its widgets left-to-right and WRAPS to the next
    line when it runs out of horizontal room (like text). Used for the app's
    button rows so nothing is ever clipped off the right edge on a smaller
    window / lower resolution - the buttons simply reflow onto additional
    lines and the whole window stays usable. (Standard Qt "flow layout"
    pattern, adapted for PySide6.)
    """

    def __init__(self, parent=None, margin=0, hspacing=8, vspacing=6):
        super().__init__(parent)
        self._items = []
        self._hspace = hspacing
        self._vspace = vspacing
        self.setContentsMargins(margin, margin, margin, margin)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientations(Qt.Orientation(0))

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        return size

    def _do_layout(self, rect, test_only):
        margins = self.contentsMargins()
        effective = rect.adjusted(
            margins.left(), margins.top(), -margins.right(), -margins.bottom()
        )
        x = effective.x()
        y = effective.y()
        line_height = 0
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + self._hspace
            if next_x - self._hspace > effective.right() and line_height > 0:
                x = effective.x()
                y = y + line_height + self._vspace
                next_x = x + hint.width() + self._hspace
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + margins.bottom()


def get_app_version() -> str:
    """
    Reads the running app's version from installer_setup.iss's
    MyAppVersion define - the same file build_all.py's
    increment_iss_version() bumps on every build - instead of relying on
    a hardcoded string here that silently drifts out of sync with what's
    actually shipped. Falls back to "0.0.0" (never crashes, never used
    for anything but display/comparison) if the file's missing or the
    pattern doesn't match, e.g. a dev checkout that hasn't set one up.
    """
    try:
        iss_path = get_resource_path("installer_setup.iss")
        content = iss_path.read_text(encoding="utf-8")
        match = re.search(r'#define\s+MyAppVersion\s+"(\d+\.\d+\.\d+)"', content)
        if match:
            return match.group(1)
    except OSError:
        pass
    return "0.0.0"


def _version_tuple(v: str):
    """Best-effort "X.Y.Z" -> (X, Y, Z) for ordering comparisons. Non-numeric
    or malformed segments become 0 rather than raising, since this is only
    used to decide whether to show an update banner - never worth crashing
    over a weird tag name."""
    parts = []
    for chunk in (v or "").strip().lstrip("v").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


# =====================================================================
# SILENT BACKGROUND GITHUB UPDATE CHECKER
# =====================================================================
class GitHubUpdateCheckerWorker(QThread):
    """
    Silently checks GitHub repository releases for newer builds in the
    background - "silent" meaning it never blocks or interrupts app
    startup, not that a found update goes unreported (see
    MainWindow.notify_github_update, which surfaces a banner in the UI).

    Points at the PUBLIC releases repo (bcatsky-maker/LRJK-Studio-Releases),
    where build_update.py --push publishes each update as a GitHub Release -
    NOT the source repo, which is private (its /releases API would 404 without
    a token). Was originally pointed at a repo that didn't exist, so every
    check 404'd and silently did nothing forever; it also compared versions
    with a bare != instead of an actual "is newer" check, against a hardcoded
    constant that never matched the installed build - see get_app_version() /
    _version_tuple() above.
    """

    update_available = Signal(str, str)

    def __init__(
        self, current_version=None, repo_owner="bcatsky-maker", repo_name="LRJK-Studio-Releases"
    ):
        super().__init__()
        self.current_version = current_version or get_app_version()
        self.repo_owner = repo_owner
        self.repo_name = repo_name

    def run(self):
        url = f"https://api.github.com/repos/{self.repo_owner}/{self.repo_name}/releases/latest"
        req = urllib.request.Request(url, headers={"User-Agent": "LRJK-Blender-AI-Studio/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=4.0) as response:
                data = json.loads(response.read().decode("utf-8"))
                latest_tag = data.get("tag_name", "").replace("v", "").strip()
                html_url = data.get("html_url", "")

                if latest_tag and _version_tuple(latest_tag) > _version_tuple(self.current_version):
                    self.update_available.emit(latest_tag, html_url)
        except Exception:
            # No releases published yet (404), no network, rate-limited,
            # malformed response, etc. - none of these should ever surface
            # as an error to the user; this check is best-effort only.
            pass


# =====================================================================
# SELF-HOSTED SILENT AUTO-UPDATER
# =====================================================================
class SilentUpdateWorker(QThread):
    """
    Checks a self-hosted UPDATE FEED - a small JSON manifest produced by
    build_update.py (updates/latest_update.json) - for a newer version. If one
    is found, emits update_ready(version, installer_url, sha256); the main
    window then silently downloads + installs it (when auto-update is on) or
    surfaces a banner.

    The feed URL is wherever you host the contents of build_update.py's
    'updates/' folder: an HTTPS web host, a GitHub raw URL, a direct cloud
    link, or a file:// / UNC path. Non-blocking and best-effort - any failure
    (no feed configured, offline, malformed) is silent.
    """

    update_ready = Signal(str, str, str)  # version, installer_url, sha256

    def __init__(self, feed_url, current_version=None):
        super().__init__()
        self.feed_url = (feed_url or "").strip()
        self.current_version = current_version or get_app_version()

    def run(self):
        if not self.feed_url:
            return
        try:
            req = urllib.request.Request(
                self.feed_url, headers={"User-Agent": "LRJK-Blender-AI-Studio/1.0"}
            )
            with urllib.request.urlopen(req, timeout=6.0) as resp:
                manifest = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return

        version = str(manifest.get("version", "")).strip()
        if not version or _version_tuple(version) <= _version_tuple(self.current_version):
            return

        installer_url = str(manifest.get("url", "")).strip()
        if not installer_url:
            filename = str(manifest.get("file", "")).strip()
            if not filename:
                return
            # Resolve the installer relative to the manifest's own URL.
            installer_url = urllib.parse.urljoin(self.feed_url, filename)

        self.update_ready.emit(version, installer_url, str(manifest.get("sha256", "")).strip())


class _UpdateDownloadWorker(QThread):
    """Downloads the update installer off the GUI thread and verifies its
    sha256 before it's ever launched (integrity check on auto-run code)."""

    done = Signal(str)  # local installer path
    failed = Signal(str)

    def __init__(self, url, sha256):
        super().__init__()
        self.url = url
        self.sha256 = (sha256 or "").lower()

    def run(self):
        try:
            dest = Path(tempfile.gettempdir()) / "LRJK_Blender_AI_Studio_Update.exe"
            req = urllib.request.Request(
                self.url, headers={"User-Agent": "LRJK-Blender-AI-Studio/1.0"}
            )
            with urllib.request.urlopen(req, timeout=180.0) as resp, open(dest, "wb") as f:
                shutil.copyfileobj(resp, f)
        except Exception as e:
            self.failed.emit(str(e))
            return

        if self.sha256:
            h = hashlib.sha256()
            with open(dest, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
            if h.hexdigest().lower() != self.sha256:
                self.failed.emit("checksum mismatch - refusing to run the update for safety")
                return

        self.done.emit(str(dest))


# =====================================================================
# THREAD-SAFE HTTP BRIDGE SERVER
# =====================================================================
class BridgeSignalHub(QObject):
    payload_received = Signal(dict)


SIGNAL_HUB = BridgeSignalHub()


class BridgeHTTPRequestHandler(http.server.BaseHTTPRequestHandler):
    """
    Local HTTP bridge the Blender add-on (and the optional browser
    extension) talk to on 127.0.0.1.

    SECURITY: this used to accept any POST body from anyone who could
    reach 127.0.0.1:<port> - no authentication, no Content-Type check.
    Since the Blender add-on used to exec() whatever Python string came
    back in the response, that was effectively an unauthenticated local
    code-execution surface (any local process, or - because the server
    never validated Content-Type - even a "simple" cross-origin fetch()
    from a webpage open in the user's browser while the app was running,
    since a text/plain body bypasses the CORS preflight that a custom
    header or a declared application/json content type would force).

    Two independent fixes now apply to every POST:
      1. Content-Type must be exactly 'application/json'. This alone
         makes the request "non-simple" for browsers, forcing a CORS
         preflight - which this server never approves (no
         Access-Control-Allow-* headers are ever sent), so browser-based
         drive-by requests are refused before they can be sent at all.
      2. A shared-secret token (generated once per install, shown in the
         desktop app's AI Settings dialog, pasted into the Blender
         panel's "Bridge Token" field) must be present in the
         'X-LRJK-Token' header and match exactly.
      3. The exec() itself is gone - see handle_incoming_blender_payload:
         the response now carries a whitelisted {"action", "params"}
         descriptor, never a code string. Even a fully authenticated,
         well-formed request can only trigger one of a small fixed set
         of Blender operations.
    """

    expected_token = None  # set by MainWindow at startup
    # Filled in by MainWindow._send_bridge_response (GUI thread) with the
    # dict the waiting do_POST should return; declared here so it's a real
    # class attribute rather than one that only springs into existence at
    # runtime.
    last_response_data: dict | None = None

    # Signals when MainWindow.handle_incoming_blender_payload (running on
    # the GUI thread, via the queued payload_received connection) has
    # finished processing a request and set last_response_data. do_POST
    # blocks on this instead of a fixed sleep() - the old sleep(0.1) was a
    # race that happened to usually win for the instant rule-based
    # fallback but would silently return a stale/default response for any
    # real network call (an AI provider request, or - especially - a
    # Tripo3D text-to-3D job that can take up to several minutes).
    response_ready = threading.Event()

    def _json_response(self, code: int, payload: dict):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))

    def do_OPTIONS(self):
        # No CORS headers are ever sent (see class docstring), so this
        # just refuses preflight outright rather than pretending to
        # support cross-origin browser requests.
        self.send_response(403)
        self.end_headers()

    def do_POST(self):
        content_type = self.headers.get("Content-Type", "")
        if not content_type.split(";")[0].strip().lower() == "application/json":
            self._json_response(
                415, {"status": "error", "message": "Content-Type must be application/json"}
            )
            return

        token = self.headers.get("X-LRJK-Token", "")
        if (
            not BridgeHTTPRequestHandler.expected_token
            or token != BridgeHTTPRequestHandler.expected_token
        ):
            self._json_response(
                401, {"status": "error", "message": "Missing or invalid X-LRJK-Token header"}
            )
            return

        content_length = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_length)

        try:
            payload = json.loads(post_data.decode("utf-8"))
            BridgeHTTPRequestHandler.response_ready.clear()
            SIGNAL_HUB.payload_received.emit(payload)

            # Text-to-3D generation (Tripo3D) can take up to ~180s to
            # finish rendering a mesh; everything else (rule-based
            # fallback, a single AI-provider chat-completion call) should
            # finish in a handful of seconds.
            wait_timeout = 240.0 if payload.get("type") == "generate_mesh_from_text" else 30.0
            if not BridgeHTTPRequestHandler.response_ready.wait(timeout=wait_timeout):
                self._json_response(
                    504,
                    {
                        "status": "error",
                        "message": f"Studio App did not respond within {wait_timeout:.0f}s.",
                    },
                )
                return

            response = getattr(
                BridgeHTTPRequestHandler,
                "last_response_data",
                {"status": "ok", "message": "Payload received by LRJK Studio"},
            )
            self._json_response(200, response)
        except Exception:
            self._json_response(500, {"status": "error", "message": "Failed to process payload"})

    def do_GET(self):
        # Read-only health check, no state is exposed or changed, so this
        # is left unauthenticated on purpose - it lets the Blender panel
        # show "offline" vs "online" without requiring the token to be
        # pasted in first.
        self._json_response(200, {"status": "online", "message": "LRJK AI Studio Bridge Active"})

    def log_message(self, format, *args):
        pass


class BridgeServerWorker(QThread):
    def __init__(self, port=8081):
        super().__init__()
        self.port = port
        self.httpd = None

    def run(self):
        try:
            socketserver.TCPServer.allow_reuse_address = True
            with socketserver.TCPServer(
                ("127.0.0.1", self.port), BridgeHTTPRequestHandler
            ) as httpd:
                self.httpd = httpd
                httpd.serve_forever()
        except Exception as e:
            print(f"Bridge Server Thread Warning: {e}")

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()


# =====================================================================
# UI COMPONENTS
# =====================================================================
class ConsoleLogDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("📊 Live Code Stream, Errors & Generation Metrics")
        self.resize(780, 520)

        layout = QVBoxLayout(self)

        self.log_display = QTextEdit()
        self.log_display.setReadOnly(True)
        self.log_display.setStyleSheet("""
            QTextEdit {
                background-color: #1e1e1e;
                color: #d4d4d4;
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: 13px;
                border: 1px solid #333333;
                border-radius: 4px;
            }
        """)
        layout.addWidget(self.log_display)

        btn_layout = QHBoxLayout()
        self.clear_btn = QPushButton("Clear Logs")
        self.clear_btn.clicked.connect(self.log_display.clear)
        self.close_btn = QPushButton("Close Console")
        self.close_btn.clicked.connect(self.accept)

        btn_layout.addWidget(self.clear_btn)
        btn_layout.addStretch()
        btn_layout.addWidget(self.close_btn)
        layout.addLayout(btn_layout)

    def append_log(self, text: str, level: str = "info"):
        cursor = self.log_display.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.log_display.setTextCursor(cursor)

        color_map = {"info": "#4EC9B0", "error": "#F44747", "code": "#CE9178", "metric": "#DCDCAA"}
        color = color_map.get(level, "#d4d4d4")
        formatted = f'<span style="color: {color};">[{time.strftime("%H:%M:%S")}] {text}</span><br>'
        self.log_display.insertHtml(formatted)
        self.log_display.ensureCursorVisible()


class SavedHistoryDialog(QDialog):
    def __init__(self, db: ExecutionMemoryDB, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("💾 Saved Script History (Database)")
        self.resize(650, 400)

        layout = QVBoxLayout(self)
        self.list_widget = QListWidget()
        layout.addWidget(self.list_widget)

        records = self.db.get_all_saved_scripts()
        if not records:
            self.list_widget.addItem("No saved execution history in database yet.")
        else:
            for row in records:
                rec_id, prompt, exec_time, created_at = row
                self.list_widget.addItem(
                    f"[{created_at}] ID {rec_id} | '{prompt}' ({exec_time:.4f}s)"
                )

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn)


class AspectRatioLabel(QLabel):
    """
    Displays the studio banner at its native aspect ratio, scaled to the
    window's current width - the whole image always visible, never
    stretched and never cropped.

    Previously this used a hardcoded setFixedHeight(200) together with
    Qt.KeepAspectRatioByExpanding, which scales the pixmap up until it
    covers BOTH dimensions of that fixed 200px-tall box and silently
    clips whatever overflows - since the banner's own aspect ratio
    essentially never matches "current window width : 200px", that
    always chopped a chunk off one edge or the other. Instead, the
    label's own height is derived from the image's real aspect ratio and
    its current width, so there's nothing to crop and nothing to
    stretch - it just fits.
    """

    def __init__(self, image_path: Path, parent=None):
        super().__init__(parent)
        self.original_pixmap = QPixmap(str(image_path))
        self.setScaledContents(False)
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._apply_scaled_pixmap(max(self.width(), 1))

    def _apply_scaled_pixmap(self, width: int):
        if self.original_pixmap.isNull() or width <= 0:
            return
        aspect = self.original_pixmap.height() / self.original_pixmap.width()
        height = max(1, round(width * aspect))
        if self.height() != height:
            self.setFixedHeight(height)
        scaled = self.original_pixmap.scaled(
            width, height, Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        self.setPixmap(scaled)

    def resizeEvent(self, event):
        self._apply_scaled_pixmap(self.width())
        super().resizeEvent(event)


class APISettingsDialog(QDialog):
    def __init__(self, settings: QSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("⚙️ AI Service & API Configuration")
        self.setFixedSize(560, 760)

        layout = QVBoxLayout(self)

        form_box = QGroupBox("AI Provider & Tier Setup")
        form_layout = QFormLayout(form_box)

        self.provider_combo = QComboBox()
        self.provider_combo.addItems(
            [
                "OpenAI (Paid / Tiered)",
                "Anthropic Claude (Paid)",
                "Ollama / Local AI (Free)",
                "Custom REST API Endpoint",
            ]
        )

        self.endpoint_input = QLineEdit()
        self.endpoint_input.setPlaceholderText(
            "https://api.openai.com/v1 or http://127.0.0.1:11434/v1"
        )

        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.Password)
        self.api_key_input.setPlaceholderText(
            "Enter Secret Key (leave empty for free local models)"
        )

        self.model_input = QLineEdit()
        self.model_input.setPlaceholderText("e.g. gpt-4o, claude-3-5-sonnet, llama3")

        self.bridge_port_input = QLineEdit()
        self.bridge_port_input.setPlaceholderText("8081")

        saved_provider = self.settings.value("api_provider", "OpenAI (Paid / Tiered)")
        self.provider_combo.setCurrentText(saved_provider)
        self.endpoint_input.setText(
            self.settings.value("api_endpoint", "https://api.openai.com/v1")
        )
        self.api_key_input.setText(self.settings.value("api_key", ""))
        self.model_input.setText(self.settings.value("api_model", "gpt-4o"))
        self.bridge_port_input.setText(self.settings.value("bridge_port", "8081"))

        self.provider_combo.currentTextChanged.connect(self.on_provider_changed)

        form_layout.addRow("AI Service Preset:", self.provider_combo)
        form_layout.addRow("API Endpoint:", self.endpoint_input)
        form_layout.addRow("API Secret Key:", self.api_key_input)
        form_layout.addRow("Model Name:", self.model_input)
        form_layout.addRow("Blender Bridge Port:", self.bridge_port_input)

        layout.addWidget(form_box)

        # --- Bridge pairing token: paste this into the Blender add-on's
        # "Bridge Token" field so requests from Blender are authenticated.
        token_box = QGroupBox("🔑 Blender Bridge Pairing Token")
        token_layout = QVBoxLayout(token_box)
        token_hint = QLabel(
            'Paste this token into the LRJK AI Studio panel\'s "Bridge Token" field in Blender. '
            "Requests without a matching token are rejected."
        )
        token_hint.setWordWrap(True)
        token_row = QHBoxLayout()
        self.token_display = QLineEdit()
        self.token_display.setReadOnly(True)
        self.token_display.setText(self.settings.value("bridge_token", ""))
        self.copy_token_btn = QPushButton("Copy")
        self.copy_token_btn.clicked.connect(self.copy_token)
        self.regenerate_token_btn = QPushButton("Regenerate")
        self.regenerate_token_btn.clicked.connect(self.regenerate_token)
        token_row.addWidget(self.token_display)
        token_row.addWidget(self.copy_token_btn)
        token_row.addWidget(self.regenerate_token_btn)
        token_layout.addWidget(token_hint)
        token_layout.addLayout(token_row)
        layout.addWidget(token_box)

        # --- Tripo3D key: powers the separate "Generate 3D Mesh from
        # Text" button in the Blender panel. Kept independent of the
        # scene-action AI provider above since it's a different service
        # doing a different job (an actual mesh, not an action descriptor).
        mesh_box = QGroupBox("🧊 Text-to-3D Mesh Generation (Tripo3D)")
        mesh_layout = QFormLayout(mesh_box)
        mesh_hint = QLabel(
            'Powers the Blender panel\'s "Generate 3D Mesh from Text" button. '
            "Get a free key at tripo3d.ai - leave blank to disable that button."
        )
        mesh_hint.setWordWrap(True)
        mesh_layout.addRow(mesh_hint)
        self.tripo3d_key_input = QLineEdit()
        self.tripo3d_key_input.setEchoMode(QLineEdit.Password)
        self.tripo3d_key_input.setPlaceholderText("Tripo3D API key (optional)")
        self.tripo3d_key_input.setText(
            os.getenv("TRIPO3D_API_KEY", "") or self.settings.value("tripo3d_api_key", "")
        )
        mesh_layout.addRow("Tripo3D API Key:", self.tripo3d_key_input)
        layout.addWidget(mesh_box)

        # --- Auto-Update: point at the manifest produced by build_update.py ---
        upd_box = QGroupBox("⬆️ Automatic Updates")
        upd_layout = QVBoxLayout(upd_box)
        upd_hint = QLabel(
            "Set this to the URL of the update manifest (latest_update.json). "
            "If you publish with 'build_update.py --push', that's your public "
            "releases repo's latest-release asset, e.g.\n"
            "https://github.com/bcatsky-maker/LRJK-Studio-Releases/releases/latest/download/latest_update.json\n"
            "The app checks it on launch and silently installs newer builds."
        )
        upd_hint.setWordWrap(True)
        upd_layout.addWidget(upd_hint)
        feed_row = QFormLayout()
        self.update_feed_input = QLineEdit()
        self.update_feed_input.setPlaceholderText(
            "https://github.com/<owner>/<releases-repo>/releases/latest/download/latest_update.json"
        )
        self.update_feed_input.setText(self.settings.value("update_feed_url", ""))
        feed_row.addRow("Update feed URL:", self.update_feed_input)
        upd_layout.addLayout(feed_row)
        self.auto_update_check = QCheckBox(
            "Install newer updates silently on launch (then restart)"
        )
        self.auto_update_check.setChecked(
            str(self.settings.value("auto_update", "true")).lower() in ("1", "true", "yes")
        )
        upd_layout.addWidget(self.auto_update_check)
        layout.addWidget(upd_box)

        btn_layout = QHBoxLayout()
        self.test_btn = QPushButton("Test Connection")
        self.test_btn.clicked.connect(self.test_connection)
        self.save_btn = QPushButton("Save & Close")
        self.save_btn.clicked.connect(self.save_and_close)

        btn_layout.addWidget(self.test_btn)
        btn_layout.addWidget(self.save_btn)
        layout.addLayout(btn_layout)

    def on_provider_changed(self, provider_text: str):
        if "Ollama" in provider_text:
            self.endpoint_input.setText("http://127.0.0.1:11434/v1")
            self.model_input.setText("llama3")
            self.api_key_input.clear()
        elif "OpenAI" in provider_text:
            self.endpoint_input.setText("https://api.openai.com/v1")
            self.model_input.setText("gpt-4o")
        elif "Claude" in provider_text:
            self.endpoint_input.setText("https://api.anthropic.com/v1")
            self.model_input.setText("claude-3-5-sonnet-20240620")

    def copy_token(self):
        QApplication.clipboard().setText(self.token_display.text())

    def regenerate_token(self):
        confirm = QMessageBox.question(
            self,
            "Regenerate Bridge Token",
            "This invalidates the current token. You'll need to paste the new one into "
            "Blender's LRJK AI Studio panel before it can talk to the desktop app again. Continue?",
        )
        if confirm != QMessageBox.Yes:
            return
        new_token = secrets.token_hex(16)
        self.settings.setValue("bridge_token", new_token)
        self.token_display.setText(new_token)
        BridgeHTTPRequestHandler.expected_token = new_token

    def test_connection(self):
        endpoint = self.endpoint_input.text().strip()
        if not endpoint:
            QMessageBox.warning(self, "Connection Error", "Please provide a valid API Endpoint.")
            return

        provider = self.provider_combo.currentText()
        api_key = self.api_key_input.text().strip()
        model = self.model_input.text().strip()

        self.test_btn.setEnabled(False)
        self.test_btn.setText("Testing...")
        QApplication.processEvents()
        try:
            ok, message = test_provider_connection(provider, endpoint, api_key, model)
        finally:
            self.test_btn.setEnabled(True)
            self.test_btn.setText("Test Connection")

        if ok:
            QMessageBox.information(self, "Connection Verified", message)
        else:
            QMessageBox.warning(
                self,
                "Connection Failed",
                f"{message}\n\nGeneration will still work using the local rule-based "
                "fallback if this isn't fixed - it just won't be AI-driven.",
            )

    def save_and_close(self):
        self.settings.setValue("api_provider", self.provider_combo.currentText())
        self.settings.setValue("api_endpoint", self.endpoint_input.text().strip())
        self.settings.setValue("api_key", self.api_key_input.text().strip())
        self.settings.setValue("api_model", self.model_input.text().strip())
        self.settings.setValue("bridge_port", self.bridge_port_input.text().strip())
        self.settings.setValue("tripo3d_api_key", self.tripo3d_key_input.text().strip())
        self.settings.setValue("update_feed_url", self.update_feed_input.text().strip())
        self.settings.setValue(
            "auto_update", "true" if self.auto_update_check.isChecked() else "false"
        )
        QMessageBox.information(self, "Settings Saved", "API settings updated successfully!")
        self.accept()


class VideoSplashScreen(QWidget):
    def __init__(self, video_path: Path, on_finished_callback):
        super().__init__()
        self.on_finished_callback = on_finished_callback

        self.setWindowFlags(Qt.SplashScreen | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.resize(800, 450)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.video_widget = QVideoWidget()
        layout.addWidget(self.video_widget)

        self.media_player = QMediaPlayer()
        self.media_player.setVideoOutput(self.video_widget)
        self.media_player.setSource(QUrl.fromLocalFile(str(video_path)))
        self.media_player.playbackStateChanged.connect(self.handle_state_changed)

        screen = QApplication.primaryScreen().geometry()
        self.move((screen.width() - self.width()) // 2, (screen.height() - self.height()) // 2)

    def start_playback(self):
        self.show()
        self.media_player.play()

    def handle_state_changed(self, state):
        if state == QMediaPlayer.StoppedState:
            self.finish_splash()

    def finish_splash(self):
        self.media_player.stop()
        self.close()
        if self.on_finished_callback:
            self.on_finished_callback()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LRJK Blender AI Studio")
        # A modest minimum so the window can be made small on a low-res screen;
        # the FlowLayout button rows wrap and the QScrollArea handles the rest,
        # so no control is ever clipped off the edge.
        self.setMinimumSize(560, 480)
        self.resize(1150, 850)
        self.settings = QSettings("RKOffisium", "LRJK_Blender_AI_Studio")
        self.db = ExecutionMemoryDB()

        # --- INITIALIZE STUDIO ASSET MANAGER & RUNTIME CACHE ---
        self.asset_manager = StudioAssetManager()
        self.runtime_cache_dir = self.asset_manager.project_root / "assets" / "runtime_cache"
        self.runtime_cache_dir.mkdir(parents=True, exist_ok=True)
        # --------------------------------------------------------

        # --- BRIDGE PAIRING TOKEN: generated once per install, required
        # on every POST to the local bridge server (see
        # BridgeHTTPRequestHandler). Paste this into the Blender add-on's
        # "Bridge Token" field (also viewable/regeneratable from AI Settings).
        bridge_token = self.settings.value("bridge_token", "")
        if not bridge_token:
            bridge_token = secrets.token_hex(16)
            self.settings.setValue("bridge_token", bridge_token)
        BridgeHTTPRequestHandler.expected_token = bridge_token
        # --------------------------------------------------------

        self.console_dialog = ConsoleLogDialog(self)

        # First-run: if this is an installed build that shipped a bundled
        # asset library, register it in the (writable) runtime DB so the AI
        # can import from it immediately - no ingestion step for the person
        # who received the installer. No-ops when running from source or
        # once already imported. Never allowed to block startup.
        try:
            from src.core.paths import get_bundled_asset_root, get_bundled_seed_db
            from src.core.seed_library import import_seed

            _seed_db = get_bundled_seed_db()
            _asset_root = get_bundled_asset_root()
            if _seed_db and _asset_root:
                _n = import_seed(self.asset_manager.db_path, _seed_db, _asset_root)
                if _n:
                    self.console_dialog.append_log(
                        f"📦 First-run setup: registered {_n} bundled library assets.", "info"
                    )
        except Exception as _seed_err:
            print(f"[WARN] Bundled library seeding skipped: {_seed_err}")

        icon_path = get_resource_path("assets/app_icon.png")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        central_widget = QWidget()
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(12, 10, 12, 12)
        main_layout.setSpacing(10)

        # 1. Banner
        banner_path = get_resource_path("assets/app_banner.png")
        if banner_path.exists():
            self.banner_label = AspectRatioLabel(banner_path)
            main_layout.addWidget(self.banner_label)

        # 2. Header Bar - a FlowLayout so the title + buttons WRAP onto more
        #    lines on a narrow window instead of the right-most buttons
        #    (⚙ AI Settings etc.) being clipped off the edge.
        header_bar = FlowLayout(hspacing=8, vspacing=6)
        header_title = QLabel("<h3>LRJK Blender AI Studio (RK Offisium)</h3>")

        self.import_db_btn = QPushButton("📥 Import Memory DB")
        self.import_db_btn.setStyleSheet("""
            QPushButton {
                font-weight: bold; padding: 6px 12px;
                background-color: #2e7d32; color: #ffffff;
                border: 1px solid #388e3c; border-radius: 4px;
            }
            QPushButton:hover { background-color: #388e3c; }
        """)
        self.import_db_btn.clicked.connect(self.import_database)

        self.export_db_btn = QPushButton("📤 Export Source & DB Package")
        self.export_db_btn.setStyleSheet("""
            QPushButton {
                font-weight: bold; padding: 6px 12px;
                background-color: #0e639c; color: #ffffff;
                border: 1px solid #1177bb; border-radius: 4px;
            }
            QPushButton:hover { background-color: #1177bb; }
        """)
        self.export_db_btn.clicked.connect(self.export_source_and_database)

        self.history_btn = QPushButton("💾 Saved Scripts")
        self.history_btn.setStyleSheet("padding: 6px 12px;")
        self.history_btn.clicked.connect(self.open_history_dialog)

        self.logs_btn = QPushButton("📊 Live Logs")
        self.logs_btn.setStyleSheet("""
            QPushButton {
                font-weight: bold; padding: 6px 12px;
                background-color: #1e1e1e; color: #4EC9B0;
                border: 1px solid #4EC9B0; border-radius: 4px;
            }
            QPushButton:hover { background-color: #2e2e2e; }
        """)
        self.logs_btn.clicked.connect(self.open_console_dialog)

        self.settings_btn = QPushButton("⚙️ AI Settings")
        self.settings_btn.setStyleSheet("""
            QPushButton {
                font-weight: bold; padding: 6px 12px;
                background-color: #2b2b2b; color: #ffffff;
                border: 1px solid #555555; border-radius: 4px;
            }
            QPushButton:hover { background-color: #3b3b3b; border-color: #007acc; }
        """)
        self.settings_btn.clicked.connect(self.open_settings_dialog)

        header_bar.addWidget(header_title)
        header_bar.addWidget(self.import_db_btn)
        header_bar.addWidget(self.export_db_btn)
        header_bar.addWidget(self.history_btn)
        header_bar.addWidget(self.logs_btn)
        header_bar.addWidget(self.settings_btn)
        main_layout.addLayout(header_bar)

        # 2b. Update banner - hidden until the background GitHub checker
        # (started at the end of __init__) actually finds a newer release.
        # The check itself is silent (never blocks/interrupts startup);
        # this is where a found update gets surfaced instead of only
        # living in the console log the user has to think to open.
        self.update_banner_label = QLabel()
        self.update_banner_label.setOpenExternalLinks(True)
        self.update_banner_label.setStyleSheet("""
            QLabel {
                background-color: #2b5d34; color: #ffffff;
                padding: 6px 10px; border-radius: 4px; font-weight: bold;
            }
            QLabel a { color: #ffffff; }
        """)
        self.update_banner_label.setVisible(False)
        main_layout.addWidget(self.update_banner_label)

        # 3. Local Knowledge & Extensions Engine
        rag_box = QGroupBox("📁 Smart Knowledge Ingestion (Addons, Extensions, Models & Engines)")
        rag_layout = FlowLayout(rag_box, hspacing=8, vspacing=6)
        rag_layout.setContentsMargins(10, 8, 10, 8)

        self.rag_status_label = QLabel(
            f"<b>Indexed Documents:</b> {self.db.get_indexed_count()} files"
        )
        self.upload_folder_btn = QPushButton("📂 Upload Folder (Auto-Classify & Absorb)")
        self.upload_folder_btn.clicked.connect(self.upload_manuals_folder)

        self.upload_files_btn = QPushButton("🧬 Ingest & Absorb Files / ZIPs")
        self.upload_files_btn.clicked.connect(self.upload_specific_files)

        rag_layout.addWidget(self.rag_status_label)
        rag_layout.addWidget(self.upload_folder_btn)
        rag_layout.addWidget(self.upload_files_btn)
        main_layout.addWidget(rag_box)

        # 4. BlendKit Web Import
        blendkit_box = QGroupBox("🔗 BlendKit Reference Import")
        blendkit_layout = FlowLayout(blendkit_box, hspacing=8, vspacing=6)
        blendkit_layout.setContentsMargins(10, 8, 10, 8)

        bk_label = QLabel("BlendKit Reference:")
        self.blendkit_input = QLineEdit()
        self.blendkit_input.setPlaceholderText("Paste BlendKit URL or asset_base_id string")
        # Give the field a real width in the wrapping row (its natural
        # sizeHint is only a few characters wide).
        self.blendkit_input.setMinimumWidth(340)

        self.attach_bk_btn = QPushButton("Attach Context")
        self.attach_bk_btn.clicked.connect(self.attach_blendkit_reference)

        self.clear_bk_btn = QPushButton("Clear Reference")
        self.clear_bk_btn.setToolTip(
            "Removes every attached BlendKit reference so it stops being "
            "suggested for future prompts."
        )
        self.clear_bk_btn.clicked.connect(self.clear_blendkit_reference)

        blendkit_layout.addWidget(bk_label)
        blendkit_layout.addWidget(self.blendkit_input)
        blendkit_layout.addWidget(self.attach_bk_btn)
        blendkit_layout.addWidget(self.clear_bk_btn)
        main_layout.addWidget(blendkit_box)

        # 5. Active Connection Status Card
        self.info_box = QGroupBox("📡 Active AI Service Configuration")
        self.info_layout = QVBoxLayout(self.info_box)
        self.info_layout.setContentsMargins(10, 8, 10, 8)
        self.active_status_label = QLabel()
        self.update_active_status_label()
        self.info_layout.addWidget(self.active_status_label)
        main_layout.addWidget(self.info_box)

        # 6. Performance Engine Box
        generator = AIGenerator()
        start_time = time.time()
        status_text = generator.generate_terrain(42, 512)
        execution_time = time.time() - start_time

        self.console_dialog.append_log(
            "Generator Engine Initialized: generate_terrain(seed=42, res=512)", "code"
        )
        self.console_dialog.append_log(
            f"Blender Output Execution Time: {execution_time:.4f} seconds", "metric"
        )

        status_box = QGroupBox("⚡ Core Generator Engine Performance")
        status_layout = QVBoxLayout(status_box)
        status_layout.setContentsMargins(10, 8, 10, 8)
        self.welcome_label = QLabel(
            f"<b>Output Status:</b> {status_text}<br><b>Blender Generation Speed:</b> {execution_time:.4f} seconds"
        )
        self.welcome_label.setStyleSheet("font-size: 12px;")
        status_layout.addWidget(self.welcome_label)
        main_layout.addWidget(status_box)

        scroll.setWidget(central_widget)
        self.setCentralWidget(scroll)

        SIGNAL_HUB.payload_received.connect(self.handle_incoming_blender_payload)

        port = int(self.settings.value("bridge_port", "8081"))
        self.bridge_worker = BridgeServerWorker(port=port)
        self.bridge_worker.start()

        # 7. GitHub Checker - silent (background thread, never blocks
        # startup or prompts anything); see notify_github_update for how a
        # found update actually gets surfaced.
        self.update_checker = GitHubUpdateCheckerWorker()
        self.update_checker.update_available.connect(self.notify_github_update)
        self.update_checker.start()

        # 8. Self-hosted silent auto-updater. Only runs if you've set an
        # "Update feed URL" in AI Settings (the manifest build_update.py
        # produces). Checks in the background and, when auto-update is on,
        # installs a newer version silently and relaunches.
        feed_url = self.settings.value("update_feed_url", "").strip()
        if feed_url:
            self.silent_updater = SilentUpdateWorker(feed_url)
            self.silent_updater.update_ready.connect(self.handle_update_ready)
            self.silent_updater.start()

    def fetch_cached_asset_path(self, asset_name: str, asset_type: str) -> Path:
        """
        Dynamically extracts a binary asset from the asset store (or a
        legacy database BLOB row) into the local runtime cache directory
        on-demand.
        """
        cached_file_path = self.runtime_cache_dir / f"{asset_name}.{asset_type}"

        if cached_file_path.exists():
            return cached_file_path

        success = self.asset_manager.load_asset_to_disk(asset_name, cached_file_path)
        if success:
            self.console_dialog.append_log(
                f"📦 Extracted asset to runtime cache: {asset_name}.{asset_type}", "info"
            )
            return cached_file_path

        self.console_dialog.append_log(f"⚠️ Failed to extract asset '{asset_name}'.", "error")
        raise FileNotFoundError(
            f"Asset '{asset_name}' could not be extracted from the asset store."
        )

    def notify_github_update(self, new_version: str, download_url: str):
        self.console_dialog.append_log(
            f"🔔 A new update (v{new_version}) is available on GitHub! Download: {download_url}",
            "info",
        )
        self.update_banner_label.setText(
            f"🔔 Update available: v{new_version} (you have v{get_app_version()}) - "
            f'<a href="{download_url}">View release on GitHub</a>'
        )
        self.update_banner_label.setVisible(True)

    def handle_update_ready(self, version: str, installer_url: str, sha256: str):
        """A newer version was found on the self-hosted update feed."""
        self.console_dialog.append_log(
            f"⬆️ Update v{version} available on your update feed (you have v{get_app_version()}).",
            "info",
        )
        auto = str(self.settings.value("auto_update", "true")).lower() in ("1", "true", "yes")
        if not auto:
            self.update_banner_label.setText(
                f"⬆️ Update available: v{version}. Enable silent auto-update in AI Settings, "
                f"or reinstall from your update feed."
            )
            self.update_banner_label.setVisible(True)
            return

        self.console_dialog.append_log(
            f"⬇️ Downloading update v{version} in the background...", "info"
        )
        self._update_downloader = _UpdateDownloadWorker(installer_url, sha256)
        self._update_downloader.done.connect(self._launch_update)
        self._update_downloader.failed.connect(
            lambda m: self.console_dialog.append_log(f"⚠️ Update download failed ({m}).", "error")
        )
        self._update_downloader.start()

    def _launch_update(self, installer_path: str):
        """Run the downloaded update installer silently, then quit so it can
        replace the app's files and relaunch it."""
        try:
            subprocess.Popen(
                [installer_path, "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"],
                close_fds=False,
            )
            self.console_dialog.append_log(
                "⬆️ Installing update - the app will restart shortly.", "info"
            )
            QApplication.quit()
        except Exception as e:
            self.console_dialog.append_log(
                f"⚠️ Could not launch the update installer: {e}", "error"
            )

    def classify_and_absorb_file(self, file_path: str):
        path = Path(file_path)
        ext = path.suffix.lower().replace(".", "")

        if ext == "zip":
            temp_check_dir = Path(tempfile.gettempdir()) / "lrjk_zip_inspect" / path.stem
            if temp_check_dir.exists():
                shutil.rmtree(temp_check_dir)
            temp_check_dir.mkdir(parents=True, exist_ok=True)

            try:
                with zipfile.ZipFile(file_path, "r") as zip_ref:
                    zip_ref.extractall(temp_check_dir)

                is_addon = False
                for root, _, files in os.walk(temp_check_dir):
                    for file in files:
                        if file.endswith(".py"):
                            with open(
                                os.path.join(root, file), encoding="utf-8", errors="ignore"
                            ) as f:
                                if "bl_info" in f.read():
                                    is_addon = True
                                    break

                if is_addon:
                    self.absorb_addon_into_source(file_path)
                else:
                    for root, _, files in os.walk(temp_check_dir):
                        for file in files:
                            sub_ext = file.lower().split(".")[-1]
                            if sub_ext in ["blend", "obj", "fbx", "stl", "target", "uproject"]:
                                sub_path = os.path.join(root, file)
                                self.db.index_reference_file(sub_path, sub_ext)
                    self.console_dialog.append_log(
                        f"📦 Unpacked and indexed model/engine ZIP: {path.name}", "info"
                    )

            except Exception as e:
                self.console_dialog.append_log(
                    f"⚠️ Error reading ZIP file {path.name}: {e}", "error"
                )

        elif ext == "py":
            try:
                with open(file_path, encoding="utf-8", errors="ignore") as f:
                    if "bl_info" in f.read():
                        target_dir = (
                            Path(__file__).parent.parent.parent
                            / "src"
                            / "core"
                            / "absorbed_addons"
                            / path.stem
                        )
                        target_dir.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(file_path, target_dir / path.name)
                        self.db.index_reference_file(str(target_dir / path.name), "addon_py")
                        self.console_dialog.append_log(
                            f"🧬 Absorbed standalone add-on python module: {path.name}", "info"
                        )
                    else:
                        self.db.index_reference_file(file_path, "py")
            except Exception as e:
                self.console_dialog.append_log(f"Error inspecting script {path.name}: {e}", "error")

        elif ext in ["blend", "obj", "fbx", "stl", "target", "gltf", "glb"]:
            self.db.index_reference_file(file_path, f"3d_model_{ext}")
            self.console_dialog.append_log(
                f"🎨 Classified & indexed 3D model asset: {path.name}", "info"
            )

        elif ext in ["uproject", "unitypackage", "godot"] or "ProjectSettings" in file_path:
            self.db.index_reference_file(file_path, f"game_engine_{ext}")
            self.console_dialog.append_log(
                f"🎮 Classified & indexed Game Engine Project file: {path.name}", "info"
            )

        elif ext in ["txt", "md", "json", "pdf"]:
            self.db.index_reference_file(file_path, f"doc_{ext}")

    def absorb_addon_into_source(self, zip_path: str):
        project_root = Path(__file__).parent.parent.parent
        addons_core_dir = project_root / "src" / "core" / "absorbed_addons" / Path(zip_path).stem
        addons_core_dir.mkdir(parents=True, exist_ok=True)

        try:
            with zipfile.ZipFile(zip_path, "r") as zip_ref:
                zip_ref.extractall(addons_core_dir)

            init_file = addons_core_dir / "__init__.py"
            if not init_file.exists():
                with open(init_file, "w", encoding="utf-8") as f:
                    f.write("# Auto-generated package init for absorbed addon\n")

            for root, _, files in os.walk(addons_core_dir):
                for file in files:
                    ext = file.lower().split(".")[-1]
                    file_path = os.path.join(root, file)
                    self.db.index_reference_file(file_path, ext)

            self.console_dialog.append_log(
                f"🧬 Absorbed add-on '{Path(zip_path).stem}' directly into app core source tree: {addons_core_dir}",
                "info",
            )

        except Exception as e:
            self.console_dialog.append_log(
                f"❌ Failed absorbing addon into source tree: {e}", "error"
            )

    def attach_blendkit_reference(self):
        raw_text = self.blendkit_input.text().strip()
        if not raw_text:
            QMessageBox.warning(
                self, "Input Error", "Please paste a BlendKit URL or Asset ID string."
            )
            return

        attached_refs = self.settings.value("attached_references", [])
        if isinstance(attached_refs, str):
            attached_refs = [attached_refs]

        if raw_text in attached_refs:
            QMessageBox.warning(
                self, "Duplicate Reference", "The reference is already in the database!"
            )
            self.console_dialog.append_log(
                f"⚠️ Reference already exists in memory DB: {raw_text}", "error"
            )
            self.blendkit_input.clear()
            return

        attached_refs.append(raw_text)
        self.settings.setValue("attached_references", attached_refs)
        self.settings.setValue("last_blendkit_ref", raw_text)

        if "asset_base_id:" in raw_text:
            asset_id_match = re.search(r"asset_base_id:([a-f0-9\-]+)", raw_text)
            type_match = re.search(r"asset_type:([a-zA-Z0-9]+)", raw_text)

            asset_id = asset_id_match.group(1) if asset_id_match else "Unknown"
            asset_type = type_match.group(1) if type_match else "model"

            # Also register it in the searchable reference index so
            # search_reference() can surface it for matching prompts.
            self.db.index_reference_file(
                f"blendkit_asset_id:{asset_id} asset_type:{asset_type}",
                f"blendkit_{asset_type}",
                source=f"Attached BlendKit Reference: {raw_text}",
            )

            self.console_dialog.append_log(
                f"🔗 Attached BlendKit Asset ID: {asset_id} (Type: {asset_type}) to RAG memory",
                "info",
            )
            QMessageBox.information(
                self,
                "BlendKit Attached",
                f"Successfully attached BlendKit Asset ID:\n{asset_id}\nType: {asset_type}",
            )
        else:
            self.console_dialog.append_log(f"🔗 Attached BlendKit Web URL: {raw_text}", "info")
            QMessageBox.information(
                self, "BlendKit Attached", "Successfully attached BlendKit reference URL!"
            )

        self.blendkit_input.clear()

    def clear_blendkit_reference(self):
        """
        Forgets every attached BlendKit reference - both the QSettings
        list attach_blendkit_reference() maintains and the searchable
        rows it wrote into reference_index (see
        ExecutionMemoryDB.clear_blendkit_references()). Without this,
        an old attached reference had no way to stop being suggested for
        new prompts short of attaching a different one over it.
        """
        self.blendkit_input.clear()
        self.settings.remove("attached_references")
        self.settings.remove("last_blendkit_ref")
        removed = self.db.clear_blendkit_references()

        self.rag_status_label.setText(
            f"<b>Indexed Documents:</b> {self.db.get_indexed_count()} files"
        )

        if removed:
            self.console_dialog.append_log(
                f"🧹 Cleared {removed} attached BlendKit reference(s) from memory.", "info"
            )
            QMessageBox.information(
                self, "Reference Cleared", f"Removed {removed} attached BlendKit reference(s)."
            )
        else:
            self.console_dialog.append_log("🧹 No attached BlendKit references to clear.", "info")

    def import_database(self):
        source_file, _ = QFileDialog.getOpenFileName(
            self, "Select Memory Database to Import", "", "SQLite Database (*.db)"
        )

        if source_file:
            try:
                shutil.copy2(source_file, self.db.db_path)
                self.db = ExecutionMemoryDB(db_path=self.db.db_path)
                self.rag_status_label.setText(
                    f"<b>Indexed Documents:</b> {self.db.get_indexed_count()} files"
                )

                self.console_dialog.append_log(
                    f"📥 Successfully imported memory database from: {source_file}", "info"
                )
                QMessageBox.information(
                    self,
                    "Database Imported",
                    f"Successfully imported memory database from:\n{source_file}\n\nAll indexed knowledge and history are now active!",
                )
            except Exception as e:
                QMessageBox.critical(self, "Import Error", f"Failed to import database: {e}")

    def export_source_and_database(self):
        project_root = Path(__file__).parent.parent.parent
        output_dir = project_root / "dist" / "next_build_package"
        output_dir.mkdir(parents=True, exist_ok=True)

        db_source = self.db.db_path
        addons_source = project_root / "src" / "core" / "absorbed_addons"

        try:
            if db_source.exists():
                shutil.copy2(db_source, output_dir / "studio_memory.db")

            if addons_source.exists():
                addons_output = output_dir / "absorbed_addons"
                if addons_output.exists():
                    shutil.rmtree(addons_output)
                shutil.copytree(addons_source, addons_output)

            self.console_dialog.append_log(
                f"📤 Exported source code & database memory to: {output_dir}", "info"
            )
            QMessageBox.information(
                self,
                "Source & Database Exported",
                f"Successfully exported build package to:\n{output_dir}\n\n"
                "Contains studio_memory.db and absorbed add-on python source code ready for your next installer build!",
            )
        except Exception as e:
            QMessageBox.critical(self, "Export Error", f"Failed to export build package: {e}")

    def parse_colors_from_prompt(self, prompt: str):
        prompt_lower = prompt.lower()
        color_map = {
            "blue": (0.1, 0.4, 0.9, 1.0),
            "green": (0.1, 0.8, 0.2, 1.0),
            "red": (0.9, 0.1, 0.1, 1.0),
            "purple": (0.6, 0.1, 0.8, 1.0),
            "yellow": (0.95, 0.85, 0.1, 1.0),
            "orange": (0.95, 0.45, 0.1, 1.0),
            "cyan": (0.1, 0.9, 0.9, 1.0),
            "pink": (0.95, 0.2, 0.6, 1.0),
            "white": (0.9, 0.9, 0.9, 1.0),
            "black": (0.05, 0.05, 0.05, 1.0),
            "golden": (0.85, 0.65, 0.15, 1.0),
            "gold": (0.85, 0.65, 0.15, 1.0),
        }

        found_colors = []
        for name, rgba in color_map.items():
            if name in prompt_lower:
                found_colors.append(rgba)

        primary_color = found_colors[0] if found_colors else (0.1, 0.4, 0.9, 1.0)
        secondary_color = found_colors[1] if len(found_colors) > 1 else (0.8, 0.5, 0.2, 1.0)
        return primary_color, secondary_color

    def upload_manuals_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select Folder to Ingest (Addons, Extensions, Models or Engines)"
        )
        if folder:
            count = 0
            for root, _, files in os.walk(folder):
                for file in files:
                    file_path = os.path.join(root, file)
                    self.classify_and_absorb_file(file_path)
                    count += 1

            self.rag_status_label.setText(
                f"<b>Indexed Documents:</b> {self.db.get_indexed_count()} files"
            )
            self.console_dialog.append_log(
                f"📂 Intelligently scanned & absorbed {count} files from '{folder}'", "info"
            )
            QMessageBox.information(
                self,
                "Smart Ingestion Complete",
                f"Successfully analyzed and absorbed {count} files!",
            )

    def upload_specific_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Select Files or Addon ZIPs to Absorb",
            "",
            "All Supported Files (*.zip *.blend *.obj *.fbx *.py *.uproject *.txt *.md)",
        )
        if files:
            for file_path in files:
                self.classify_and_absorb_file(file_path)

            self.rag_status_label.setText(
                f"<b>Indexed Documents:</b> {self.db.get_indexed_count()} files"
            )
            self.console_dialog.append_log(
                f"📄 Processed {len(files)} uploaded items into core memory.", "info"
            )
            QMessageBox.information(
                self, "Ingestion Complete", f"Successfully processed {len(files)} selected files!"
            )

    def _scene_frame_actions(self):
        """Ground plane + area light + camera - the common 'stage' every
        rule-based scene sits on, so a render is never black."""
        return [
            {
                "action": "add_primitive",
                "params": {
                    "shape": "plane",
                    "name": "AI_Ground",
                    "size": 20.0,
                    "location": [0, 0, -1.2],
                },
            },
            {
                "action": "add_material",
                "params": {"base_color": [0.25, 0.25, 0.28, 1.0], "roughness": 0.9},
            },
            {
                "action": "add_light",
                "params": {"light_type": "AREA", "location": [4, -4, 6], "energy": 900},
            },
            {"action": "set_camera", "params": {"location": [6, -6, 4.5], "look_at": [0, 0, 0]}},
        ]

    def _donut_actions(self, prompt_lower, icing_color):
        """
        A realistic donut, composed from the same whitelisted primitives the
        AI uses:
          - Dough: a fat torus in a light fried-tan (the exposed outer band
            reads lighter, as real donuts do), roughened by a low-strength
            Displace so it isn't perfectly round - real dough has bumps/dents.
          - Icing: a THIN coat torus (small tube radius) that hugs the top and
            drips down the sides rather than a thick second ring, glossy, also
            displaced for an irregular dripping edge.
          - Sprinkles: many small cylinders scattered across the whole top,
            lying flat on the icing (not poking up).
        """
        import math

        dough = [0.72, 0.5, 0.3, 1.0]  # light fried-dough tan
        sprinkle_colors = [
            [0.95, 0.15, 0.15, 1.0],
            [0.98, 0.85, 0.10, 1.0],
            [0.15, 0.75, 0.25, 1.0],
            [0.15, 0.55, 0.95, 1.0],
            [0.95, 0.35, 0.80, 1.0],
            [0.98, 0.98, 0.98, 1.0],
            [0.55, 0.2, 0.85, 1.0],
            [0.98, 0.55, 0.1, 1.0],
        ]
        actions = [
            {
                "action": "set_world_background",
                "params": {"color": [0.06, 0.06, 0.07], "strength": 1.0},
            },
            # --- Dough: fat tube, smoothed, then bumped so it isn't a perfect ring.
            {
                "action": "add_primitive",
                "params": {
                    "shape": "torus",
                    "name": "AI_Subject",
                    "major_radius": 1.0,
                    "minor_radius": 0.46,
                },
            },
            {
                "action": "apply_modifier",
                "params": {"target": "AI_Subject", "modifier": "SUBSURF", "levels": 3},
            },
            {
                "action": "apply_modifier",
                "params": {"target": "AI_Subject", "modifier": "DISPLACE", "strength": 0.06},
            },
            {
                "action": "add_material",
                "params": {"target": "AI_Subject", "base_color": dough, "roughness": 0.65},
            },
            # --- Icing: thin coat hugging the top, lifted a touch and squashed so it
            #     drips down the outer/inner sides while the tan dough shows below.
            {
                "action": "add_primitive",
                "params": {
                    "shape": "torus",
                    "name": "AI_Icing",
                    "major_radius": 1.0,
                    "minor_radius": 0.5,
                    "location": [0, 0, 0.16],
                    "scale": [1, 1, 0.8],
                },
            },
            {
                "action": "apply_modifier",
                "params": {"target": "AI_Icing", "modifier": "SUBSURF", "levels": 3},
            },
            {
                "action": "apply_modifier",
                "params": {"target": "AI_Icing", "modifier": "DISPLACE", "strength": 0.07},
            },
            {
                "action": "add_material",
                "params": {"target": "AI_Icing", "base_color": icing_color, "roughness": 0.2},
            },
        ]
        # --- Sprinkles: phyllotaxis scatter across the whole top of the icing,
        #     laid flat (long axis horizontal), varied radius/tilt/color.
        golden = 2.399963229728653
        n = 24
        for i in range(n):
            ang = golden * i
            r = 0.72 + (i % 6) * 0.09  # spread across the ring band 0.72..1.17
            x = round(r * math.cos(ang), 3)
            y = round(r * math.sin(ang), 3)
            z = round(0.52 - 0.5 * abs(r - 1.0), 3)  # follow the domed icing top
            col = sprinkle_colors[i % len(sprinkle_colors)]
            name = f"AI_Sprinkle_{i}"
            actions.append(
                {
                    "action": "add_primitive",
                    "params": {
                        "shape": "cylinder",
                        "name": name,
                        "size": 0.14,
                        "location": [x, y, z],
                        # lie flat: tip the cylinder onto its side (X +90deg) then spin it
                        "rotation": [
                            math.pi / 2 + 0.25 * math.sin(i * 1.7),
                            0.2 * math.cos(i * 1.1),
                            ang,
                        ],
                        "scale": [0.22, 0.22, 1.0],
                    },
                }
            )
            actions.append(
                {"action": "add_material", "params": {"target": name, "base_color": col}}
            )
        actions.extend(self._scene_frame_actions())
        return actions

    @staticmethod
    def _wants_precise_layout(prompt: str) -> bool:
        """
        True when the prompt is a MEASURED layout request - a road/street/etc.
        that specifies real dimensions or spacings ("1 km", "every 300 m",
        "trees every 10 m", "sidewalks both sides"). These are built
        deterministically instead of by the AI: a language model will not
        reliably emit 200 exactly-spaced objects, but the rule-based builder
        reproduces the exact numbers the user asked for. Creative,
        unmeasured prompts ("a winding country road at sunset") still go to
        the AI.
        """
        p = (prompt or "").lower()
        has_layout_kw = any(
            k in p for k in ["road", "street", "highway", "avenue", "lane", "boulevard"]
        )
        if not has_layout_kw:
            return False
        has_measure = bool(
            re.search(r"\d+\s*(km|kilomet|m\b|meter|metre)", p)
            or re.search(r"every\s+\d+", p)
            or "sidewalk" in p
            or "pavement" in p
            or re.search(r"trees?\s+every", p)
        )
        return has_measure

    @staticmethod
    def _parse_length_m(text, default_m):
        """Pull a length in metres out of free text: '1 km' -> 1000, '750m' -> 750."""
        m = re.search(r"(\d+(?:\.\d+)?)\s*(km|kilometre|kilometer)", text)
        if m:
            return float(m.group(1)) * 1000.0
        m = re.search(r"(\d+(?:\.\d+)?)\s*(m\b|metre|meter)", text)
        if m:
            return float(m.group(1))
        return float(default_m)

    def _road_actions(self, prompt):
        """
        Deterministic builder for a road / street scene, so a prompt like
        'build a 1 km road with roads exiting left every 300m, sidewalks both
        sides and trees every 10m' produces a REAL multi-object scene even
        with no AI provider (the case that used to collapse into a single
        failed BlendKit import). Everything is built from the same whitelisted
        primitives the AI uses; trees are instanced with duplicate_object so a
        1 km row is a handful of KB, not thousands of primitives.

        Geometry: the main road runs along +Y, centred on the origin. 'Left'
        (as you look down the road, +Y) is -X. Lengths parsed from the prompt;
        spacings parsed from the two 'every N m' phrases.
        """
        p = prompt.lower()

        length = self._parse_length_m(p, 1000.0)
        length = max(40.0, min(length, 4000.0))  # keep it sane

        # Branch spacing: the 'every N m' tied to roads/exits/left.
        branch_spacing = 300.0
        bm = (
            re.search(r"(?:road|exit|split|turn|junction)[^.]*?every\s+(\d+)\s*m", p)
            or re.search(r"left[^.]*?every\s+(\d+)\s*m", p)
            or re.search(r"every\s+(\d+)\s*m[^.]*?(?:road|left|exit)", p)
        )
        if bm:
            branch_spacing = max(20.0, float(bm.group(1)))

        # Tree spacing: the 'every N m' tied to trees.
        tree_spacing = 10.0
        tm = re.search(r"tree[s]?\s+every\s+(\d+)\s*m", p) or re.search(
            r"every\s+(\d+)\s*m[^.]*?tree", p
        )
        if tm:
            tree_spacing = max(3.0, float(tm.group(1)))

        want_sidewalks = "sidewalk" in p or "pavement" in p or "footpath" in p
        want_trees = "tree" in p
        branch_left = "left" in p or "right" not in p  # default to left
        branch_dir = -1.0 if branch_left else 1.0

        ROAD_W, SW_W = 8.0, 2.0
        half = length / 2.0
        asphalt = [0.09, 0.09, 0.10, 1.0]
        grass = [0.16, 0.42, 0.14, 1.0]
        concrete = [0.62, 0.62, 0.60, 1.0]
        foliage = [0.13, 0.45, 0.16, 1.0]
        bark = [0.32, 0.20, 0.11, 1.0]
        line_paint = [0.92, 0.92, 0.90, 1.0]  # road markings (near-white)

        actions = [
            {
                "action": "set_world_background",
                "params": {"color": [0.45, 0.62, 0.85], "strength": 1.0},
            },
        ]

        # Ground (big grass plane). size is capped at 100 in the add-on, so
        # scale it up instead to cover the whole road.
        ground_scale = max(length * 0.6, 120.0)
        actions += [
            {
                "action": "add_primitive",
                "params": {
                    "shape": "plane",
                    "name": "AI_Ground",
                    "size": 2.0,
                    "location": [0, 0, -0.05],
                    "scale": [ground_scale, ground_scale, 1.0],
                },
            },
            {
                "action": "add_material",
                "params": {"target": "AI_Ground", "base_color": grass, "roughness": 0.95},
            },
        ]

        # Main road. If the asset library has a real road MODEL, import one
        # segment and let the add-on tile it end-to-end to the full length
        # (dimension-agnostic, keeps the model's own texture + lane lines
        # crisp - no stretching). Otherwise lay a procedural asphalt strip and
        # paint our own lane markings further down.
        used_real_road = self._library_has_asset("road")
        if used_real_road:
            actions.append(
                {
                    "action": "import_asset_from_library",
                    "params": {
                        "query": "road",
                        "name": "AI_RoadSeg",
                        "location": [0, 0, 0.0],
                        "tile_length": length,
                        "tile_axis": "y",
                    },
                }
            )
        else:
            actions += [
                {
                    "action": "add_primitive",
                    "params": {
                        "shape": "cube",
                        "name": "AI_Road",
                        "size": 2.0,
                        "location": [0, 0, 0.05],
                        "scale": [ROAD_W / 2.0, half, 0.1],
                    },
                },
                {
                    "action": "add_material",
                    "params": {"target": "AI_Road", "base_color": asphalt, "roughness": 0.8},
                },
            ]

        # Sidewalks on both sides.
        if want_sidewalks:
            sw_x = ROAD_W / 2.0 + SW_W / 2.0
            for side, sx in (("L", -sw_x), ("R", sw_x)):
                nm = f"AI_Sidewalk_{side}"
                actions += [
                    {
                        "action": "add_primitive",
                        "params": {
                            "shape": "cube",
                            "name": nm,
                            "size": 2.0,
                            "location": [sx, 0, 0.08],
                            "scale": [SW_W / 2.0, half, 0.12],
                        },
                    },
                    {
                        "action": "add_material",
                        "params": {"target": nm, "base_color": concrete, "roughness": 0.9},
                    },
                ]

        # Branch roads exiting to the side every branch_spacing.
        branch_len = 60.0
        n_branches = 0
        y = -half + branch_spacing
        idx = 0
        while y < half - 1.0 and n_branches < 30:
            bx = branch_dir * (ROAD_W / 2.0 + branch_len / 2.0)
            nm = f"AI_Branch_{idx}"
            actions += [
                {
                    "action": "add_primitive",
                    "params": {
                        "shape": "cube",
                        "name": nm,
                        "size": 2.0,
                        "location": [bx, round(y, 2), 0.05],
                        "scale": [branch_len / 2.0, ROAD_W / 2.0, 0.1],
                    },
                },
                {
                    "action": "add_material",
                    "params": {"target": nm, "base_color": asphalt, "roughness": 0.8},
                },
            ]
            n_branches += 1
            idx += 1
            y += branch_spacing

        # Lane markings ON the procedural road surface (just proud of it to
        # avoid z-fighting): two solid white edge lines + a dashed white centre
        # line, dashes instanced with duplicate_object. Skipped entirely for a
        # real imported road model, which carries its own painted markings -
        # our own would z-fight and double up.
        n_dashes = 0
        if not used_real_road:
            MARK_Z = 0.18  # road top sits at ~0.15; markings ride just above it
            edge_off = ROAD_W / 2.0 - 0.45
            for side, ex in (("L", -edge_off), ("R", edge_off)):
                nm = f"AI_EdgeLine_{side}"
                actions += [
                    {
                        "action": "add_primitive",
                        "params": {
                            "shape": "cube",
                            "name": nm,
                            "size": 2.0,
                            "location": [ex, 0, MARK_Z],
                            "scale": [0.09, half, 0.015],
                        },
                    },
                    {
                        "action": "add_material",
                        "params": {"target": nm, "base_color": line_paint, "roughness": 0.5},
                    },
                ]
            # Dashed centre line: a 3 m dash every 12 m.
            dash_spacing = 12.0
            n_dashes = min(int(length // dash_spacing) + 1, 150)
            dash0 = "AI_CenterDash_0"
            actions += [
                {
                    "action": "add_primitive",
                    "params": {
                        "shape": "cube",
                        "name": dash0,
                        "size": 2.0,
                        "location": [0, round(-half, 2), MARK_Z],
                        "scale": [0.11, 1.5, 0.015],
                    },
                },
                {
                    "action": "add_material",
                    "params": {"target": dash0, "base_color": line_paint, "roughness": 0.5},
                },
            ]
            for i in range(1, n_dashes):
                actions.append(
                    {
                        "action": "duplicate_object",
                        "params": {
                            "target": dash0,
                            "name": f"AI_CenterDash_{i}",
                            "offset": [0, round(i * dash_spacing, 2), 0],
                        },
                    }
                )

        # Trees every tree_spacing next to each sidewalk. If the ingested asset
        # library actually has a tree MODEL, we import one real tree per side
        # and instance it down the row (duplicate_object linked=True → the
        # copies share the mesh, so hundreds of real trees stay light).
        # Otherwise we fall back to a built-from-primitives tree: a brown trunk
        # (cylinder) + a green canopy (sphere), both instanced down the row.
        n_trees = 0
        used_real_trees = False
        if want_trees:
            tree_x = ROAD_W / 2.0 + SW_W + 1.0
            count = int(length // tree_spacing) + 1
            MAX_PER_SIDE = 150
            capped = count > MAX_PER_SIDE
            if capped:
                count = MAX_PER_SIDE
            used_real_trees = self._library_has_asset("tree")

            for side, tx in (("L", -tree_x), ("R", tree_x)):
                base = f"AI_Tree{side}_0"  # trunk / whole-tree keeps AI_Tree{L,R}_
                y0 = round(-half, 2)
                if used_real_trees:
                    # One real imported tree per side, placed at the row start;
                    # the resolver turns this into a concrete import_mesh_file.
                    actions.append(
                        {
                            "action": "import_asset_from_library",
                            "params": {"query": "tree", "name": base, "location": [tx, y0, 0.0]},
                        }
                    )
                    n_trees += 1
                    for i in range(1, count):
                        actions.append(
                            {
                                "action": "duplicate_object",
                                "params": {
                                    "target": base,
                                    "name": f"AI_Tree{side}_{i}",
                                    "offset": [0, round(i * tree_spacing, 2), 0],
                                    "linked": True,  # share the tree mesh - cheap instances
                                },
                            }
                        )
                        n_trees += 1
                else:
                    canopy0 = f"AI_Tree{side}_0c"  # canopy shares the prefix (+ 'c')
                    actions += [
                        {
                            "action": "add_primitive",
                            "params": {
                                "shape": "cylinder",
                                "name": base,
                                "size": 2.0,
                                "location": [tx, y0, 1.1],
                                "scale": [0.12, 0.12, 1.1],
                            },
                        },
                        {
                            "action": "add_material",
                            "params": {"target": base, "base_color": bark, "roughness": 0.9},
                        },
                        {
                            "action": "add_primitive",
                            "params": {
                                "shape": "sphere",
                                "name": canopy0,
                                "size": 2.6,
                                "location": [tx, y0, 3.1],
                                "scale": [1.0, 1.0, 1.15],
                            },
                        },
                        {
                            "action": "add_material",
                            "params": {"target": canopy0, "base_color": foliage, "roughness": 0.85},
                        },
                    ]
                    n_trees += 1
                    for i in range(1, count):
                        off = [0, round(i * tree_spacing, 2), 0]
                        actions += [
                            {
                                "action": "duplicate_object",
                                "params": {
                                    "target": base,
                                    "name": f"AI_Tree{side}_{i}",
                                    "offset": off,
                                },
                            },
                            {
                                "action": "duplicate_object",
                                "params": {
                                    "target": canopy0,
                                    "name": f"AI_Tree{side}_{i}c",
                                    "offset": off,
                                },
                            },
                        ]
                        n_trees += 1
            if capped:
                self.console_dialog.append_log(
                    f"🌳 Tree row capped at {MAX_PER_SIDE}/side (prompt implied more); "
                    f"spacing kept at {tree_spacing:.0f} m.",
                    "info",
                )

        # Sun + a camera looking down the road.
        actions += [
            {
                "action": "add_light",
                "params": {"light_type": "SUN", "location": [30, -half, 60], "energy": 4.0},
            },
            {
                "action": "set_camera",
                "params": {
                    "location": [22, round(-half - 25, 2), 16],
                    "look_at": [0, round(-half + 120, 2), 0],
                },
            },
        ]

        tree_kind = "real library models" if used_real_trees else "trunk + canopy"
        road_kind = (
            "real library model tiled to length"
            if used_real_road
            else f"procedural strip + lane markings (edge lines + {n_dashes} centre dashes)"
        )
        self.console_dialog.append_log(
            f"🛣️ Road builder: {length:.0f} m road ({road_kind}), "
            f"{n_branches} branch road(s) every {branch_spacing:.0f} m, "
            f"{'sidewalks, ' if want_sidewalks else ''}"
            f"{n_trees} trees ({tree_kind}) every {tree_spacing:.0f} m.",
            "metric",
        )
        return validate_scene_program({"actions": actions})

    def _library_has_asset(self, query: str) -> bool:
        """True if the ingested asset library has an importable model matching
        `query` (e.g. a real tree). Best-effort: any failure (no manager, empty
        library) just returns False so the caller falls back to primitives."""
        try:
            return bool(self.asset_manager.search_assets(query))
        except Exception:
            return False

    def _rule_based_program(self, prompt: str):
        """
        Deterministic multi-action fallback used when no AI provider is
        configured or the provider call fails. This is intentionally far
        richer than the old single-action fallback (which only ever made a
        grid or a sphere): it builds an actual little lit, framed scene from
        keywords in the prompt so a first-run user with no API key still
        sees a real result, not a grey primitive in the dark.
        """
        prompt_lower = prompt.lower()
        primary, _secondary = self.parse_colors_from_prompt(prompt)
        primary = list(primary)

        # Roads / streets get a dedicated multi-object layout builder.
        if any(k in prompt_lower for k in ["road", "street", "highway", "avenue", "lane"]):
            return self._road_actions(prompt)

        # Recognizable composed subjects (multi-part) get their own builder.
        if any(k in prompt_lower for k in ["donut", "doughnut"]):
            # Icing takes the prompt's color if one was named, else pink.
            has_color = primary != [0.1, 0.4, 0.9, 1.0]  # parse default = "no color found"
            icing = primary if has_color else [0.95, 0.45, 0.7, 1.0]
            return validate_scene_program({"actions": self._donut_actions(prompt_lower, icing)})

        # Pick a subject shape from keywords.
        shape_map = {
            "donut": "torus",
            "ring": "torus",
            "tire": "torus",
            "torus": "torus",
            "ball": "sphere",
            "sphere": "sphere",
            "planet": "sphere",
            "orb": "sphere",
            "can": "cylinder",
            "mug": "cylinder",
            "cup": "cylinder",
            "cylinder": "cylinder",
            "pyramid": "cone",
            "cone": "cone",
            "tree": "cone",
            "floor": "plane",
            "ground": "plane",
            "plane": "plane",
            "box": "cube",
            "cube": "cube",
            "crate": "cube",
            "building": "cube",
            "head": "monkey",
            "character": "monkey",
            "monkey": "monkey",
            "face": "monkey",
        }
        shape = "cube"
        for kw, sh in shape_map.items():
            if kw in prompt_lower:
                shape = sh
                break

        metallic = (
            1.0
            if any(
                k in prompt_lower for k in ["gold", "golden", "metal", "silver", "chrome", "steel"]
            )
            else 0.0
        )
        emissive = any(
            k in prompt_lower for k in ["glow", "neon", "glowing", "emissive", "light-up", "lava"]
        )

        material_params = {
            "base_color": primary,
            "metallic": metallic,
            "roughness": 0.3 if metallic else 0.5,
        }
        if emissive:
            material_params["emission_color"] = primary
            material_params["emission_strength"] = 5.0

        candidate = {
            "actions": [
                {
                    "action": "set_world_background",
                    "params": {"color": [0.05, 0.05, 0.06], "strength": 1.0},
                },
                {
                    "action": "add_primitive",
                    "params": {"shape": shape, "name": "AI_Subject", "size": 2.0},
                },
                {"action": "add_material", "params": material_params},
                {
                    "action": "add_primitive",
                    "params": {
                        "shape": "plane",
                        "name": "AI_Ground",
                        "size": 20.0,
                        "location": [0, 0, -1.2],
                    },
                },
                {
                    "action": "add_material",
                    "params": {"base_color": [0.25, 0.25, 0.28, 1.0], "roughness": 0.9},
                },
                {
                    "action": "add_light",
                    "params": {"light_type": "AREA", "location": [4, -4, 6], "energy": 800},
                },
                {"action": "set_camera", "params": {"location": [7, -7, 5], "look_at": [0, 0, 0]}},
            ]
        }
        # Smooth round shapes.
        if shape in ("sphere", "torus", "cone", "cylinder"):
            candidate["actions"].insert(
                3,
                {
                    "action": "apply_modifier",
                    "params": {"target": "AI_Subject", "modifier": "SUBSURF", "levels": 2},
                },
            )
        return validate_scene_program(candidate)

    def _matched_blendkit_action(self, prompt: str):
        """
        If the user has attached a BlendKit reference that matches this prompt,
        return a single validated import_blendkit_asset action to APPEND to the
        scene (augment, never replace). Returns None when there's no match or
        the reference is malformed. Deliberately does not raise - a bad
        reference should never break generation.
        """
        try:
            if not hasattr(self.db, "search_reference"):
                return None
            rag_references = self.db.search_reference(prompt) or []
            if not rag_references or "blendkit_asset_id:" not in rag_references[0]:
                return None
            match = re.search(r"blendkit_asset_id:([a-f0-9\-]+)", rag_references[0])
            if not match:
                return None
            asset_id = match.group(1)
            self.console_dialog.append_log(
                f"🔗 Augmenting scene with attached BlendKit asset: {asset_id}", "info"
            )
            program = validate_scene_program(
                {"actions": [{"action": "import_blendkit_asset", "params": {"asset_id": asset_id}}]}
            )
            return program[0]
        except Exception as e:
            self.console_dialog.append_log(f"⚠️ Ignoring BlendKit reference: {e}", "error")
            return None

    def _resolve_library_imports(self, program):
        """
        Turns each import_asset_from_library action into a concrete
        import_mesh_file by searching the ingested asset store, extracting
        the best match into the runtime cache, and rewriting the action.
        Unresolved queries (nothing in the library matches) are dropped with
        a log line. This is the bridge that finally makes downloaded assets
        usable by generation instead of write-only.
        """
        resolved = []
        for action in program:
            if action.get("action") != "import_asset_from_library":
                resolved.append(action)
                continue

            params = action.get("params", {}) or {}
            query = str(params.get("query", "")).strip()
            matches = self.asset_manager.search_assets(query) if query else []
            if not matches:
                self.console_dialog.append_log(
                    f"🔎 No library asset matched '{query}' - skipping that import.", "error"
                )
                continue

            best = matches[0]
            asset_name = best["name"]
            asset_type = (best.get("type") or "obj").lower()
            stored_path = best.get("file_path")

            # Prefer importing the asset IN PLACE from its original store
            # location. This matters for .gltf (references a sibling .bin +
            # textures) and .obj (references a sibling .mtl + textures) -
            # copying only the single indexed file to a flat cache folder
            # would strip those siblings and import a broken/untextured
            # mesh. The desktop app and Blender run on the same machine, so
            # the original absolute path resolves for Blender directly.
            import_path = None
            if stored_path and Path(stored_path).exists():
                import_path = stored_path
            else:
                # Legacy BLOB row (single self-contained file) or a moved
                # asset - extract the one file to the runtime cache.
                cache_path = self.runtime_cache_dir / f"{asset_name}.{asset_type}"
                if cache_path.exists() or self.asset_manager.load_asset_to_disk(
                    asset_name, cache_path
                ):
                    import_path = str(cache_path)

            if not import_path:
                self.console_dialog.append_log(
                    f"⚠️ Found '{asset_name}' in the index but its file is missing on disk.",
                    "error",
                )
                continue

            self.console_dialog.append_log(
                f"📦 Library match for '{query}': {asset_name} ({asset_type})", "info"
            )
            new_params = {"file_path": import_path, "file_ext": asset_type}
            # Pass placement / naming through so the add-on can position, name,
            # and (later) instance the imported asset - e.g. a real tree model
            # placed and then duplicated down a road.
            for key in ("location", "scale", "rotation"):
                if isinstance(params.get(key), (list, tuple)):
                    new_params[key] = list(params[key])
            if isinstance(params.get("name"), str) and params["name"].strip():
                new_params["name"] = params["name"].strip()

            # If a tile_length was requested, the add-on imports the segment
            # once, measures its real length, and repeats it end-to-end to fill
            # that length (a road model tiled to 1 km without stretching).
            if params.get("tile_length"):
                new_params["tile_length"] = params["tile_length"]
                new_params["tile_axis"] = params.get("tile_axis", "y")
                resolved.append({"action": "import_mesh_tiled", "params": new_params})
            else:
                resolved.append({"action": "import_mesh_file", "params": new_params})

        # Guarantee the program is never empty after resolution (e.g. the
        # only action was an unresolved library import) so Blender always
        # gets something to do.
        if not resolved:
            resolved = [
                {"action": "add_primitive", "params": {"shape": "cube", "name": "AI_Placeholder"}},
                {"action": "add_material", "params": {"base_color": [0.6, 0.6, 0.65, 1.0]}},
                {
                    "action": "add_light",
                    "params": {"light_type": "AREA", "location": [4, -4, 6], "energy": 800},
                },
                {"action": "set_camera", "params": {"location": [6, -6, 4], "look_at": [0, 0, 0]}},
            ]
        return resolved

    def _send_bridge_response(self, response: dict):
        """
        Publishes the response for the HTTP handler thread waiting in
        do_POST and wakes it up. Every exit path out of
        handle_incoming_blender_payload must go through this (instead of
        setting BridgeHTTPRequestHandler.last_response_data directly) or
        the waiting request will time out after wait_timeout seconds.
        """
        BridgeHTTPRequestHandler.last_response_data = response
        BridgeHTTPRequestHandler.response_ready.set()

    def handle_incoming_blender_payload(self, payload: dict):
        payload_type = payload.get("type", "unknown")
        start_time = time.time()

        if payload_type == "browser_model_import":
            model_data = payload.get("prompt", "").strip()
            source_url = payload.get("source_url", "")

            self.console_dialog.append_log(
                f"🌐 Received Model from Browser: '{model_data}' (Source: {source_url})", "info"
            )

            self.blendkit_input.setText(model_data)
            self.attach_blendkit_reference()

            self._send_bridge_response(
                {"status": "ok", "message": "Browser model reference processed by LRJK AI Studio"}
            )
            return

        elif payload_type == "generate_prompt":
            prompt = payload.get("prompt", "").strip()
            self.console_dialog.append_log(f"📥 Received Blender Prompt: '{prompt}'", "code")

            program = None
            source = "unknown"

            # 0) MEASURED layout requests (a road/street with explicit lengths
            #    and spacings) are built DETERMINISTICALLY, ahead of the AI. The
            #    user asked for exact numbers ("1 km", "every 300 m", "trees
            #    every 10 m") and a language model won't reliably emit 200
            #    exactly-spaced objects - it produces a vague handful instead
            #    (the "Road_Main + 3 trees" result). The rule-based builder
            #    reproduces the exact geometry the prompt describes.
            if self._wants_precise_layout(prompt):
                program = self._road_actions(prompt)
                source = "precise-layout"
                self.console_dialog.append_log(
                    f"📐 Measured layout detected - built exactly from your "
                    f"numbers ({len(program)} actions), bypassing the AI's "
                    f"approximation.",
                    "metric",
                )

            # 1) Otherwise, if an AI provider is configured, it drives
            #    generation. It can build the subject from primitives AND emit
            #    import_asset_from_library to pull a real model out of the
            #    ingested library. (Previously an attached BlendKit reference
            #    was checked first and would hijack any loosely-matching prompt
            #    - e.g. "road" - into a single import_blendkit_asset action,
            #    so the AI and the asset library were never even consulted.)
            endpoint = self.settings.value("api_endpoint", "").strip()
            if program is None and endpoint:
                try:
                    api_key = self.settings.value("api_key", "")
                    model = self.settings.value("api_model", "")
                    provider = self.settings.value("api_provider", "")
                    program = request_scene_program(prompt, provider, endpoint, api_key, model)
                    source = "ai"
                    names = ", ".join(a["action"] for a in program)
                    self.console_dialog.append_log(
                        f"🤖 AI built a {len(program)}-step scene program: [{names}]", "metric"
                    )
                except AIProviderError as e:
                    self.console_dialog.append_log(
                        f"⚠️ AI provider unavailable ({e}); trying reference/rule-based fallback.",
                        "error",
                    )

            # 2) No AI (or it failed): build the deterministic rule-based
            #    program. This is ALWAYS a real, lit, framed scene (a road, a
            #    donut, a lit primitive - see _rule_based_program), never a
            #    lone import.
            if program is None:
                program = self._rule_based_program(prompt)
                source = "rule-based"
                self.console_dialog.append_log(
                    f"🧩 Rule-based fallback built a {len(program)}-step program.", "info"
                )

                # 2b) An explicitly attached BlendKit reference AUGMENTS the
                #     scene (appended as one extra optional import) - it must
                #     never REPLACE it. Previously a loosely-matching reference
                #     turned an entire prompt (e.g. a whole 1 km road) into a
                #     single import_blendkit_asset action, which then failed
                #     outright when the BlendKit add-on wasn't installed
                #     ("Built scene: 0/1 actions succeeded"). Now the built
                #     scene stands on its own and the reference rides on top.
                bk_action = self._matched_blendkit_action(prompt)
                if bk_action is not None:
                    program = program + [bk_action]
                    source = "rule-based + blendkit-ref"

            # Resolve any library-import actions (search the ingested asset
            # store, extract the match to the runtime cache, rewrite into a
            # concrete import_mesh_file) BEFORE sending to Blender.
            program = self._resolve_library_imports(program)

            exec_time = time.time() - start_time
            self.db.save_successful_script(prompt, json.dumps(program), exec_time)
            self.console_dialog.append_log(
                f"💾 Saved {len(program)}-action program ({source}) to DB. Time: {exec_time:.4f}s",
                "metric",
            )
            self.welcome_label.setText(
                f"<b>Output Status:</b> Built a {len(program)}-action scene program "
                f"from '{prompt}' ({source}, {exec_time:.4f}s)"
            )

            self._send_bridge_response(
                {
                    "status": "ok",
                    "message": f"Generated {len(program)}-action scene program",
                    "actions": program,
                }
            )

        elif payload_type == "generate_mesh_from_text":
            # Explicit, user-triggered mesh generation via Tripo3D - a
            # separate dedicated button in the Blender panel, deliberately
            # NOT folded into the AI-provider scene-action flow above. The
            # user knows exactly when this runs because they clicked a
            # button that says so (and it can spend paid generation
            # credits), unlike generate_prompt which may be AI-decided.
            mesh_prompt = payload.get("prompt", "").strip()
            self.console_dialog.append_log(
                f"🧊 Received Text-to-3D request: '{mesh_prompt}'", "code"
            )

            if not mesh_prompt:
                self._send_bridge_response(
                    {
                        "status": "error",
                        "message": "Empty prompt - nothing to generate.",
                    }
                )
                return

            tripo_key = (
                os.getenv("TRIPO3D_API_KEY", "").strip()
                or self.settings.value("tripo3d_api_key", "").strip()
            )

            try:
                local_path = generate_mesh_from_text(mesh_prompt, tripo_key, self.runtime_cache_dir)
                exec_time = time.time() - start_time
                self.console_dialog.append_log(
                    f"🧊 Mesh generated via Tripo3D in {exec_time:.1f}s -> {local_path.name}",
                    "metric",
                )
                self.db.save_successful_script(
                    mesh_prompt,
                    json.dumps(
                        {
                            "action": "import_mesh_file",
                            "params": {"file_ext": local_path.suffix.lstrip(".")},
                        }
                    ),
                    exec_time,
                )
                self._send_bridge_response(
                    {
                        "status": "ok",
                        "message": f"Generated mesh from text in {exec_time:.1f}s",
                        "action": "import_mesh_file",
                        "params": {
                            "file_path": str(local_path),
                            "file_ext": local_path.suffix.lstrip("."),
                        },
                    }
                )
            except Tripo3DError as e:
                self.console_dialog.append_log(f"⚠️ Text-to-3D generation failed: {e}", "error")
                self._send_bridge_response({"status": "error", "message": str(e)})
            except Exception as e:
                # Anything unexpected (disk full, bad path, etc.) still
                # needs to unblock the waiting HTTP request rather than
                # leave it to time out after wait_timeout seconds.
                self.console_dialog.append_log(
                    f"⚠️ Text-to-3D generation failed unexpectedly: {e}", "error"
                )
                self._send_bridge_response({"status": "error", "message": f"Unexpected error: {e}"})

        else:
            self.console_dialog.append_log(f"📡 Handshake Ping from Blender: {payload}", "info")
            self._send_bridge_response({"status": "ok", "message": "Handshake acknowledged"})

    def open_history_dialog(self):
        dialog = SavedHistoryDialog(self.db, self)
        dialog.exec()

    def open_console_dialog(self):
        self.console_dialog.show()

    def open_settings_dialog(self):
        dialog = APISettingsDialog(self.settings, self)
        if dialog.exec() == QDialog.Accepted:
            self.update_active_status_label()

    def update_active_status_label(self):
        provider = self.settings.value("api_provider", "OpenAI (Paid / Tiered)")
        endpoint = self.settings.value("api_endpoint", "https://api.openai.com/v1")
        model = self.settings.value("api_model", "gpt-4o")
        port = self.settings.value("bridge_port", "8081")

        has_key = (
            "Configured" if self.settings.value("api_key", "") else "None Required (Local/Free)"
        )

        self.active_status_label.setText(
            f"<b>Provider:</b> {provider} | "
            f"<b>Model:</b> {model} | "
            f"<b>Endpoint:</b> {endpoint}<br>"
            f"<b>API Key Status:</b> {has_key} | "
            f"<b>Blender Bridge Port:</b> {port}"
        )

    def closeEvent(self, event):
        if hasattr(self, "bridge_worker"):
            self.bridge_worker.stop()
            self.bridge_worker.quit()
            self.bridge_worker.wait()
        if hasattr(self, "update_checker"):
            self.update_checker.quit()
            self.update_checker.wait()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    video_splash_path = get_resource_path("assets/splash_screen.mp4")
    main_window = MainWindow()

    def show_main_window():
        main_window.show()

    if video_splash_path.exists():
        splash = VideoSplashScreen(video_splash_path, show_main_window)
        splash.start_playback()
    else:
        show_main_window()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
