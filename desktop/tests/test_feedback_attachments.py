"""Feedback attachments -- the shared rules and the service call.

Two things are protected here. The first is the rule set the dialog and the
service share (`validate_attachment_file` and friends): what is accepted, what
is refused, and the exact sentence the user is shown. The second is the
request the service sends and what it makes of every answer the backend can
give -- the contract is `POST /feedback/with-attachments`, multipart, with the
file part repeated under the name `files`.
"""
from __future__ import annotations

import io
import json

import httpx
import pytest
from PIL import Image

from app.api.client import ApiClient, TIMEOUT_SLOW
from app.api.exceptions import (
    ApiConnectionError, ApiError, ApiHttpError, ApiTimeoutError,
)
from app.feedback import (
    ALLOWED_ATTACHMENT_TYPES, MAX_ATTACHMENTS, MAX_TOTAL_ATTACHMENT_BYTES,
    FeedbackApiService, FeedbackAttachmentError, format_file_size,
    validate_attachment_file,
)
from app.feedback.service import detect_image_type

TYPE_MESSAGE = "That file type isn't supported. Please attach a PNG, JPG or WEBP image."
TOO_LARGE = "Attachment is too large. Maximum allowed size is 10 MB."
UNREADABLE = "That file couldn't be read. Please choose another."
VANISHED = "An attachment could no longer be read. Please remove it and try again."
GOOD_OP = "0123456789abcdef0123456789abcdef"


# ── Real image bytes ──────────────────────────────────────────────────────────


def image_bytes(fmt: str, size=(32, 24), colour=(40, 100, 200)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, fmt)
    return buffer.getvalue()


PNG = image_bytes("PNG")
JPEG = image_bytes("JPEG")
WEBP = image_bytes("WEBP")


def padded(header_source: bytes, total: int) -> bytes:
    """A file of `total` bytes that starts like `header_source`."""
    return header_source + b"\0" * (total - len(header_source))


def write(tmp_path, name: str, data: bytes):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


# ── The shared rules ──────────────────────────────────────────────────────────


def test_the_limits_are_the_documented_ones():
    assert MAX_ATTACHMENTS == 3
    assert MAX_TOTAL_ATTACHMENT_BYTES == 10 * 1024 * 1024
    assert set(ALLOWED_ATTACHMENT_TYPES) == {"png", "jpg", "jpeg", "webp"}


def test_real_headers_are_recognised():
    assert detect_image_type(PNG[:12]) == "png"
    assert detect_image_type(JPEG[:12]) == "jpeg"
    assert detect_image_type(WEBP[:12]) == "webp"
    assert detect_image_type(b"MZ\x90\x00" + b"\0" * 20) is None
    assert detect_image_type(b"") is None
    # RIFF alone is not WEBP (it is also WAV and AVI).
    assert detect_image_type(b"RIFF\0\0\0\0WAVE") is None


@pytest.mark.parametrize("name,data,label", [
    ("shot.png", PNG, "PNG image"),
    ("shot.jpg", JPEG, "JPEG image"),
    ("shot.jpeg", JPEG, "JPEG image"),
    ("shot.webp", WEBP, "WEBP image"),
    ("SHOT.PNG", PNG, "PNG image"),       # the extension is case-insensitive
    ("Shot.JpG", JPEG, "JPEG image"),
])
def test_an_image_of_an_allowed_type_is_accepted(tmp_path, name, data, label):
    info = validate_attachment_file(write(tmp_path, name, data))

    assert info.name == name
    assert info.size == len(data)
    assert info.type_label == label


@pytest.mark.parametrize("name", [
    "setup.exe", "run.bat", "run.cmd", "installer.msi", "lib.dll",
    "script.ps1", "payload.js", "archive.zip", "archive.rar", "bundle.7z",
    "notes.txt", "report.pdf", "noextension", "image.png.exe", "image.gif",
])
def test_anything_off_the_allow_list_is_refused(tmp_path, name):
    # The bytes are a perfectly good PNG: only the extension is wrong.
    with pytest.raises(FeedbackAttachmentError) as caught:
        validate_attachment_file(write(tmp_path, name, PNG))
    assert str(caught.value) == TYPE_MESSAGE


@pytest.mark.parametrize("name", ["fake.png", "fake.jpg", "fake.webp"])
def test_an_executable_renamed_to_an_image_is_refused_by_its_content(tmp_path, name):
    exe = b"MZ\x90\x00\x03\x00\x00\x00" + b"\0" * 200
    with pytest.raises(FeedbackAttachmentError) as caught:
        validate_attachment_file(write(tmp_path, name, exe))
    assert str(caught.value) == TYPE_MESSAGE


def test_an_image_whose_extension_names_a_different_type_is_refused(tmp_path):
    with pytest.raises(FeedbackAttachmentError) as caught:
        validate_attachment_file(write(tmp_path, "really-a-png.jpg", PNG))
    assert str(caught.value) == TYPE_MESSAGE


