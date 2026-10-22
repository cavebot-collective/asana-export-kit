"""Download HTTPS attachment bytes without bearer headers or private-network access."""
import http.client
import ipaddress
import os
import socket
import ssl
import urllib.parse
from pathlib import Path
from .core import gid


def destination(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError("Attachment must use HTTPS port 443 without credentials")
    answers = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    ips = [item[4][0] for item in answers]
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise ValueError("Attachment host does not resolve exclusively to public addresses")
    return parsed, ips[0]


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, ip):
        super().__init__(host, timeout=60)
        self.ip = ip

    def connect(self):
        sock = socket.create_connection((self.ip, 443), timeout=self.timeout)
        self.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)


def download_attachment(root, attachment, max_bytes=1024 * 1024 * 1024):
    url = attachment.get("download_url")
    if not url:
        raise ValueError("No downloadable URL; external linked file")
    # IDs, never remote filenames, determine the destination path.
    relative = "attachments/" + gid(attachment["gid"]) + ".bin"
    target = Path(root) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".part")
    try:
        for _ in range(6):
            parsed, ip = destination(url)
            conn = PinnedHTTPS(parsed.hostname, ip)
            try:
                conn.request("GET", urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, "")),
                    headers={"User-Agent": "asana-export-kit/0.1.0", "Accept-Encoding": "identity"})
                response = conn.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    if not location:
                        raise ValueError("Redirect missing Location")
                    url = urllib.parse.urljoin(url, location)
                    continue
                if response.status != 200:
                    raise ValueError("Attachment HTTP " + str(response.status))
                length = response.getheader("Content-Length")
                if length and int(length) > max_bytes:
                    raise ValueError("Attachment exceeds size limit")
                total = 0
                with open(tmp, "wb") as f:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_bytes:
                            raise ValueError("Attachment exceeds size limit")
                        f.write(chunk)
                if length and total != int(length):
                    raise ValueError("Incomplete attachment")
                os.replace(tmp, target)
                return relative
            finally:
                conn.close()
        raise ValueError("Too many attachment redirects")
    finally:
        if tmp.exists():
            tmp.unlink()
