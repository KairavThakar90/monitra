import sys
from PySide6.QtWidgets import QApplication
from ui.idle_alert_dialog import IdleAlertDialog

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

def main():
    app = QApplication(sys.argv)
    period = {"idle_started_at": "2026-09-17T14:30:00Z", "original_project_id": 1}
    dialog = IdleAlertDialog(MockApi(), period, project_name_resolver=lambda pid: "Beta Launch")
    dialog.show()

    from PySide6.QtCore import QTimer
    def take_screenshot():
        pixmap = dialog.grab()
        pixmap.save("scratch/current_dialog.png")
        app.quit()
    QTimer.singleShot(400, take_screenshot)
    app.exec()

if __name__ == "__main__":
    main()