def test_an_empty_file_is_refused(tmp_path):
    with pytest.raises(FeedbackAttachmentError) as caught:
        validate_attachment_file(write(tmp_path, "empty.png", b""))
    assert str(caught.value) == UNREADABLE


def test_a_directory_is_refused(tmp_path):
    folder = tmp_path / "folder.png"
    folder.mkdir()
    with pytest.raises(FeedbackAttachmentError) as caught:
        validate_attachment_file(str(folder))
    assert str(caught.value) == UNREADABLE


def test_a_missing_file_is_refused(tmp_path):
    with pytest.raises(FeedbackAttachmentError) as caught:
        validate_attachment_file(str(tmp_path / "gone.png"))
    assert str(caught.value) == UNREADABLE


def test_a_file_over_the_limit_is_refused_and_exactly_the_limit_is_not(tmp_path):
    ok = write(tmp_path, "exact.png", padded(PNG, MAX_TOTAL_ATTACHMENT_BYTES))
    assert validate_attachment_file(ok).size == MAX_TOTAL_ATTACHMENT_BYTES

    big = write(tmp_path, "big.png", padded(PNG, MAX_TOTAL_ATTACHMENT_BYTES + 1))
    with pytest.raises(FeedbackAttachmentError) as caught:
        validate_attachment_file(big)
    assert str(caught.value) == TOO_LARGE


def test_validation_reads_only_the_header(tmp_path, monkeypatch):
    """The dialog calls this on the GUI thread: never the whole file."""
    path = write(tmp_path, "big.png", padded(PNG, 5 * 1024 * 1024))
    requested = []
    real_open = open

    def spying_open(file, mode="r", *args, **kwargs):
        handle = real_open(file, mode, *args, **kwargs)
        if str(file) == path:
            original = handle.read

            class Spy:
                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *exc):
                    handle.close()

                def read(self_inner, size=-1):
                    requested.append(size)
                    return original(size)

            return Spy()
        return handle

    monkeypatch.setattr("builtins.open", spying_open)
    validate_attachment_file(path)

    assert requested and all(0 < size <= 64 for size in requested)


@pytest.mark.parametrize("size,text", [
    (0, "0 B"), (512, "512 B"), (1024, "1.0 KB"), (1536, "1.5 KB"),
    (1024 * 1024, "1.0 MB"), (int(1.2 * 1024 * 1024), "1.2 MB"),
    (10 * 1024 * 1024, "10.0 MB"),
])
def test_file_sizes_are_shown_in_plain_units(size, text):
    assert format_file_size(size) == text


# ── The service call ──────────────────────────────────────────────────────────


class FakeClient:
    """Captures `post_multipart` and answers with a canned response or error."""

    def __init__(self, response=None, error=None):
        self.calls = []
        self.response = response if response is not None else {
            "id": 7, "category": "other", "message": "m", "status": "new",
            "created_at": "2026-10-06T10:00:00Z", "duplicate": False,
            "attachments": [],
        }
        self.error = error

    def post_multipart(self, path, files, data=None, timeout=None):
        self.calls.append({"path": path, "files": files, "data": data, "timeout": timeout})
        if self.error is not None:
            raise self.error

        class Response:
            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return self._payload

        return Response(self.response)


def submit(client, tmp_path, names=("a.png",), data=PNG, category="other",
           message="  Something broke.  ", client_op=GOOD_OP):
    paths = [write(tmp_path, name, data) for name in names]
    return FeedbackApiService(client).submit_feedback_with_attachments(
        category, message, paths, client_op
    )


def test_the_request_is_one_multipart_post_with_the_file_part_repeated(tmp_path):
    client = FakeClient()
    paths = [
        write(tmp_path, "one.png", PNG),
        write(tmp_path, "two.JPG", JPEG),
        write(tmp_path, "three.webp", WEBP),
    ]
    result = FeedbackApiService(client).submit_feedback_with_attachments(
        "report_a_problem", "  The timer resets.  ", paths, GOOD_OP
    )

    assert result["id"] == 7
    (call,) = client.calls
    assert call["path"] == "/feedback/with-attachments"
    assert call["timeout"] == TIMEOUT_SLOW
    assert call["data"] == {
        "category": "report_a_problem",
        "message": "The timer resets.",
        "client_op": GOOD_OP,
    }
    assert [field for field, _ in call["files"]] == ["files", "files", "files"]
    parts = [part for _, part in call["files"]]
    assert [(name, mime) for name, _body, mime in parts] == [
        ("one.png", "image/png"),
        ("two.JPG", "image/jpeg"),
        ("three.webp", "image/webp"),
    ]
    assert [body for _name, body, _mime in parts] == [PNG, JPEG, WEBP]


