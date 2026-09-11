"""
Login window — Monitra branding on a centered card.

Presentation only: the AuthService integration is unchanged, the widget owns
no thread, and the login call still runs on the shared bounded pool when a
BackgroundApi is attached (inline in unit tests, where it is not).
"""
from typing import Optional

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QFrame, QSizePolicy, QGraphicsDropShadowEffect,
    QToolButton,
)

from app.api.exceptions import redact_urls
from app.auth.service import AuthService
from core.branding import logo_pixmap
from core.validation import (
    EMAIL_MAX_LENGTH,
    PASSWORD_MAX_LENGTH,
    validate_credential,
    validate_username,
)
from ui import icons
from ui.styles import (
    CONTENT_BG, PRIMARY, PRIMARY_LIGHT, TEXT_PRIMARY, TEXT_SECONDARY, TEXT_MUTED,
    ERROR, BORDER_LIGHT, CARD_BG, LOGIN_QSS,
    BUTTON_GRADIENT, BUTTON_GRADIENT_HOVER,
)


class MonitoraLogo(QWidget):
    """The Monitra mark and wordmark side by side, centered.

    The mark comes from core.branding, so a real logo file dropped into
    desktop/assets/ shows here too, exactly as it does in the sidebar and the
    tray -- there is no second copy of the artwork.
    """

    def __init__(self, mark_size: int = 48, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.mark_size = mark_size
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.mark = QLabel(self)
        self.mark.setFixedSize(mark_size, mark_size)
        self.mark.setPixmap(logo_pixmap(mark_size))
        self.mark.setStyleSheet("background: transparent;")
        layout.addWidget(self.mark)

        wordmark = QLabel("Monitra", self)
        wordmark.setFont(QFont("Segoe UI", 22, QFont.Weight.Black))
        wordmark.setStyleSheet(
            f"color: {TEXT_PRIMARY}; letter-spacing: -0.5px; background: transparent;"
        )
        layout.addWidget(wordmark)
        self.setFixedHeight(mark_size + 4)


class _Field(QFrame):
    """A labelled input with a leading icon tile.

    The tile is a sibling of the QLineEdit inside one bordered frame rather
    than an action inside the field, so the frame can carry the focus ring
    around both -- which is what makes the focused state read as one control.
    """

    def __init__(
        self,
        icon_name: str,
        placeholder: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("FieldFrame")
        self.setFixedHeight(48)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 6, 10, 6)
        layout.setSpacing(10)

        self.tile = QLabel(self)
        self.tile.setObjectName("FieldTile")
        self.tile.setFixedSize(34, 34)
        self.tile.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tile.setPixmap(icons.pixmap(icon_name, PRIMARY, 18))
        layout.addWidget(self.tile)

        self.input = QLineEdit(self)
        self.input.setObjectName("LoginInput")
        self.input.setPlaceholderText(placeholder)
        self.input.setFrame(False)
        self.input.setFont(QFont("Segoe UI", 11))
        layout.addWidget(self.input, 1)

        self.input.installEventFilter(self)
        self._apply_style(focused=False)

    def eventFilter(self, watched, event) -> bool:
        if watched is self.input:
            if event.type() == event.Type.FocusIn:
                self._apply_style(focused=True)
            elif event.type() == event.Type.FocusOut:
                self._apply_style(focused=False)
        return super().eventFilter(watched, event)

    def _apply_style(self, focused: bool) -> None:
        border = PRIMARY if focused else BORDER_LIGHT
        self.setStyleSheet(f"""
            QFrame#FieldFrame {{
                background: {CARD_BG};
                border: 1.5px solid {border};
                border-radius: 12px;
            }}
            QLabel#FieldTile {{
                background: {PRIMARY_LIGHT};
                border: none;
                border-radius: 9px;
            }}
            QLineEdit#LoginInput {{
                background: transparent;
                border: none;
                /* The frame supplies the padding; the input must claim none of
                   its own. Padding here is subtracted from the text rect, and
                   the frame's fixed height leaves no slack to absorb it, so
                   any value above zero clips the glyph descenders. */
                padding: 0;
                color: {TEXT_PRIMARY};
                font-size: 13px;
                selection-background-color: {PRIMARY_LIGHT};
            }}
        """)


class LoginWindow(QWidget):
    """
    Full-page login widget with Monitra branding.
    Emits login_success(dict) on successful authentication.
    """
    login_success = Signal(dict)

    def __init__(
        self,
        auth_service: AuthService,
        api=None,
        parent: Optional[QWidget] = None,
    ) -> None:
        """
        :param api: `BackgroundApi`. Authentication runs on the shared bounded
            pool; this widget owns no thread. When omitted (unit tests), the
            login call runs inline.
        """
        super().__init__(parent)
        self.auth_service = auth_service
        self.api = api
        self._login_in_flight = False
        self.setObjectName("LoginPage")
        self._init_ui()

    def _init_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.setContentsMargins(20, 20, 20, 20)

        card = QFrame(self)
        card.setObjectName("LoginCard")
        card.setFixedWidth(420)
        card.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)

        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(48)
        shadow.setOffset(0, 12)
        shadow.setColor(QColor(16, 24, 40, 38))
        card.setGraphicsEffect(shadow)

        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(40, 38, 40, 38)
        card_layout.setSpacing(0)

        # ── Logo ──────────────────────────────────────────────────
        card_layout.addWidget(MonitoraLogo(mark_size=46, parent=card))
        card_layout.addSpacing(26)

        # ── Heading ───────────────────────────────────────────────
        heading = QLabel("Welcome back 👋", card)
        heading.setFont(QFont("Segoe UI", 19, QFont.Weight.Black))
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        heading.setStyleSheet(f"color: {TEXT_PRIMARY}; background: transparent;")
        card_layout.addWidget(heading)
        card_layout.addSpacing(4)

        sub = QLabel("Sign in to your account to continue", card)
        sub.setFont(QFont("Segoe UI", 11))
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setStyleSheet(f"color: {TEXT_SECONDARY}; background: transparent;")
        card_layout.addWidget(sub)
        card_layout.addSpacing(26)

        # ── Email / username ──────────────────────────────────────
        card_layout.addWidget(self._field_label("Email or Username", card))
        card_layout.addSpacing(7)

        self._username_field = _Field("person", "Enter your email or username", card)
        self.username_input = self._username_field.input
        self.username_input.setMaxLength(EMAIL_MAX_LENGTH)
        self.username_input.returnPressed.connect(self._handle_login)
        card_layout.addWidget(self._username_field)
        card_layout.addSpacing(16)

        # ── Password ──────────────────────────────────────────────
        card_layout.addWidget(self._field_label("Password", card))
        card_layout.addSpacing(7)

        self._password_field = _Field("lock", "Enter your password", card)
        self.password_input = self._password_field.input
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        # Only the upper bound is applied to a password field. It exists to cap
        # the work handed to the hasher, and it is the sole password rule this
        # widget enforces -- nothing here trims, normalises or inspects the
        # secret, because every one of those would silently change it.
        self.password_input.setMaxLength(PASSWORD_MAX_LENGTH)
        self.password_input.returnPressed.connect(self._handle_login)

        # Reveal toggle. Nothing is remembered across a reset(): the field
        # always returns to masked, so a cleared form cannot leave the next
        # person's typing visible.
        self.reveal_button = QToolButton(self._password_field)
        self.reveal_button.setObjectName("RevealBtn")
        self.reveal_button.setCheckable(True)
        self.reveal_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reveal_button.setFixedSize(28, 28)
        self.reveal_button.setIconSize(QSize(18, 18))
        self.reveal_button.setIcon(icons.icon("visibility", TEXT_MUTED, 18))
        self.reveal_button.setToolTip("Show password")
        self.reveal_button.setStyleSheet(
            "QToolButton#RevealBtn { background: transparent; border: none; }"
        )
        self.reveal_button.toggled.connect(self._on_reveal_toggled)
        self._password_field.layout().addWidget(self.reveal_button)
        card_layout.addWidget(self._password_field)
        card_layout.addSpacing(14)

        # ── Message line (errors and hints share one line) ────────
        self.error_label = QLabel("", card)
        self.error_label.setFont(QFont("Segoe UI", 10, QFont.Weight.DemiBold))
        self.error_label.setStyleSheet(f"color: {ERROR}; background: transparent;")
        self.error_label.setWordWrap(True)
        self.error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.error_label.setMinimumHeight(18)
        card_layout.addWidget(self.error_label)
        card_layout.addSpacing(10)

        # ── Sign In ───────────────────────────────────────────────
        self.login_button = QPushButton(" Sign In", card)
        self.login_button.setObjectName("LoginBtn")
        self.login_button.setIcon(icons.icon("login", "#FFFFFF", 18))
        self.login_button.setIconSize(QSize(18, 18))
        self.login_button.setFixedHeight(48)
        self.login_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.login_button.clicked.connect(self._handle_login)
        card_layout.addWidget(self.login_button)

        outer.addWidget(card)
        self.setStyleSheet(LOGIN_QSS + self._page_qss())

    def _field_label(self, text: str, parent: QWidget) -> QLabel:
        label = QLabel(text, parent)
        label.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        label.setStyleSheet(f"color: {TEXT_PRIMARY}; background: transparent;")
        return label

    def _page_qss(self) -> str:
        return f"""
            QWidget#LoginPage {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                            stop:0 {CONTENT_BG}, stop:1 #EEF1FB);
            }}
            QFrame#LoginCard {{
                background-color: {CARD_BG};
                border: 1px solid {BORDER_LIGHT};
                border-radius: 20px;
            }}
            QPushButton#LoginBtn {{
                background: {BUTTON_GRADIENT};
                color: #FFFFFF;
                border: none;
                border-radius: 12px;
                font-size: 14px;
                font-weight: bold;
            }}
            QPushButton#LoginBtn:hover {{
                background: {BUTTON_GRADIENT_HOVER};
            }}
            QPushButton#LoginBtn:disabled {{
                background: #C7D2FE;
                color: #F8FAFC;
            }}
        """

    # ── Interaction ───────────────────────────────────────────────────────────

    def _on_reveal_toggled(self, revealed: bool) -> None:
        self.password_input.setEchoMode(
            QLineEdit.EchoMode.Normal if revealed else QLineEdit.EchoMode.Password
        )
        self.reveal_button.setIcon(
            icons.icon("visibility_off" if revealed else "visibility", TEXT_MUTED, 18)
        )
        self.reveal_button.setToolTip("Hide password" if revealed else "Show password")

    def _set_message(self, text: str, color: str = ERROR) -> None:
        self.error_label.setText(text)
        self.error_label.setStyleSheet(f"color: {color}; background: transparent;")

    def _handle_login(self) -> None:
        if self._login_in_flight:
            return
        # The username field accepts either a username or an email address, so
        # it is held to plain-text rules rather than email rules -- requiring an
        # "@" here would break username logins, which this deployment supports.
        username = validate_username(
            self.username_input.text(), field_label="Email/username"
        )
        # `validate_credential`, not `validate_password`: the password is being
        # presented, not chosen. Enforcing the minimum-length policy here would
        # lock out any account created before that policy, and the failure would
        # look like a wrong password. The value is passed on exactly as typed --
        # never trimmed or normalised, because either would change the secret.
        password = validate_credential(self.password_input.text())
        if not username.ok or not password.ok:
            self._set_message("Email/username and password are required.")
            return
        username, password = username.value, password.value

        self._set_message("")
        self._set_loading(True)
        self._login_in_flight = True

        def call():
            return self.auth_service.login(username, password)

        if self.api is None:
            # No runtime attached (unit tests): run inline.
            try:
                self._on_login_success(call())
            except Exception as exc:  # noqa: BLE001
                self._on_login_error(str(exc))
            return

        self.api.run_in_background(
            call,
            on_success=self._on_login_success,
            on_error=lambda exc: self._on_login_error(str(exc)),
            key="login",
        )

    def _set_loading(self, loading: bool) -> None:
        self.username_input.setEnabled(not loading)
        self.password_input.setEnabled(not loading)
        self.login_button.setEnabled(not loading)
        self.login_button.setText(" Signing in..." if loading else " Sign In")

    def _on_login_success(self, user_data: dict) -> None:
        self._login_in_flight = False
        self._set_loading(False)
        self.password_input.clear()
        self.login_success.emit(user_data)

    def _on_login_error(self, error_message: str) -> None:
        self._login_in_flight = False
        self._set_loading(False)
        # The message is shown verbatim, so it is redacted here rather than
        # trusted. This handler receives str() of *any* exception -- including
        # ones raised below the API client and text echoed from a backend
        # `detail` -- and this screen is the one most likely to be read over a
        # user's shoulder. The endpoint is in the log for support.
        self._set_message(redact_urls(error_message))

    def reset(self) -> None:
        """Clear inputs when returning to login screen."""
        self.username_input.clear()
        self.password_input.clear()
        self.reveal_button.setChecked(False)
        self._set_message("")
        self._set_loading(False)
        self.username_input.setFocus()

    def show_checking_session(self) -> None:
        """Show loading indicator while restoring stored session on startup."""
        self._set_loading(True)
        self._set_message("Restoring session...", TEXT_SECONDARY)
