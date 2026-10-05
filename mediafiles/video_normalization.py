"""Decode phone videos locally and store a portable, metadata-free MP4.

Only validated MOV/MP4 containers reach this module. External track references
are disabled, subprocesses have time/resource bounds, and originals are never
published. Keep the total processing budget below the API worker timeout.
"""

import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from django.conf import settings
from django.core.files.uploadedfile import TemporaryUploadedFile
from rest_framework.exceptions import APIException, ValidationError


class VideoProcessingUnavailable(APIException):
    status_code = 503
    default_detail = "视频处理服务暂不可用，请稍后重试或联系客服。"
    default_code = "video_processing_unavailable"


INPUT_OPTIONS = ["-protocol_whitelist", "file", "-f", "mov", "-enable_drefs", "0", "-use_absolute_path", "0"]
INVALID_VIDEO = "视频无法解码，请重新选择或导出为 MP4/MOV 后重试。"
PROBE_TIMEOUT = 8
TRANSCODE_TIMEOUT = 45
MAX_PIXELS = 4096 * 4096
MAX_VIDEO_SECONDS = 10


def _run(args, *, timeout, capture=False):
    try:
        # Never invoke a shell or log file contents, names, GPS or decoder output.
        with tempfile.TemporaryFile() as output:
            subprocess.run(
                args, stdin=subprocess.DEVNULL,
                stdout=output if capture else subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=timeout, check=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if capture:
                output.seek(0)
                data = output.read(1024 * 1024 + 1)
                if len(data) > 1024 * 1024:
                    raise ValidationError({"file": INVALID_VIDEO})
                return data
    except FileNotFoundError as exc:
        raise VideoProcessingUnavailable() from exc
    except subprocess.TimeoutExpired as exc:
        raise ValidationError({"file": "视频处理超时，请裁剪或压缩视频后重试。"}) from exc
    except subprocess.CalledProcessError as exc:
        raise ValidationError({"file": INVALID_VIDEO}) from exc
    except OSError as exc:
        raise VideoProcessingUnavailable() from exc


def _probe(executable, path):
    raw = _run([
        executable, "-v", "error", "-max_alloc", "134217728", "-threads", "2",
        *INPUT_OPTIONS, "-i", str(path), "-show_entries",
        "stream=index,codec_type,codec_name,pix_fmt,width,height,duration,color_transfer:"
        "stream_disposition=attached_pic:format=duration",
        "-of", "json",
    ], timeout=PROBE_TIMEOUT, capture=True)
    try:
        data = json.loads(raw)
        streams = data["streams"]
        video = next(item for item in streams if item.get("codec_type") == "video"
                     and not item.get("disposition", {}).get("attached_pic"))
        width, height = int(video["width"]), int(video["height"])
        if min(width, height) < 2 or max(width, height) > 8192 or width * height > MAX_PIXELS:
            raise ValidationError({"file": "视频分辨率过大，请先压缩至 4K 或更低分辨率。"})
        duration = float(video.get("duration", data.get("format", {}).get("duration", 0)))
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("Invalid duration")
        return video, duration, streams
    except (ValueError, TypeError, KeyError, StopIteration, AttributeError) as exc:
        raise ValidationError({"file": INVALID_VIDEO}) from exc


def _filters(video):
    # Rotation is applied by FFmpeg before filters; fit landscape AND portrait
    # into a 1080p display envelope without upscaling. Even sizes suit yuv420p.
    filters = [
        "scale=w='trunc(iw*min(1,min(1920/max(iw,ih),1080/min(iw,ih)))/2)*2':"
        "h='trunc(ih*min(1,min(1920/max(iw,ih),1080/min(iw,ih)))/2)*2'",
        "setsar=1", "fps=30",
    ]
    if video.get("color_transfer") in ("smpte2084", "arib-std-b67"):
        # iPhone HDR / Dolby Vision's compatible HLG or PQ base layer -> SDR.
        filters += ["zscale=t=linear:npl=100", "format=gbrpf32le", "zscale=p=bt709",
                    "tonemap=tonemap=hable:desat=0", "zscale=t=bt709:m=bt709:r=tv"]
    filters.append("format=yuv420p")
    return ",".join(filters)


def normalize_video_upload(uploaded, *, max_size):
    ffmpeg = shutil.which(getattr(settings, "MEDIA_FFMPEG_PATH", "ffmpeg"))
    ffprobe = shutil.which(getattr(settings, "MEDIA_FFPROBE_PATH", "ffprobe"))
    if not ffmpeg or not ffprobe:
        raise VideoProcessingUnavailable()
    prepared = None
    try:
        with tempfile.TemporaryDirectory(prefix="dazzy-video-") as folder:
            source, target = Path(folder) / "input.mov", Path(folder) / "output.mp4"
            with source.open("wb") as body:
                uploaded.seek(0)
                for chunk in uploaded.chunks():
                    body.write(chunk)
            video, duration, _ = _probe(ffprobe, source)
            if duration > MAX_VIDEO_SECONDS:
                raise ValidationError({"file": "每个视频时长不能超过10秒，请裁剪后重新上传。"})
            color_options = []
            if video.get("color_transfer") in ("smpte2084", "arib-std-b67"):
                color_options = ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv"]
            _run([
                ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror", "-y",
                "-max_alloc", "134217728", "-threads", "2", "-filter_threads", "1",
                *INPUT_OPTIONS, "-i", str(source), "-map", f"0:{int(video['index'])}",
                "-map", "0:a:0?", "-map_metadata", "-1", "-map_metadata:s", "-1", "-map_chapters", "-1",
                "-vf", _filters(video), "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "23", "-threads", "2", "-pix_fmt", "yuv420p", *color_options,
                "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "48000",
                "-movflags", "+faststart",
                # Bound temporary disk usage too. Never accept a truncated result.
                "-fs", str(max_size + 1024 * 1024), "-f", "mp4", str(target),
            ], timeout=TRANSCODE_TIMEOUT)
            size = target.stat().st_size
            if size <= 0 or size > max_size:
                raise ValidationError({"file": "视频转换后体积过大，请裁剪或压缩后重试。"})
            result, result_duration, streams = _probe(ffprobe, target)
            if result_duration > MAX_VIDEO_SECONDS:
                raise ValidationError({"file": "视频转换后超过10秒，请稍作裁剪后重新上传。"})
            if (result.get("codec_name") != "h264" or result.get("pix_fmt") != "yuv420p"
                    or abs(result_duration - duration) > max(0.5, duration * 0.001)
                    or any(item.get("codec_name") != "aac" for item in streams if item.get("codec_type") == "audio")):
                raise ValidationError({"file": "视频转换结果不完整，请裁剪或重新导出后重试。"})
            prepared = TemporaryUploadedFile(uploaded.name, "video/mp4", size, None)
            prepared.duration_ms = math.ceil(result_duration * 1000)
            with target.open("rb") as body:
                shutil.copyfileobj(body, prepared)
            prepared.seek(0)
        return prepared
    except Exception as exc:
        if prepared is not None:
            prepared.close()
        if isinstance(exc, OSError):
            raise VideoProcessingUnavailable() from exc
        raise
    finally:
        uploaded.seek(0)