def test_a_filename_is_sent_without_its_directory(tmp_path):
    client = FakeClient()
    submit(client, tmp_path, names=("shot.png",))
    (_field, (name, _body, _mime)), = client.calls[0]["files"]
    assert name == "shot.png"


def test_zero_attachments_is_a_valid_multipart_call(tmp_path):
    client = FakeClient()
    FeedbackApiService(client).submit_feedback_with_attachments(
        "other", "hello", [], GOOD_OP
    )
    assert client.calls[0]["files"] == []


def test_a_duplicate_answer_is_a_success(tmp_path):
    """The reply to the first attempt was lost; the retry finds it stored."""
    stored = {"id": 7, "duplicate": True, "attachments": [{"id": 1}], "status": "new"}
    result = submit(FakeClient(response=stored), tmp_path)
    assert result["duplicate"] is True
    assert result["id"] == 7


@pytest.mark.parametrize("status,body,expected", [
    (413, "<html>Request Entity Too Large</html>", TOO_LARGE),   # a proxy's page
    (413, json.dumps({"detail": "too big"}), TOO_LARGE),
    (422, json.dumps({"detail": "Attachment content does not match its type."}),
     "Attachment content does not match its type."),
    (422, "", "Please check your message and attachment and try again."),
    (401, "", "Your session has expired. Please sign in again and retry."),
    (403, json.dumps({"detail": "Forbidden"}),
     "Your session has expired. Please sign in again and retry."),
    (502, "", "Unable to upload the attachment. Please try again."),
    (503, json.dumps({"detail": "Drive is down"}),
     "Unable to upload the attachment. Please try again."),
    (500, "Traceback (most recent call last): secrets",
     "Something went wrong while submitting your feedback. Please try again."),
    (404, "", "Something went wrong while submitting your feedback. Please try again."),
])
def test_every_http_answer_becomes_a_sentence_for_the_user(tmp_path, status, body, expected):
    client = FakeClient(error=ApiHttpError(status, body))
    with pytest.raises(ApiError) as caught:
        submit(client, tmp_path)
    assert str(caught.value) == expected
    assert caught.value.status_code == status
    # Nothing the backend said that is not the sentence itself reaches the user.
    assert "Traceback" not in str(caught.value)


def test_a_timeout_and_a_lost_connection_say_what_to_do(tmp_path):
    with pytest.raises(ApiError) as timed_out:
        submit(FakeClient(error=ApiTimeoutError("raw timeout")), tmp_path)
    assert str(timed_out.value) == "Uploading your attachment timed out. Please try again."

    with pytest.raises(ApiError) as offline:
        submit(FakeClient(error=ApiConnectionError("raw connect error http://host/x")), tmp_path)
    assert str(offline.value) == (
        "Unable to submit feedback. Please check your internet connection and try again."
    )


def test_an_unexpected_failure_is_not_shown_raw(tmp_path):
    with pytest.raises(ApiError) as caught:
        submit(FakeClient(error=RuntimeError("secret internals")), tmp_path)
    assert "secret" not in str(caught.value)


def test_a_file_that_vanished_since_it_was_chosen_says_so(tmp_path):
    client = FakeClient()
    path = write(tmp_path, "gone.png", PNG)
    import os
    os.remove(path)

    with pytest.raises(ApiError) as caught:
        FeedbackApiService(client).submit_feedback_with_attachments(
            "other", "hello", [path], GOOD_OP
        )
    assert str(caught.value) == VANISHED
    assert client.calls == []


def test_the_bytes_are_re_checked_at_submit_time(tmp_path):
    client = FakeClient()
    exe = write(tmp_path, "swapped.png", b"MZ\x90\x00" + b"\0" * 100)
    with pytest.raises(ApiError) as caught:
        FeedbackApiService(client).submit_feedback_with_attachments(
            "other", "hello", [exe], GOOD_OP
        )
    assert str(caught.value) == TYPE_MESSAGE
    assert client.calls == []


def test_the_total_is_enforced_from_the_bytes_actually_read(tmp_path):
    client = FakeClient()
    six_mib = 6 * 1024 * 1024
    paths = [
        write(tmp_path, "a.png", padded(PNG, six_mib)),
        write(tmp_path, "b.png", padded(PNG, six_mib)),
    ]
    with pytest.raises(ApiError) as caught:
        FeedbackApiService(client).submit_feedback_with_attachments(
            "other", "hello", paths, GOOD_OP
        )
    assert str(caught.value) == TOO_LARGE
    assert client.calls == []


def test_more_than_three_files_is_refused(tmp_path):
    client = FakeClient()
    paths = [write(tmp_path, f"{n}.png", PNG) for n in range(4)]
    with pytest.raises(ApiError) as caught:
        FeedbackApiService(client).submit_feedback_with_attachments(
            "other", "hello", paths, GOOD_OP
        )
    assert str(caught.value) == "Maximum 3 attachments allowed."
    assert client.calls == []


