"""Official SDK for bill metadata. HTTPS download is for signed file URLs only."""
import csv
import hashlib
import io
import ipaddress
import re
import socket
import uuid
import zipfile
from datetime import timedelta
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import requests
from django.conf import settings
from django.utils import timezone

from orders.distribution_transport import sdk_call
from orders.huifu import _canonical_digest

MAX_DOWNLOAD = 20 * 1024 * 1024
MAX_UNPACKED = 100 * 1024 * 1024
MAX_ROWS = 200000


class BillError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)  # Never include request/URL/source data.


class BillPending(BillError):
    pass


def download_bill(url):
    """No redirects, credentials, internal destinations, file writes or URL logs."""
    try:
        parsed = urlsplit(url)
        allowed = settings.HUIFU_RECONCILIATION_DOWNLOAD_HOSTS
        if (parsed.scheme != "https" or parsed.hostname not in allowed
                or parsed.port not in (None, 443) or parsed.username or parsed.password
                or parsed.fragment or len(url) > 4096):
            raise ValueError
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise ValueError
        # Only explicit official hosts, never a wildcard/user-provided endpoint.
        with requests.Session() as session:
            session.trust_env = False
            with session.get(url, timeout=(10, 30), stream=True, allow_redirects=False) as response:
                if response.status_code != 200:
                    raise ValueError
                content = bytearray()
                for chunk in response.iter_content(65536):
                    content.extend(chunk)
                    if len(content) > MAX_DOWNLOAD:
                        raise ValueError
                return bytes(content)
    except Exception:
        raise BillError("download_failed") from None


def fetch_bill(config, data_date, bill_type):
    seq, req_date = uuid.uuid4().hex, timezone.localdate().strftime("%Y%m%d")
    file_date = (data_date + timedelta(days=1)).strftime("%Y%m%d")
    try:
        data = sdk_call("bill_query", {
            "req_date": req_date, "req_seq_id": seq, "huifu_id": config.merchant_id,
            "file_date": file_date, "bill_type": bill_type,
        }, config)
    except Exception:
        raise BillError("channel_unverified") from None
    if (data.get("resp_code") != "00000000" or data.get("req_date") != req_date
            or data.get("req_seq_id") != seq):
        raise BillError("channel_unverified")
    files, tasks = data.get("file_details") or [], data.get("task_details") or []
    if not isinstance(files, list) or not isinstance(tasks, list) or max(len(files), len(tasks)) > 20:
        raise BillError("metadata_invalid")
    for task in tasks:
        if (not isinstance(task, dict) or task.get("huifu_id") != config.merchant_id
                or task.get("bill_type") != bill_type or task.get("data_date") != data_date.strftime("%Y%m%d")):
            raise BillError("metadata_invalid")
        if task.get("task_stat") != "S":
            # Some files can be available while another part is still generating.
            raise BillPending("bill_not_ready")
    if not files:
        # Empty HTTP success is not evidence that no transactions occurred.
        if tasks and all(
            isinstance(t, dict) and t.get("huifu_id") == config.merchant_id
            and t.get("bill_type") == bill_type and t.get("data_date") == data_date.strftime("%Y%m%d")
            and t.get("task_stat") == "S" and not t.get("download_url") and not t.get("file_id") for t in tasks
        ):
            return [], _canonical_digest(data)
        raise BillPending("bill_not_ready")
    blobs, identifiers = [], set()
    for item in files:
        if not isinstance(item, dict):
            raise BillError("metadata_invalid")
        name = item.get("file_name") or item.get("file_Name")
        if (item.get("huifu_id") != config.merchant_id or item.get("file_date") != file_date
                or item.get("bill_type") != bill_type or not isinstance(name, str)
                or not name.lower().endswith(".zip") or not isinstance(item.get("download_url"), str)):
            raise BillError("metadata_invalid")
        identity = item.get("file_id") or name
        if identity in identifiers:
            raise BillError("metadata_duplicate")
        identifiers.add(identity)
        blobs.append(download_bill(item["download_url"]))
        if sum(map(len, blobs)) > MAX_DOWNLOAD:
            raise BillError("file_limit")
    digest = _canonical_digest([_canonical_digest(data), *[hashlib.sha256(b).hexdigest() for b in blobs]])
    return read_archives(blobs), digest


def read_archives(blobs):
    rows, total, file_count = [], 0, 0
    try:
        for blob in blobs:
            with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                members = archive.infolist()
                if not members:
                    raise ValueError
                for member in members:
                    path = PurePosixPath(member.filename)
                    file_count += 1
                    total += member.file_size
                    if (file_count > 100 or total > MAX_UNPACKED or member.is_dir()
                            or "\\" in member.orig_filename or "\x00" in member.orig_filename
                            or path.is_absolute() or ".." in path.parts
                            or ":" in member.filename or path.suffix.lower() != ".csv"
                            or member.flag_bits & 1 or (member.external_attr >> 16) & 0o170000 == 0o120000
                            or member.file_size > max(member.compress_size, 1) * 200):
                        raise ValueError
                    raw = archive.read(member)
                    try:
                        text = raw.decode("utf-8-sig")
                    except UnicodeDecodeError:
                        text = raw.decode("gb18030")
                    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
                    header = next(reader)
                    header = [value.strip() for value in header]
                    nonempty = [h for h in header if h]
                    if len(header) > 120 or len(nonempty) != len(set(nonempty)) or not nonempty:
                        raise ValueError
                    # Header-only files retain schema evidence; never accept an empty ZIP.
                    rows.append({"__header__": header})
                    for row in reader:
                        if not any(value.strip() for value in row):
                            continue
                        if len(row) > len(header) or len(rows) > MAX_ROWS:
                            raise ValueError
                        rows.append(dict(zip(header, row + [""] * (len(header) - len(row)))))
        return rows
    except Exception:
        raise BillError("file_format") from None


def money(value):
    """Official CSV amounts are yuan; integer/fractional decimals, never floats."""
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,2})?", value.strip()):
        raise BillError("row_amount")
    whole, _, fraction = value.strip().partition(".")
    return int(whole) * 100 + int(fraction.ljust(2, "0"))
