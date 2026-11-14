
import os
import json
import socket
import ipaddress
import yt_dlp

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote
from email.message import Message
import mimetypes
import re
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

PORT = int(os.environ.get("PORT", 8001))


def validate_url(url):
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Please enter a valid HTTP or HTTPS URL.")

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
    return host in ("x.com", "www.x.com", "twitter.com", "www.twitter.com")


def get_ytdlp_options():
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

        height = item.get("height") or 0
        bitrate = item.get("tbr") or 0

        videos.append({
            "url": video_url,
            "type": "video/mp4",
            "format_id": item.get("format_id"),
            "width": item.get("width") or 0,
            "height": height,
            "bitrate": bitrate,
            "quality": f"{height}p" if height else "MP4 video",
        })

    videos.sort(
        key=lambda video: (video["height"], video["bitrate"]),
        reverse=True
    )

    return videos


def detect_video(url):
    validate_url(url)

    with yt_dlp.YoutubeDL(get_ytdlp_options()) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        raise ValueError("No video information was returned.")

    videos = extract_video_formats(info)

    return {
        "success": bool(videos),
        "platform": "X" if is_x_url(url) else info.get("extractor_key", "Website"),
        "title": info.get("title") or "Video",
        "text": info.get("description") or "",
        "thumbnail": info.get("thumbnail") or "",
        "duration": info.get("duration"),
        "videos": videos,
        "message": (
            f"Found {len(videos)} public MP4 option(s)."
            if videos else
            "No publicly accessible direct MP4 formats were found."
        ),
    }


class VideoGrabberHandler(SimpleHTTPRequestHandler):

    def send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def handle_detection(self, url):
        return detect_video(url)

    def handle_download(self, video_url):
        validate_url(video_url)

        # Accept download endpoints such as /download.php as well as .mp4 URLs.
        # The response must identify itself as a video or as a downloadable file.
        request = Request(
            video_url,
            headers={"User-Agent": "Mozilla/5.0"}
        )

        with urlopen(request, timeout=45) as response:
            headers = response.headers
            content_type = headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            disposition = headers.get("Content-Disposition", "")
            disposition_message = Message()
            disposition_message["content-disposition"] = disposition
            filename = disposition_message.get_filename()

            if not filename:
                filename = unquote(os.path.basename(urlparse(response.geturl()).path))

            filename = os.path.basename((filename or "").replace("\\", "/"))
            filename = re.sub(r'[^A-Za-z0-9._ -]', "_", filename).strip(" .")
            if not filename:
                filename = "Video-Grabber"

            guessed_type, _ = mimetypes.guess_type(filename)
            is_video = content_type.startswith("video/")
            is_binary_download = (
                content_type == "application/octet-stream"
                or disposition.lower().startswith("attachment")
            )

            # Avoid saving an HTML login/error page as if it were a video.
            video_extensions = (
                ".mp4", ".m4v", ".mov", ".webm", ".mkv",
                ".avi", ".mpeg", ".mpg", ".wmv", ".flv"
            )
            filename_looks_like_video = filename.lower().endswith(video_extensions)
            if not is_video and not (
                is_binary_download and (filename_looks_like_video or guessed_type and guessed_type.startswith("video/"))
            ):
                raise ValueError(
                    "The link did not return a recognizable video download. "
                    "It may require sign-in, or the server may have returned a webpage."
                )

            output_type = content_type if is_video else (guessed_type or "application/octet-stream")

            self.send_response(200)
            self.send_header("Content-Type", output_type)
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{filename}"'
            )
            self.send_header("Cache-Control", "no-store")

            content_length = headers.get("Content-Length")
            if content_length:
                self.send_header("Content-Length", content_length)

            self.end_headers()

            while True:
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path == "/detect":
            url = params.get("url", [""])[0].strip()

            try:
                if not url:
                    raise ValueError("Please provide a video URL.")

                self.send_json(self.handle_detection(url))

            except Exception as error:
                self.send_json({
                    "success": False,
                    "message": str(error),
                    "videos": [],
                }, 400)

            return

        if parsed.path == "/download":
            video_url = params.get("url", [""])[0].strip()

            try:
                if not video_url:
                    raise ValueError("Please provide a video URL.")

                self.handle_download(video_url)

            except Exception as error:
                if not self.wfile.closed:
                    self.send_json({
                        "success": False,
                        "message": str(error),
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
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            url = (data.get("url") or "").strip()

            if not url:
                raise ValueError("Please provide a video URL.")

            self.send_json(self.handle_detection(url))

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