@pytest.mark.parametrize("category,message,client_op", [
    ("other", "   ", GOOD_OP),
    ("nonsense", "hello", GOOD_OP),
    ("other", "hello", "short"),
    ("other", "hello", "has spaces and !! in it......"),
    ("other", "hello", "x" * 65),
    ("other", "x" * 5001, GOOD_OP),
])
def test_the_text_and_the_key_are_validated_before_anything_is_read_or_sent(
    tmp_path, category, message, client_op,
):
    client = FakeClient()
    with pytest.raises(ApiError):
        submit(client, tmp_path, category=category, message=message, client_op=client_op)
    assert client.calls == []


def test_the_plain_json_path_is_untouched(tmp_path):
    """`submit_feedback(category, message)` still posts JSON to /feedback."""
    posted = []

    class JsonClient:
        def post(self, path, json_data=None, timeout=None, **kwargs):
            posted.append((path, json_data))

            class Response:
                @staticmethod
                def json():
                    return {"id": 1}

            return Response()

    FeedbackApiService(JsonClient()).submit_feedback("other", " hi ")
    assert posted == [("/feedback", {"category": "other", "message": "hi"})]


# ── The real client accepts a list of parts ───────────────────────────────────


def _client_with(handler) -> ApiClient:
    client = ApiClient(base_url="http://backend.test")
    client.access_token = "token-123"
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def test_post_multipart_accepts_a_list_of_tuples_and_repeats_the_field(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["content_type"] = request.headers["content-type"]
        seen["authorization"] = request.headers.get("authorization")
        seen["body"] = request.read()
        return httpx.Response(201, json={"id": 3, "duplicate": False, "attachments": []})

    client = _client_with(handler)
    try:
        service = FeedbackApiService(client)
        paths = [write(tmp_path, "one.png", PNG), write(tmp_path, "two.jpg", JPEG)]
        result = service.submit_feedback_with_attachments(
            "suggestion", "Looks great", paths, GOOD_OP
        )
    finally:
        client.close()

    assert result["id"] == 3
    assert seen["url"] == "http://backend.test/feedback/with-attachments"
    assert seen["content_type"].startswith("multipart/form-data; boundary=")
    assert seen["authorization"] == "Bearer token-123"
    body = seen["body"]
    assert body.count(b'name="files"') == 2
    assert b'filename="one.png"' in body and b'filename="two.jpg"' in body
    assert b"Content-Type: image/png" in body and b"Content-Type: image/jpeg" in body
    for field, value in (("category", b"suggestion"), ("message", b"Looks great"),
                         ("client_op", GOOD_OP.encode())):
        assert f'name="{field}"'.encode() in body
        assert value in body
    assert PNG in body and JPEG in body


def test_post_multipart_still_accepts_the_dict_form_the_screenshot_upload_uses():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.read()
        return httpx.Response(201, json={})

    client = _client_with(handler)
    try:
        client.post_multipart(
            "/upload", files={"file": ("a.png", PNG, "image/png")}, data={"k": "v"}
        )
    finally:
        client.close()
    assert seen["body"].count(b'name="file"') == 1


def test_a_413_from_a_proxy_with_an_html_body_reaches_the_user_as_the_size_message(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(413, text="<html><body>413 Request Entity Too Large</body></html>")

    client = _client_with(handler)
    try:
        with pytest.raises(ApiError) as caught:
            FeedbackApiService(client).submit_feedback_with_attachments(
                "other", "hello", [write(tmp_path, "a.png", PNG)], GOOD_OP
            )
    finally:
        client.close()
    assert str(caught.value) == TOO_LARGE


def test_the_dashboard_hands_the_dialog_both_service_methods(monkeypatch):
    """The wiring: the dialog's attachment path is the service's, not a new one."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    import ui.dashboard_window as dashboard

    built = {}

    class SpyDialog:
        def __init__(self, api, **kwargs):
            built.update(kwargs)
            self.finished = MagicMock()

        def show(self): ...
        def raise_(self): ...
        def activateWindow(self): ...

    monkeypatch.setattr(dashboard, "FeedbackDialog", SpyDialog)
    service = SimpleNamespace(
        submit_feedback=lambda c, m: {},
        submit_feedback_with_attachments=lambda c, m, p, op: {},
    )
    fake = SimpleNamespace(
        _feedback_dialog=None, api=MagicMock(),
        runtime=SimpleNamespace(feedback_service=service),
        window=lambda: None, _forget_feedback_dialog=lambda: None,
    )
    dashboard.DashboardWindow._open_feedback_dialog(fake)

    assert built["submitter"] is service.submit_feedback
    assert built["attachment_submitter"] is service.submit_feedback_with_attachments
