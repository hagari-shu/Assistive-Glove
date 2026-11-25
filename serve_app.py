import json
import os
import re
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 8000
OUTPUT_FORMAT = "audio-24khz-48kbitrate-mono-mp3"
MAX_TEXT_LENGTH = 500
REGION_PATTERN = re.compile(r"^[a-z0-9-]+$")
FILIPINO_VOICE_PATTERN = re.compile(r"^fil-PH-[A-Za-z0-9-]+Neural$")


def azure_config():
    key = os.environ.get("AZURE_SPEECH_KEY", "").strip()
    region = os.environ.get("AZURE_SPEECH_REGION", "").strip().lower()
    if not key or not REGION_PATTERN.fullmatch(region):
        return None
    return key, region


def azure_url(region, path):
    return f"https://{region}.tts.speech.microsoft.com/cognitiveservices/{path}"


def request_azure(url, key, method="GET", body=None, headers=None):
    request_headers = {"Ocp-Apim-Subscription-Key": key}
    if headers:
        request_headers.update(headers)
    request = Request(url, data=body, headers=request_headers, method=method)
    return urlopen(request, timeout=30)


class SmartGloveHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):
        if self.path == "/api/tts-voices":
            self.get_tts_voices()
            return
        super().do_GET()

    def do_POST(self):
        if self.path != "/api/tts":
            self.send_json(404, {"error": "Not found."})
            return
        if self.headers.get("Origin") not in (None, f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"):
            self.send_json(403, {"error": "Requests must come from the local Smart Glove viewer."})
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            self.send_json(415, {"error": "Use application/json."})
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(400, {"error": "Invalid content length."})
            return
        if content_length <= 0 or content_length > 8192:
            self.send_json(413, {"error": "Request body must be between 1 and 8192 bytes."})
            return
        try:
            payload = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_json(400, {"error": "Request body must be valid JSON."})
            return
        if not isinstance(payload, dict):
            self.send_json(400, {"error": "Request body must be a JSON object."})
            return

        text = payload.get("text")
        voice = payload.get("voice")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_LENGTH:
            self.send_json(400, {"error": f"Speech text must contain 1 to {MAX_TEXT_LENGTH} characters."})
            return
        if not isinstance(voice, str) or not FILIPINO_VOICE_PATTERN.fullmatch(voice):
            self.send_json(400, {"error": "Choose a Filipino neural voice returned by the voice list."})
            return

        config = azure_config()
        if not config:
            self.send_json(503, {"error": "Azure Speech is not configured. Set AZURE_SPEECH_KEY and AZURE_SPEECH_REGION, then restart the local server."})
            return
        key, region = config
        ssml = (
            '<speak version="1.0" xml:lang="fil-PH">'
            f'<voice name="{escape(voice)}"><prosody rate="-5%" pitch="+1%">'
            f'{escape(text.strip())}'
            '</prosody></voice></speak>'
        ).encode("utf-8")
        try:
            with request_azure(
                azure_url(region, "v1"),
                key,
                method="POST",
                body=ssml,
                headers={
                    "Content-Type": "application/ssml+xml",
                    "X-Microsoft-OutputFormat": OUTPUT_FORMAT,
                },
            ) as response:
                audio = response.read()
        except HTTPError as error:
            message = "Azure Speech rejected the request. Check the resource key, region, and voice."
            self.send_json(502, {"error": f"{message} (HTTP {error.code})."})
            return
        except (URLError, TimeoutError, OSError) as error:
            self.send_json(502, {"error": f"Could not reach Azure Speech: {error}."})
            return

        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(audio)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(audio)

    def get_tts_voices(self):
        config = azure_config()
        if not config:
            self.send_json(503, {"error": "Azure Speech is not configured. Set AZURE_SPEECH_KEY and AZURE_SPEECH_REGION, then restart the local server."})
            return
        key, region = config
        try:
            with request_azure(azure_url(region, "voices/list"), key) as response:
                voices = json.loads(response.read())
        except HTTPError as error:
            self.send_json(502, {"error": f"Azure Speech voice lookup failed (HTTP {error.code}); check the resource key and region."})
            return
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            self.send_json(502, {"error": f"Could not retrieve Azure Filipino voices: {error}."})
            return
        filipino_voices = [
            {
                "shortName": voice["ShortName"],
                "name": voice.get("LocalName", voice["ShortName"]),
                "gender": voice.get("Gender", "Voice"),
            }
            for voice in voices
            if voice.get("Locale") == "fil-PH"
            and voice.get("VoiceType") == "Neural"
            and isinstance(voice.get("ShortName"), str)
            and FILIPINO_VOICE_PATTERN.fullmatch(voice["ShortName"])
        ]
        filipino_voices.sort(key=lambda voice: (voice["gender"], voice["name"]))
        self.send_json(200, {"voices": filipino_voices})

    def send_json(self, status, payload):
        content = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)


if __name__ == "__main__":
    print(f"Smart Glove viewer: http://{HOST}:{PORT}")
    if not azure_config():
        print("Online Filipino speech is disabled until AZURE_SPEECH_KEY and AZURE_SPEECH_REGION are set.")
    ThreadingHTTPServer((HOST, PORT), SmartGloveHandler).serve_forever()
