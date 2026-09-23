from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from accounts.models import User
from providers.models import ProviderProfile
from .models import MediaAsset
from .views import ProviderVideoUploadView
from .video import validate_provider_video


def box(kind, payload):
    return (len(payload) + 8).to_bytes(4, "big") + kind + payload


def video_container(brand=b"mp42", handler=b"vide"):
    # Small structural fixture: upload validation does not decode video frames.
    return (
        box(b"ftyp", brand + b"\0" * 4 + brand)
        + box(b"moov", box(b"trak", box(b"mdia", box(b"hdlr", b"\0" * 8 + handler))))
        + box(b"mdat", b"test-frame")
    )


class ProviderVideoTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(phone="13977000001")
        self.profile = ProviderProfile.objects.create(
            user=self.user, status=ProviderProfile.Status.APPROVED
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    @patch("mediafiles.views.upload_public_stream")
    def test_upload_requires_approved_provider(self, upload):
        self.profile.status = ProviderProfile.Status.DRAFT
        self.profile.save(update_fields=("status",))
        response = self.client.post(
            "/api/v1/media/provider-videos/",
            {"file": SimpleUploadedFile("clip.mp4", video_container(), "video/mp4")},
            format="multipart",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(MediaAsset.objects.exists())
        upload.assert_not_called()

    @patch("mediafiles.views.build_media_url", return_value="https://media.test/video.mp4")
    @patch("mediafiles.views.upload_public_stream", return_value="video-etag")
    def test_upload_accepts_mp4_and_mov_and_normalizes_device_mime(self, upload, _url):
        for brand, expected_type in ((b"mp42", "video/mp4"), (b"qt  ", "video/quicktime")):
            with self.subTest(brand=brand):
                response = self.client.post(
                    "/api/v1/media/provider-videos/",
                    {
                        "file": SimpleUploadedFile(
                            "clip.bin", video_container(brand), "application/octet-stream"
                        )
                    },
                    format="multipart",
                )
                self.assertEqual(response.status_code, 201, response.data)
                asset = MediaAsset.objects.get(pk=response.data["data"]["id"])
                self.assertEqual(asset.category, "provider_video")
                self.assertEqual(asset.status, "uploaded")
                self.assertEqual(asset.owner, self.user)
                self.assertEqual(asset.content_type, expected_type)
                self.assertEqual(upload.call_args.kwargs["content_type"], expected_type)

    @patch("mediafiles.views.upload_public_stream")
    def test_rejects_fake_truncated_audio_only_or_wrong_mime(self, upload):
        for payload, mime in (
            (b"not a video", "video/mp4"),
            (video_container()[:-2], "video/mp4"),
            (video_container(handler=b"soun"), "video/mp4"),
            (video_container(), "image/jpeg"),
        ):
            response = self.client.post(
                "/api/v1/media/provider-videos/",
                {"file": SimpleUploadedFile("clip.mp4", payload, mime)},
                format="multipart",
            )
            self.assertEqual(response.status_code, 400, response.data)
        upload.assert_not_called()
        self.assertFalse(MediaAsset.objects.exists())

    def test_validation_restores_stream_position(self):
        uploaded = SimpleUploadedFile("clip.mp4", video_container(), "video/mp4")
        validate_provider_video(uploaded)
        self.assertEqual(uploaded.tell(), 0)
        uploaded = SimpleUploadedFile("bad.mp4", b"broken", "video/mp4")
        with self.assertRaises(ValidationError):
            validate_provider_video(uploaded)
        self.assertEqual(uploaded.tell(), 0)

    @patch.object(ProviderVideoUploadView, "max_size", 10)
    def test_rejects_oversize_video(self):
        response = self.client.post(
            "/api/v1/media/provider-videos/",
            {"file": SimpleUploadedFile("clip.mp4", video_container(), "video/mp4")},
            format="multipart",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(MediaAsset.objects.exists())

    def test_upload_requires_login(self):
        self.client.force_authenticate(None)
        response = self.client.post("/api/v1/media/provider-videos/", {}, format="multipart")
        self.assertIn(response.status_code, (401, 403))
