
import os
import json
import socket
import ipaddress
import yt_dlp

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PORT = int(os.environ.get("PORT", 8001))


def validate_url(url):
    """Allow ordinary public HTTP and HTTPS URLs."""
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise ValueError("Please enter an HTTP or HTTPS URL.")

    if not parsed.hostname:
        raise ValueError("That URL is invalid.")

    host = parsed.hostname.lower()

    if host in ("localhost", "localhost.localdomain"):
        raise ValueError("Local addresses are not supported.")

    try:
        addresses = socket.getaddrinfo(host, None)

        for address in addresses:
            ip = address[4][0]

            if not ipaddress.ip_address(ip).is_global:
                raise ValueError("Private network addresses are not supported.")

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


def get_ytdlp_options():
    """Shared settings for public video detection."""
    return {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 45,
        "retries": 2,
        "extractor_retries": 2,
    }


def extract_video_formats(info):
    videos = []
    seen_urls = set()

    for item in info.get("formats") or []:
        video_url = item.get("url")
        video_codec = item.get("vcodec")

        if not video_url or video_codec == "none":
            continue

        if item.get("has_drm"):
            continue

        if item.get("ext") != "mp4":
            continue

        if item.get("protocol") not in ("http", "https"):
            continue

        if video_url in seen_urls:
            continue

        seen_urls.add(video_url)

        width = item.get("width") or 0
        height = item.get("height") or 0
        bitrate = item.get("tbr") or 0

        videos.append({
            "url": video_url,
            "type": "video/mp4",
            "format_id": item.get("format_id"),
            "width": width,
            "height": height,
            "bitrate": bitrate,
            "quality": f"{height}p" if height else "MP4 video",
        })

    videos.sort(
        key=lambda video: (
            video["height"],
            video["bitrate"]
        ),
        reverse=True
    )

    return videos


def detect_x(url):
    with yt_dlp.YoutubeDL(get_ytdlp_options()) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        raise ValueError("X did not return video information.")

    videos = extract_video_formats(info)

    return {
        "success": bool(videos),
        "platform": "X",
        "title": info.get("title") or "",
        "text": info.get("description") or "",
        "thumbnail": info.get("thumbnail") or "",
        "duration": info.get("duration"),
        "videos": videos,
        "message": (
            f"Found {len(videos)} public MP4 option(s)."
            if videos else
            "No publicly accessible direct MP4 formats were returned."
        ),
    }


def detect_with_ytdlp(url):
    with yt_dlp.YoutubeDL(get_ytdlp_options()) as ydl:
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

    def handle_detection(self, url):
        validate_url(url)

        if is_x_url(url):
            return detect_x(url)

        return detect_with_ytdlp(url)

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/detect":
            params = parse_qs(parsed.query)
            url = params.get("url", [""])[0].strip()

            try:
                if not url:
                    raise ValueError("Please provide a video URL.")

                result = self.handle_detection(url)
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
        if urlparse(self.path).path != "/detect":
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

            result = self.handle_detection(url)
            self.send_json(result)

        except Exception as error:
            self.send_json({
                "success": False,
                "message": str(error),
                "videos": [],
            }, 400)


if __name__ == "__main__":
    print(f"Video Grabber server starting on port {PORT}...")

    server = ThreadingHTTPServer(
        ("0.0.0.0", PORT),
        VideoGrabberHandler
    )

    try:
        server.serve_forever()

    except KeyboardInterrupt:
        print("\nStopping Video Grabber server...")
        server.server_close()
