
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



def bdecode(data):
    """Decode the small subset of BitTorrent bencoding needed for metadata."""
    def parse(pos):
        if pos >= len(data):
            raise ValueError("Torrent metadata is truncated.")
        token = data[pos:pos + 1]
        if token == b"i":
            end = data.find(b"e", pos)
            if end < 0:
                raise ValueError("Invalid integer in torrent metadata.")
            return int(data[pos + 1:end]), end + 1
        if token == b"l":
            values, pos = [], pos + 1
            while pos < len(data) and data[pos:pos + 1] != b"e":
                value, pos = parse(pos)
                values.append(value)
            if pos >= len(data):
                raise ValueError("Invalid list in torrent metadata.")
            return values, pos + 1
        if token == b"d":
            values, pos = {}, pos + 1
            while pos < len(data) and data[pos:pos + 1] != b"e":
                key, pos = parse(pos)
                value, pos = parse(pos)
                values[key] = value
            if pos >= len(data):
                raise ValueError("Invalid dictionary in torrent metadata.")
            return values, pos + 1
        if token.isdigit():
            colon = data.find(b":", pos)
            if colon < 0:
                raise ValueError("Invalid byte string in torrent metadata.")
            length = int(data[pos:colon])
            start, end = colon + 1, colon + 1 + length
            if end > len(data):
                raise ValueError("Torrent metadata is truncated.")
            return data[start:end], end
        raise ValueError("The URL did not return a valid .torrent file.")

    result, end = parse(0)
    if end != len(data) or not isinstance(result, dict) or not isinstance(result.get(b"info"), dict):
        raise ValueError("The response is not a complete .torrent file.")
    return result


def torrent_metadata(data):
    decoded = bdecode(data)
    info = decoded[b"info"]
    raw_name = info.get(b"name.utf-8") or info.get(b"name") or b"Unnamed torrent"
    name = raw_name.decode("utf-8", "replace") if isinstance(raw_name, bytes) else "Unnamed torrent"
    files = []
    if isinstance(info.get(b"files"), list):
        for item in info[b"files"]:
            if not isinstance(item, dict):
                continue
            raw_path = item.get(b"path.utf-8") or item.get(b"path") or []
            parts = [p.decode("utf-8", "replace") if isinstance(p, bytes) else str(p) for p in raw_path]
            files.append({"path": "/".join(parts), "size": int(item.get(b"length", 0))})
    else:
        files.append({"path": name, "size": int(info.get(b"length", 0))})
    total = sum(max(0, f["size"]) for f in files)
    return {"name": name, "file_count": len(files), "total_bytes": total, "files": files}


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
    all_formats = info.get("formats") or []
    drm_detected = bool(info.get("has_drm") or any(item.get("has_drm") for item in all_formats))
    protocols = sorted({str(item.get("protocol") or "").lower() for item in all_formats if item.get("protocol")})
    extensions = {str(item.get("ext") or "").lower() for item in all_formats}
    manifest = str(info.get("manifest_url") or "").lower()
    stream_type = (
        "HLS" if any("m3u8" in value for value in protocols) or "m3u8" in extensions or ".m3u8" in manifest
        else "MPEG-DASH" if any("dash" in value for value in protocols) or "mpd" in extensions or ".mpd" in manifest
        else "Direct/other"
    )

    if videos:
        message = f"Found {len(videos)} accessible direct MP4 option(s)."
    elif drm_detected:
        message = "A video stream was found, but it is marked DRM-protected. Video Grabber can identify it but cannot download or decrypt the protected stream."
    elif stream_type in ("HLS", "MPEG-DASH"):
        message = f"A {stream_type} stream was found, but no downloadable, unprotected direct MP4 format was exposed."
    else:
        message = "The page was recognized, but no publicly accessible direct MP4 formats were found. It may require sign-in or use an unsupported player."

    return {
        "success": bool(videos),
        "platform": "X" if is_x_url(url) else info.get("extractor_key", "Website"),
        "title": info.get("title") or "Video",
        "text": info.get("description") or "",
        "thumbnail": info.get("thumbnail") or "",
        "duration": info.get("duration"),
        "videos": videos,
        "stream_type": stream_type,
        "drm_detected": drm_detected,
        "formats_checked": len(all_formats),
        "protocols": protocols,
        "message": message,
    }


