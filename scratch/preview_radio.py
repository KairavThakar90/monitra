import sys
from PySide6.QtWidgets import QApplication
from ui.idle_alert_dialog import IdleAlertDialog
from ui.styles import BORDER_MID, PRIMARY, PRIMARY_HOVER, TEXT_PRIMARY, TEXT_MUTED

def main():
    app = QApplication(sys.argv)
    period = {"idle_started_at": "2026-09-17T14:30:00Z", "original_project_id": 1}
    dialog = IdleAlertDialog(MockApi(), period, project_name_resolver=lambda pid: "Beta Launch")

    qss = f"""
            QRadioButton {{
                color: {TEXT_PRIMARY};
                spacing: 10px;
            }}
            QRadioButton::indicator {{
                width: 14px;
                height: 14px;
                border-radius: 9px;
                border: 2px solid {BORDER_MID};
                background-color: #FFFFFF;
            }}
            QRadioButton::indicator:hover {{
                border-color: {PRIMARY};
            }}
            QRadioButton::indicator:checked {{
                border-color: {PRIMARY};
                background-color: {PRIMARY};
                padding: 3px;
                background-clip: content;
            }}
            QRadioButton::indicator:checked:hover {{
                border-color: {PRIMARY_HOVER};
                background-color: {PRIMARY_HOVER};
            }}
            QRadioButton:disabled {{
                color: {TEXT_MUTED};
            }}
    """
    dialog.setStyleSheet(qss)
    dialog.show()

    from PySide6.QtCore import QTimer
    def take_screenshot():
        pixmap = dialog.grab()
        pixmap.save("scratch/idle_dialog_pureqss_preview.png")
        app.quit()
    QTimer.singleShot(500, take_screenshot)
    app.exec()

class MockApi:
    def __init__(self):
        self.idle = MockIdleService()
    def active_session(self):
        return {"project_id": 1, "task_id": 2, "task_name": "v2 task 1"}

class MockIdleService:
    def __init__(self):
        from PySide6.QtCore import QObject, Signal
        class DummySignal(QObject):
            sig = Signal(object)
            def connect(self, fn): pass
        self.resolve_succeeded = DummySignal()
        self.resolve_failed = DummySignal()
        self.reassign_succeeded = DummySignal()
        self.reassign_failed = DummySignal()
        self.idle_period_cleared = DummySignal()

if __name__ == "__main__":
    main()
