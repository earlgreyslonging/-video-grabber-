
import json
import re
import socket
import ipaddress
from html import unescape
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import yt_dlp

PORT = int(os.environ.get("PORT", 8001))


def validate_url(url):
    """Allow ordinary public HTTP and HTTPS URLs."""
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise ValueError("Please enter an HTTP or HTTPS website URL.")

    if not parsed.hostname:
        raise ValueError("That URL does not appear to be valid.")

    host = parsed.hostname.lower()

    if host in ("localhost", "localhost.localdomain"):
        raise ValueError("Local addresses are not supported.")

    try:
        addresses = socket.getaddrinfo(host, None)

        for address in addresses:
            ip = address[4][0]

            if not ipaddress.ip_address(ip).is_global:
                raise ValueError(
                    "Private network addresses are not supported."
                )

    except socket.gaierror:
        raise ValueError("Could not find that website.")

    return parsed


def is_x_url(url):
    host = (urlparse(url).hostname or "").lower()

    return (
        host == "x.com"
        or host.endswith(".x.com")
        or host == "twitter.com"
        or host.endswith(".twitter.com")
    )


def extract_video_formats(info):
    """Convert available public MP4 formats into our app format."""

    videos = []
    seen_urls = set()

    for item in info.get("formats") or []:
        video_url = item.get("url")
        video_codec = item.get("vcodec")
        extension = item.get("ext")
        protocol = item.get("protocol", "")

        if not video_url or video_codec == "none":
            continue

        if item.get("has_drm"):
            continue

        if extension != "mp4":
            continue

        if protocol not in ("http", "https"):
            continue

        if video_url in seen_urls:
            continue

        seen_urls.add(video_url)

        width = item.get("width") or 0
        height = item.get("height") or 0
        bitrate = item.get("tbr") or 0

        quality = f"{height}p" if height else "MP4 video"

        videos.append({
            "url": video_url,
            "type": "video/mp4",
            "format_id": item.get("format_id"),
            "width": width,
            "height": height,
            "bitrate": bitrate,
            "quality": quality,
        })

    videos.sort(
        key=lambda video: (
            video.get("height") or 0,
            video.get("bitrate") or 0
        ),
        reverse=True
    )

    return videos


def detect_x(url):
    """Detect publicly accessible X/Twitter video formats."""

    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        raise ValueError("X did not return video information.")

    videos = extract_video_formats(info)

    if not videos:
        return {
            "success": False,
            "platform": "X",
            "message": (
                "The post was recognized, but no publicly accessible "
                "direct MP4 formats were returned."
            ),
            "videos": [],
        }

    return {
        "success": True,
        "platform": "X",
        "post_id": info.get("id"),
        "title": info.get("title") or "",
        "text": info.get("description") or info.get("title") or "",
        "uploader": info.get("uploader") or "",
        "thumbnail": info.get("thumbnail") or "",
        "duration": info.get("duration"),
        "videos": videos,
        "message": f"Found {len(videos)} public MP4 quality option(s).",
    }


def detect_with_ytdlp(url):
    """Detect publicly accessible videos supported by yt-dlp."""

    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        raise ValueError("No video information was returned.")

    videos = extract_video_formats(info)

    return {
        "success": bool(videos),
        "platform": info.get("extractor_key", "Website"),
        "title": info.get("title") or "Video",
        "text": info.get("description") or "",
        "thumbnail": info.get("thumbnail") or "",
        "duration": info.get("duration"),
        "videos": videos,
        "message": (
            f"Found {len(videos)} public MP4 format(s)."
            if videos else
            "Video recognized, but no public direct MP4 formats were found."
        ),
    }


class VideoGrabberHandler(SimpleHTTPRequestHandler):

    def send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")

        self.send_response(status)
        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8"
        )
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Access-Control-Allow-Methods",
            "GET, POST, OPTIONS"
        )
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type"
        )
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/detect":
            params = parse_qs(parsed.query)
            url = params.get("url", [""])[0].strip()

            if not url:
                self.send_json({
                    "success": False,
                    "message": "Please provide a video URL.",
                    "videos": [],
                }, 400)
                return

            try:
                validate_url(url)

                if is_x_url(url):
                    result = detect_x(url)
                else:
                    result = detect_with_ytdlp(url)

                self.send_json(result)

            except Exception as error:
                self.send_json({
                    "success": False,
                    "message": str(error),
                    "videos": [],
                }, 400)

            return

        if parsed.path == "/health":
            self.send_json({
                "success": True,
                "message": "Video Grabber server is running.",
            })
            return

        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path != "/detect":
            self.send_json({
                "success": False,
                "message": "Unknown endpoint.",
            }, 404)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(
                self.rfile.read(length).decode("utf-8")
            )

            url = (data.get("url") or "").strip()

            if not url:
                raise ValueError("Please provide a video URL.")

            validate_url(url)

            if is_x_url(url):
                result = detect_x(url)
            else:
                result = detect_with_ytdlp(url)

            self.send_json(result)

        except Exception as error:
            self.send_json({
                "success": False,
                "message": str(error),
                "videos": [],
            }, 400)


if __name__ == "__main__":
    print(f"Video Grabber server starting on port {PORT}...")
    print("Using yt-dlp for publicly accessible video detection.")

    server = ThreadingHTTPServer(
        ("0.0.0.0", PORT),
        VideoGrabberHandler
    )

    try:
        server.serve_forever()

    except KeyboardInterrupt:
        print("\nStopping Video Grabber server...")
        server.server_close()