def analyze_stream(url):
    """Inspect accessible stream metadata without retrieving DRM licenses or keys."""
    validate_url(url)
    with yt_dlp.YoutubeDL(get_ytdlp_options()) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise ValueError("No stream metadata was returned. The page may require sign-in or may not expose a supported manifest.")
    formats = info.get("formats") or []
    protocols = sorted({str(item.get("protocol") or "").lower() for item in formats if item.get("protocol")})
    extensions = {str(item.get("ext") or "").lower() for item in formats}
    manifest = str(info.get("manifest_url") or "").lower()
    stream_type = "HLS" if any("m3u8" in p for p in protocols) or "m3u8" in extensions or ".m3u8" in manifest else ("MPEG-DASH" if any("dash" in p for p in protocols) or "mpd" in extensions or ".mpd" in manifest else "Direct/other")
    drm_formats = [item for item in formats if item.get("has_drm")]
    protected = bool(info.get("has_drm") or drm_formats)
    video_formats, audio_formats = [], []
    for item in formats:
        row = {"format_id": str(item.get("format_id") or ""), "ext": str(item.get("ext") or ""), "protocol": str(item.get("protocol") or ""), "height": int(item.get("height") or 0), "width": int(item.get("width") or 0), "language": str(item.get("language") or ""), "has_drm": bool(item.get("has_drm"))}
        if item.get("vcodec") != "none" and item.get("vcodec"):
            video_formats.append(row)
        elif item.get("acodec") != "none" and item.get("acodec"):
            audio_formats.append(row)
    return {"success": True, "title": str(info.get("title") or "Unknown stream"), "extractor": str(info.get("extractor_key") or "Website"), "stream_type": stream_type, "protection": "DRM detected" if protected else "No DRM flag reported by extractor", "drm_detected": protected, "formats_checked": len(formats), "drm_format_count": len(drm_formats), "protocols": protocols, "video_formats": video_formats[:40], "audio_formats": audio_formats[:40], "subtitle_languages": sorted((info.get("subtitles") or {}).keys()), "automatic_caption_languages": sorted((info.get("automatic_captions") or {}).keys()), "message": "This stream is marked as DRM-protected. The analyzer will not request license keys or attempt decryption." if protected else "No DRM flag was reported. This is not proof that the stream is unprotected; the service may restrict access separately."}



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



    def handle_torrent_info(self, torrent_url):
        validate_url(torrent_url)
        request = Request(torrent_url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(request, timeout=45) as response:
            validate_url(response.geturl())
            data = response.read(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024:
                raise ValueError("The .torrent file is larger than the 8 MB safety limit.")
            return torrent_metadata(data)

    def handle_torrent_download(self, torrent_url):
        validate_url(torrent_url)

        request = Request(
            torrent_url,
            headers={"User-Agent": "Mozilla/5.0"}
        )

        with urlopen(request, timeout=45) as response:
            # Validate the final destination after redirects as well.
            validate_url(response.geturl())
            data = response.read(8 * 1024 * 1024 + 1)

            if len(data) > 8 * 1024 * 1024:
                raise ValueError("The .torrent file is larger than the 8 MB safety limit.")

            # A .torrent is a bencoded dictionary containing an info dictionary.
            if not data.startswith(b"d") or b"4:info" not in data:
                raise ValueError(
                    "The server did not return a valid-looking .torrent file. "
                    "It may have returned a webpage, an error, or a different file."
                )

            disposition = response.headers.get("Content-Disposition", "")
            disposition_message = Message()
            disposition_message["content-disposition"] = disposition
            filename = disposition_message.get_filename()
            if not filename:
                filename = unquote(os.path.basename(urlparse(response.geturl()).path))
            filename = os.path.basename((filename or "download.torrent").replace("\\", "/"))
            filename = re.sub(r'[^A-Za-z0-9._ -]', "_", filename).strip(" .")
            if not filename:
                filename = "download.torrent"
            if not filename.lower().endswith(".torrent"):
                filename += ".torrent"

            self.send_response(200)
            self.send_header("Content-Type", "application/x-bittorrent")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

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

        if parsed.path == "/analyze-stream":
            stream_url = params.get("url", [""])[0].strip()
            try:
                if not stream_url:
                    raise ValueError("Please provide a stream or webpage URL.")
                self.send_json(analyze_stream(stream_url))
            except Exception as error:
                self.send_json({"success": False, "message": str(error), "drm_detected": None}, 400)
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



        if parsed.path == "/torrent-info":
            torrent_url = params.get("url", [""])[0].strip()
            try:
                if not torrent_url:
                    raise ValueError("Please provide a .torrent download URL.")
                self.send_json({"success": True, **self.handle_torrent_info(torrent_url)})
            except Exception as error:
                self.send_json({"success": False, "message": str(error)}, 400)
            return

        if parsed.path == "/torrent":
            torrent_url = params.get("url", [""])[0].strip()
            try:
                if not torrent_url:
                    raise ValueError("Please provide a .torrent download URL.")
                self.handle_torrent_download(torrent_url)
            except Exception as error:
                if not self.wfile.closed:
                    self.send_json({"success": False, "message": str(error)}, 400)
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
