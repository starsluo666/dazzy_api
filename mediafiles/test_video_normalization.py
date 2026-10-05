"""Synthetic codec fixtures only: no real users' photos, voices or videos."""

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from unittest import SkipTest
from unittest.mock import patch

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from accounts.models import User
from providers.models import ProviderProfile
from .models import MediaAsset
from .test_video import video_container
from .video_normalization import (
    VideoProcessingUnavailable, _filters, _probe, _run, normalize_video_upload,
)


class VideoNormalizationGuardTests(SimpleTestCase):
    def test_overlong_video_rejected_before_transcoding(self):
        with patch("mediafiles.video_normalization.shutil.which", side_effect=lambda name: name), patch(
            "mediafiles.video_normalization._probe", return_value=({"index": 0}, 10.001, []),
        ), patch("mediafiles.video_normalization._run") as transcode:
            with self.assertRaisesMessage(ValidationError, "不能超过10秒"):
                normalize_video_upload(SimpleUploadedFile("long.mov", b"synthetic"), max_size=1024)
            transcode.assert_not_called()

    def test_missing_binary_fails_closed(self):
        with patch("mediafiles.video_normalization.shutil.which", return_value=None):
            with self.assertRaises(VideoProcessingUnavailable):
                normalize_video_upload(SimpleUploadedFile("clip.mov", b"test"), max_size=100)

    def test_timeout_decode_error_and_missing_binary_are_safe_errors(self):
        for error, expected, text in (
            (subprocess.TimeoutExpired("tool", 45), ValidationError, "处理超时"),
            (subprocess.CalledProcessError(1, "tool"), ValidationError, "无法解码"),
            (FileNotFoundError("private server path"), VideoProcessingUnavailable, "暂不可用"),
        ):
            with self.subTest(error=error), patch("subprocess.run", side_effect=error):
                with self.assertRaisesMessage(expected, text):
                    _run(["tool"], timeout=1)

    def test_probe_rejects_invalid_duration_cover_art_and_oversize(self):
        normal = {"index": 0, "codec_type": "video", "width": 320, "height": 180, "duration": "1"}
        cases = [b"invalid-json"]
        for changes in ({"duration": "nan"}, {"duration": "inf"}, {"duration": "0"},
                        {"width": 16384}, {"disposition": {"attached_pic": 1}}):
            cases.append(json.dumps({"streams": [normal | changes]}).encode())
        for raw in cases:
            with self.subTest(raw=raw), patch("mediafiles.video_normalization._run", return_value=raw):
                with self.assertRaises(ValidationError):
                    _probe("ffprobe", Path("synthetic.mov"))

    def test_hdr_is_tone_mapped_but_sdr_is_not(self):
        for transfer in ("arib-std-b67", "smpte2084"):
            self.assertIn("tonemap=", _filters({"color_transfer": transfer}))
        self.assertNotIn("tonemap=", _filters({"color_transfer": "bt709"}))

    def test_temp_files_cleaned_on_transcode_failure(self):
        paths = []
        def fail(args, **kwargs):
            paths.append(Path(args[-1]).parent)
            raise ValidationError({"file": "synthetic failure"})
        source = SimpleUploadedFile("phone.mov", b"synthetic")
        with patch("mediafiles.video_normalization.shutil.which", side_effect=lambda name: name), patch(
            "mediafiles.video_normalization._probe", return_value=({"index": 0}, 1, []),
        ), patch("mediafiles.video_normalization._run", side_effect=fail):
            with self.assertRaises(ValidationError):
                normalize_video_upload(source, max_size=1024)
        self.assertEqual(source.tell(), 0)
        self.assertTrue(paths)
        self.assertFalse(paths[0].exists())

    def test_incomplete_output_is_not_published_and_temp_files_are_removed(self):
        paths = []
        def transcode(args, **kwargs):
            target = Path(args[-1])
            paths.append(target.parent)
            target.write_bytes(b"synthetic shortened output")
        source = SimpleUploadedFile("clip.mov", b"synthetic")
        output = {"index": 0, "codec_name": "h264", "pix_fmt": "yuv420p"}
        with patch("mediafiles.video_normalization.shutil.which", side_effect=lambda name: name), patch(
            "mediafiles.video_normalization._probe", side_effect=[({"index": 0}, 10, []), (output, 1, [])],
        ), patch("mediafiles.video_normalization._run", side_effect=transcode):
            with self.assertRaisesMessage(ValidationError, "转换结果不完整"):
                normalize_video_upload(source, max_size=1024)
        self.assertEqual(source.tell(), 0)
        self.assertFalse(paths[0].exists())


class VideoCodecIntegrationTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ffmpeg = shutil.which(settings.MEDIA_FFMPEG_PATH)
        cls.ffprobe = shutil.which(settings.MEDIA_FFPROBE_PATH)
        if not cls.ffmpeg or not cls.ffprobe:
            raise SkipTest("Install FFmpeg/FFprobe or set MEDIA_FFMPEG_PATH / MEDIA_FFPROBE_PATH")
        cls.folder = tempfile.TemporaryDirectory(prefix="synthetic-video-test-")
        cls.addClassCleanup(cls.folder.cleanup)
        cls.root = Path(cls.folder.name)
        cls.clips = {}
        for name, codec, hdr, audio in (
            ("h264", "libx264", False, True),
            ("hevc", "libx265", False, True),
            ("hdr", "libx265", True, False),
        ):
            target = cls.root / f"{name}.mov"
            args = [cls.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=red:size=320x180:rate=15"]
            if audio:
                args += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
            args += ["-t", "1", "-c:v", codec, "-threads", "1", "-preset", "ultrafast",
                     "-pix_fmt", "yuv420p10le" if hdr else "yuv420p", "-c:a", "aac",
                     "-metadata", "location=+00.0000+000.0000/"]
            if codec == "libx265":
                params = "pools=1:frame-threads=1:log-level=error"
                if hdr:
                    params += ":colorprim=9:transfer=18:colormatrix=9"
                args += ["-x265-params", params, "-tag:v", "hvc1"]
            if hdr:
                args += ["-color_primaries", "bt2020", "-color_trc", "arib-std-b67", "-colorspace", "bt2020nc"]
            _run([*args, str(target)], timeout=30)
            if hdr:
                info, _, _ = _probe(cls.ffprobe, target)
                assert info.get("color_transfer") == "arib-std-b67", "Fixture must really be HLG HDR"
            cls.clips[name] = target.read_bytes()
        # Inject a real QuickTime track display matrix. FFmpeg CLI syntax for
        # writing this metadata changed across versions; the MOV format did not.
        rotated = bytearray(cls.clips["h264"])
        payload = rotated.index(b"tkhd") + 4
        matrix_offset = payload + (40 if rotated[payload] == 0 else 52)
        matrix = (0, -65536, 0, 65536, 0, 0, 0, 0, 1 << 30)
        rotated[matrix_offset:matrix_offset + 36] = b"".join(n.to_bytes(4, "big", signed=True) for n in matrix)
        cls.clips["rotated"] = bytes(rotated)

    def test_h264_hevc_hdr_and_rotated_mov_become_playable_mp4(self):
        for name, payload in self.clips.items():
            with self.subTest(name=name):
                original = SimpleUploadedFile("wx_temp", payload, "application/octet-stream")
                converted = normalize_video_upload(original, max_size=50 * 1024 * 1024)
                temp_path = Path(converted.temporary_file_path())
                try:
                    self.assertEqual(converted.content_type, "video/mp4")
                    self.assertEqual(converted.tell(), 0)
                    self.assertEqual(original.tell(), 0)
                    details = json.loads(_run([self.ffprobe, "-v", "error", "-show_streams", "-show_format",
                                              "-of", "json", str(temp_path)], timeout=8, capture=True))
                    video = next(s for s in details["streams"] if s["codec_type"] == "video")
                    self.assertEqual(video["codec_name"], "h264")
                    self.assertEqual(video["pix_fmt"], "yuv420p")
                    self.assertEqual((video["width"], video["height"]), (180, 320) if name == "rotated" else (320, 180))
                    self.assertTrue(all(not side.get("rotation") for side in video.get("side_data_list", [])))
                    self.assertAlmostEqual(float(video["duration"]), 1, delta=0.1)
                    for part in [details["format"], *details["streams"]]:
                        self.assertFalse(any("location" in key for key in part.get("tags", {})))
                    audio = [s for s in details["streams"] if s["codec_type"] == "audio"]
                    self.assertEqual(len(audio), 0 if name == "hdr" else 1)
                    if audio:
                        self.assertEqual(audio[0]["codec_name"], "aac")
                    if name == "hdr":
                        self.assertEqual(video.get("color_transfer"), "bt709")
                        self.assertEqual(video.get("color_space"), "bt709")
                        pixel = _run([self.ffmpeg, "-v", "error", "-i", str(temp_path), "-vf", "scale=1:1",
                                      "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
                                     timeout=10, capture=True)
                        self.assertEqual(len(pixel), 3)
                        self.assertGreater(pixel[0], max(pixel[1:]) + 30, "HDR red must not become gray or black")
                    # Fully decode the result, not just container metadata.
                    _run([self.ffmpeg, "-v", "error", "-xerror", "-i", str(temp_path),
                          "-f", "null", "-"], timeout=10)
                finally:
                    converted.close()
                self.assertFalse(temp_path.exists())

    def test_rejects_fake_frames_that_pass_structural_checks(self):
        original = SimpleUploadedFile("bad.mov", video_container(), "video/quicktime")
        with self.assertRaises(ValidationError):
            normalize_video_upload(original, max_size=50 * 1024 * 1024)

    def test_real_ten_second_limit(self):
        for seconds in (10, 10.1):
            with self.subTest(seconds=seconds):
                target = self.root / f"boundary-{seconds}.mp4"
                _run([self.ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
                      "color=c=blue:size=64x64:rate=30", "-t", str(seconds),
                      "-c:v", "libx264", "-threads", "1", str(target)], timeout=15)
                source = SimpleUploadedFile("test.mp4", target.read_bytes(), "video/mp4")
                if seconds > 10:
                    with self.assertRaisesMessage(ValidationError, "不能超过10秒"):
                        normalize_video_upload(source, max_size=50 * 1024 * 1024)
                else:
                    prepared = normalize_video_upload(source, max_size=50 * 1024 * 1024)
                    try:
                        self.assertEqual(prepared.duration_ms, 10000)
                    finally:
                        prepared.close()

    def test_encoded_size_is_also_limited(self):
        original = SimpleUploadedFile("clip.mov", self.clips["h264"], "video/quicktime")
        with self.assertRaisesMessage(ValidationError, "体积过大"):
            normalize_video_upload(original, max_size=10)

    @patch("mediafiles.views.build_media_url", return_value="https://media.test/converted.mp4")
    def test_full_api_hevc_upload_stores_only_converted_mp4(self, _url):
        user = User.objects.create_user(phone="13977000980")
        ProviderProfile.objects.create(user=user, status=ProviderProfile.Status.APPROVED)
        client = APIClient()
        client.force_authenticate(user)
        stored = []
        def capture(**kwargs):
            body = kwargs["body"]
            stored.append((body, body.read(), kwargs["content_type"], body.temporary_file_path()))
            return "converted-etag"
        with patch("mediafiles.views.upload_public_stream", side_effect=capture):
            response = client.post("/api/v1/media/provider-videos/", {
                "file": SimpleUploadedFile("phone.MOV", self.clips["hevc"], "image/jpeg"),
            }, format="multipart")
        self.assertEqual(response.status_code, 201, response.data)
        asset = MediaAsset.objects.get(pk=response.data["data"]["id"])
        self.assertEqual(asset.content_type, "video/mp4")
        self.assertEqual(asset.duration_ms, 1000)
        self.assertEqual(asset.size_bytes, len(stored[0][1]))
        self.assertTrue(asset.object_key.endswith(".mp4"))
        self.assertNotEqual(stored[0][1], self.clips["hevc"])
        self.assertTrue(stored[0][0].closed)
        self.assertFalse(Path(stored[0][3]).exists())

    @override_settings(MEDIA_FFMPEG_PATH="missing-ffmpeg-do-not-install")
    @patch("mediafiles.views.upload_public_stream")
    def test_api_missing_processor_stores_nothing(self, upload):
        user = User.objects.create_user(phone="13977000981")
        ProviderProfile.objects.create(user=user, status=ProviderProfile.Status.APPROVED)
        client = APIClient()
        client.force_authenticate(user)
        response = client.post("/api/v1/media/provider-videos/", {
            "file": SimpleUploadedFile("clip.mov", self.clips["hevc"], "video/quicktime"),
        }, format="multipart")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(MediaAsset.objects.exists())
        upload.assert_not_called()
