"""Bounded ISO-BMFF container checks; no subprocesses or full-file buffering."""

from rest_framework.exceptions import ValidationError


def validate_provider_video(uploaded):
    def boxes(start, end):
        offset = start
        count = 0
        while offset < end:
            count += 1
            if count > 10000 or end - offset < 8:
                raise ValueError("Invalid box count/header")
            uploaded.seek(offset)
            header = uploaded.read(8)
            size, kind = int.from_bytes(header[:4], "big"), header[4:]
            header_size = 8
            if size == 1:
                size = int.from_bytes(uploaded.read(8), "big")
                header_size = 16
            elif size == 0:
                size = end - offset
            if size < header_size or offset + size > end:
                raise ValueError("Truncated video")
            yield kind, offset + header_size, offset + size
            offset += size

    try:
        brand = None
        has_data = has_video = False
        for kind, start, end in boxes(0, uploaded.size):
            if kind == b"ftyp":
                if end - start < 8 or end - start > 4096:
                    raise ValueError("Invalid file type")
                uploaded.seek(start)
                payload = uploaded.read(end - start)
                brands = [payload[:4]] + [payload[i : i + 4] for i in range(8, len(payload), 4)]
                allowed = {
                    b"isom",
                    b"iso2",
                    b"iso4",
                    b"iso5",
                    b"iso6",
                    b"mp41",
                    b"mp42",
                    b"avc1",
                    b"M4V ",
                    b"qt  ",
                }
                if not any(value in allowed for value in brands):
                    raise ValueError("Unsupported brand")
                brand = "video/quicktime" if payload[:4] == b"qt  " else "video/mp4"
            elif kind == b"mdat" and end > start:
                has_data = True
            elif kind == b"moov":
                for track, ts, te in boxes(start, end):
                    if track != b"trak":
                        continue
                    for media, ms, me in boxes(ts, te):
                        if media != b"mdia":
                            continue
                        for handler, hs, he in boxes(ms, me):
                            if handler == b"hdlr" and he - hs >= 12:
                                uploaded.seek(hs + 8)
                                has_video |= uploaded.read(4) == b"vide"
        if not (brand and has_data and has_video):
            raise ValueError("Missing video track or media data")
        uploaded.content_type = brand
    except (ValueError, OSError, OverflowError) as exc:
        raise ValidationError({"file": "视频文件无效，请选择包含画面的 MP4 或 MOV 视频。"}) from exc
    finally:
        uploaded.seek(0)
