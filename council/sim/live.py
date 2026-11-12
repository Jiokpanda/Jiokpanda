"""생중계 받아쓰기.

의회 생중계(HLS)의 소리를 실시간 음성 인식(Soniox, 게이트웨이 경유)으로 보내 글자로 받고,
지정한 낱말(우리 부서명 등)과 자료 요구 표현이 나온 발언을 골라낸다.

- 영상과 소리는 저장하지 않는다. 받아쓴 글자만 이 프로그램의 메모리에 둔다.
- ffmpeg가 있으면 그것으로 소리를 뽑고, 없으면 파이썬만으로 방송 조각에서 소리를 꺼낸다.
- 파이썬 기본 기능만 사용한다.
"""
import base64
import json
import os
import queue
import re
import shutil
import socket
import ssl
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

LIVE_SITE = os.environ.get("COUNCIL_LIVE_SITE", "https://clive.yongin.go.kr")   # 시험할 때만 바꾼다
REQUEST = re.compile(r"자료[^.\n]{0,40}(제출|주시|주십|주세요|부탁|요청|요구|보내|갖다|가져다)")
MARKERS = ("<end>", "<fin>")          # 음성 인식이 발언 끝에 붙이는 표시
SESSION_SECONDS = 55 * 60             # 게이트웨이의 세션당 60분 상한 전에 스스로 다시 연결한다
KEEP_SEGMENTS = 3000


def http_get(url, tries=4, timeout=20):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as res:
                return res.read()
        except (urllib.error.URLError, OSError) as e:
            last = e
            time.sleep(1 + i)
    raise OSError(f"받지 못함: {url} ({last})")


def channels(site=LIVE_SITE):
    """오늘의 생중계 목록과 각 방송의 스트림 주소를 읽는다."""
    doc = http_get(site + "/").decode("utf-8", "replace")
    out = []
    for cls, path, title, name in re.findall(
            r'<a class="([^"]*)" href="(/viewer/video/live/\d+\.do)"[^>]*title="([^"]*)"[^>]*>\s*<em>(.*?)</em>', doc, re.S):
        item = {"name": re.sub(r"<[^>]+>|\s+", " ", name).strip(), "title": title, "on": "on" in cls.split(),
                "page": site + path, "url": ""}
        if item["on"]:
            try:
                page = http_get(site + path).decode("utf-8", "replace")
                src = re.search(r'<source[^>]+src="([^"]+\.m3u8[^"]*)"', page)
                item["url"] = src.group(1) if src else ""
            except OSError:
                pass
        out.append(item)
    return out


# ---------- 방송 조각(HLS)에서 소리 꺼내기 ----------

def hls_segments(url, stop):
    """생중계 재생 목록을 따라가며 새로 나온 조각의 내용을 차례로 돌려준다."""
    seen, first = [], True
    while not stop.is_set():
        try:
            text = http_get(url).decode("utf-8", "replace")
        except OSError:
            time.sleep(2)
            continue
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if any(l.startswith("#EXT-X-STREAM-INF") for l in lines):   # 화질 목록이면 첫 번째를 따라간다
            url = urllib.parse.urljoin(url, next(l for l in lines if not l.startswith("#")))
            continue
        wait = 2.0
        for l in lines:
            if l.startswith("#EXT-X-TARGETDURATION:"):
                wait = max(1.0, float(l.split(":")[1]) / 2)
        uris = [urllib.parse.urljoin(url, l) for l in lines if not l.startswith("#")]
        if first:                       # 처음에는 가장 최근 조각부터 시작한다
            seen, first = uris[:-2], False
        for uri in uris:
            if uri in seen or stop.is_set():
                continue
            seen.append(uri)
            try:
                yield http_get(uri)
            except OSError:
                continue
        seen = seen[-200:]
        stop.wait(wait)


def ts_audio(data, state):
    """방송 조각(MPEG-TS)에서 소리(AAC 또는 MP3) 부분만 꺼낸다. state에 소리 채널 번호를 기억한다."""
    out = bytearray()
    for i in range(0, len(data) - 187, 188):
        pkt = data[i:i + 188]
        if pkt[0] != 0x47:
            continue
        start, pid, field = pkt[1] & 0x40, ((pkt[1] & 0x1F) << 8) | pkt[2], (pkt[3] >> 4) & 3
        off = 4 + (1 + pkt[4] if field & 2 else 0)
        if not field & 1 or off >= 188:
            continue
        body = pkt[off:]
        if pid == 0 and start:                                  # 프로그램 목록
            p = body[1 + body[0]:]
            end = 3 + (((p[1] & 0x0F) << 8) | p[2]) - 4
            for j in range(8, end, 4):
                if (p[j] << 8) | p[j + 1]:
                    state["pmt"] = ((p[j + 2] & 0x1F) << 8) | p[j + 3]
        elif pid == state.get("pmt") and start:                 # 프로그램 구성: 소리 채널 찾기
            p = body[1 + body[0]:]
            end = 3 + (((p[1] & 0x0F) << 8) | p[2]) - 4
            j = 12 + (((p[10] & 0x0F) << 8) | p[11])
            while j + 5 <= end:
                if p[j] in (0x0F, 0x03, 0x04):                  # AAC, MP3
                    state["audio"] = ((p[j + 1] & 0x1F) << 8) | p[j + 2]
                j += 5 + (((p[j + 3] & 0x0F) << 8) | p[j + 4])
        elif pid == state.get("audio"):
            if start and body[:3] == b"\x00\x00\x01":
                body = body[9 + body[8]:]
            out += body
    return bytes(out)


def find_ffmpeg():
    here = Path(__file__).resolve().parent
    for cand in (here / "ffmpeg.exe", here / "ffmpeg"):
        if cand.exists():
            return str(cand)
    return shutil.which("ffmpeg")


# ---------- 웹소켓(기본 기능만으로) ----------

class WebSocket:
    def __init__(self, url, headers):
        u = urllib.parse.urlsplit(url)
        secure = u.scheme == "wss"
        host, port = u.hostname, u.port or (443 if secure else 80)
        proxy = None
        if secure and host not in ("127.0.0.1", "localhost") and not urllib.request.proxy_bypass(host):
            proxy = urllib.request.getproxies().get("https")
        if proxy:                                               # 사내 프록시가 있으면 그것을 거친다
            pu = urllib.parse.urlsplit(proxy if "//" in proxy else "//" + proxy)
            sock = socket.create_connection((pu.hostname, pu.port or 8080), timeout=20)
            sock.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
            if b" 200" not in self._head(sock).split(b"\r\n")[0]:
                raise OSError("프록시가 연결을 거절했습니다")
        else:
            sock = socket.create_connection((host, port), timeout=20)
        if secure:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + ("?" + u.query if u.query else "")
        lines = [f"GET {path} HTTP/1.1", f"Host: {u.netloc}", "Upgrade: websocket", "Connection: Upgrade",
                 f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"] + [f"{k}: {v}" for k, v in headers.items()]
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        head = self._head(sock)
        if b" 101" not in head.split(b"\r\n")[0]:
            raise OSError("연결 거절: " + head.split(b"\r\n")[0].decode("latin-1"))
        sock.settimeout(60)
        self.sock, self.lock, self.buf = sock, threading.Lock(), b""

    @staticmethod
    def _head(sock):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(1)
            if not chunk:
                break
            data += chunk
        return data

    def send(self, payload, opcode=2):
        if isinstance(payload, str):
            payload, opcode = payload.encode("utf-8"), 1
        n, mask = len(payload), os.urandom(4)
        head = bytes([0x80 | opcode]) + (bytes([0x80 | n]) if n < 126 else
                                         bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536 else
                                         bytes([0x80 | 127]) + struct.pack(">Q", n))
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload)) if n < 4096 else \
            (int.from_bytes(payload, "big") ^ int.from_bytes((mask * (n // 4 + 1))[:n], "big")).to_bytes(n, "big")
        with self.lock:
            self.sock.sendall(head + mask + masked)

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OSError("연결이 끊겼습니다")
            self.buf += chunk
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def recv(self):
        """(종류, 내용)을 돌려준다. 종류는 'text', 'binary', 'close'."""
        parts = b""
        while True:
            b1, b2 = self._read(2)
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            data = self._read(n)
            op = b1 & 0x0F
            if op == 9:
                self.send(data, 10)
            elif op == 8:
                code = struct.unpack(">H", data[:2])[0] if len(data) >= 2 else 1005
                return "close", (code, data[2:].decode("utf-8", "replace"))
            elif op in (0, 1, 2):
                parts += data
                if b1 & 0x80:
                    return ("text", parts.decode("utf-8", "replace")) if op != 2 else ("binary", parts)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# ---------- 받아쓰기 세션 ----------

class LiveSession(threading.Thread):
    def __init__(self, cfg, url, name="", terms=(), hints=(), max_minutes=240, mic=False):
        super().__init__(daemon=True)
        self.cfg, self.url, self.title = cfg, url, name
        self.terms = [(t, re.sub(r"\s", "", t)) for t in terms if t.strip()]
        self.hints = [h for h in hints if h.strip()][:400]
        self.max_seconds = max_minutes * 60
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.status, self.message = "starting", ""
        self.segments, self.next_id, self.partial = [], 1, ""
        self.started, self.audio_ms, self.audio_base = time.time(), 0, 0
        self.words, self.speaker = [], ""
        self.mic = mic                                   # 방송 대신 화면이 보내 주는 마이크 소리를 쓴다
        self.inbox = queue.Queue(maxsize=600)
        self.source = "mic" if mic else "ffmpeg" if find_ffmpeg() else "python"

    # 상태를 화면에 넘길 모양으로
    def snapshot(self, after=0):
        with self.lock:
            return {"status": self.status, "message": self.message, "name": self.title, "source": self.source,
                    "elapsed": int(time.time() - self.started), "audio_seconds": int((self.audio_base + self.audio_ms) / 1000),
                    "partial": self.partial, "last_id": self.next_id - 1,
                    "segments": [s for s in self.segments if s["id"] > after]}

    def stop(self):
        self.stop_event.set()

    def feed(self, data):
        """화면이 보낸 마이크 소리(16kHz, 16비트, 한 채널)를 받는다. 밀리면 오래된 것부터 버린다."""
        if self.inbox.full():
            try:
                self.inbox.get_nowait()
            except queue.Empty:
                pass
        self.inbox.put_nowait(data)

    def _flush(self):
        text = re.sub(r"\s+", " ", "".join(self.words)).strip()
        self.words = []
        if not text:
            return
        flat = re.sub(r"\s", "", text)
        seg = {"id": self.next_id, "time": time.strftime("%H:%M:%S"), "speaker": self.speaker, "text": text,
               "hits": [t for t, key in self.terms if key and key in flat], "request": bool(REQUEST.search(text))}
        self.next_id += 1
        self.segments.append(seg)
        del self.segments[:-KEEP_SEGMENTS]

    def _on_message(self, msg):
        if msg.get("error_code") or msg.get("error_message"):
            raise RuntimeError(f"음성 인식 오류 {msg.get('error_code', '')}: {msg.get('error_message', '')}")
        with self.lock:
            partial = []
            for tok in msg.get("tokens") or []:
                text = tok.get("text", "")
                if not tok.get("is_final"):
                    if text not in MARKERS:
                        partial.append(text)
                    continue
                speaker = str(tok.get("speaker") or "")
                if text in MARKERS or (speaker and self.speaker and speaker != self.speaker):
                    self._flush()
                if text not in MARKERS:
                    self.speaker = speaker or self.speaker
                    self.words.append(text)
                    joined = "".join(self.words)
                    if len(joined) > 60 and re.search(r"[.?!]\s*$", joined):
                        self._flush()
            self.partial = re.sub(r"\s+", " ", "".join(self.words) + "".join(partial)).strip()
            if "total_audio_proc_ms" in msg:
                self.audio_ms = msg["total_audio_proc_ms"]
        return bool(msg.get("finished"))

    def _audio(self, stop):
        """소리 덩어리를 차례로 돌려준다. (형식 설정, 덩어리 생성기)"""
        if self.mic:
            def chunks():
                while not stop.is_set() and not self.stop_event.is_set():
                    try:
                        yield self.inbox.get(timeout=1)
                    except queue.Empty:
                        continue
            return {"audio_format": "pcm_s16le", "sample_rate": 16000, "num_channels": 1}, chunks()
        ffmpeg = find_ffmpeg()
        if ffmpeg:
            def chunks():
                proc = subprocess.Popen([ffmpeg, "-loglevel", "error", "-i", self.url, "-vn", "-ac", "1", "-ar", "16000",
                                         "-f", "s16le", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                try:
                    while not stop.is_set():
                        data = proc.stdout.read(6400)        # 0.2초 분량
                        if not data:
                            break
                        yield data
                finally:
                    proc.kill()
            return {"audio_format": "pcm_s16le", "sample_rate": 16000, "num_channels": 1}, chunks()

        def chunks():
            state = {}
            for seg in hls_segments(self.url, stop):
                audio = ts_audio(seg, state)
                for i in range(0, len(audio), 8192):
                    yield audio[i:i + 8192]
        return {"audio_format": "auto"}, chunks()

    def _config(self, level, audio):
        """설정을 단계적으로 줄여 가며 시도한다(모델이 받지 않는 선택 항목이 있을 때)."""
        conf = dict(audio, model="stt-rt-v5", language_hints=["ko"], enable_speaker_diarization=True)
        if level < 2:
            conf["enable_endpoint_detection"] = True
        if level < 1 and self.hints:
            conf["context"] = {"terms": self.hints}
        return conf

    def _connect_once(self, level):
        stop = threading.Event()
        audio, chunks = self._audio(stop)
        base = self.cfg["base_url"].replace("https://", "wss://").replace("http://", "ws://")
        ws = WebSocket(base + self.cfg.get("stt_path", "/soniox/transcribe-websocket"),
                       {"Authorization": "Bearer " + self.cfg["api_key"]})
        ws.send(json.dumps(self._config(level, audio)))
        opened, failed = time.time(), []

        def pump():
            try:
                for data in chunks:
                    if stop.is_set() or self.stop_event.is_set() or time.time() - opened > SESSION_SECONDS:
                        break
                    ws.send(data)
                ws.send("")                                  # 끝 표시는 빈 글자 프레임
            except OSError as e:
                failed.append(e)
        threading.Thread(target=pump, daemon=True).start()
        with self.lock:
            self.status, self.message = "running", ""
        try:
            while True:
                kind, data = ws.recv()
                if kind == "close":
                    return data
                if kind == "text" and self._on_message(json.loads(data)):
                    return (1000, "finished")
        finally:
            stop.set()
            ws.close()
            with self.lock:
                self._flush()
                self.audio_base += self.audio_ms
                self.audio_ms = 0

    def run(self):
        level, quick_fails = 0, 0
        while not self.stop_event.is_set():
            if time.time() - self.started > self.max_seconds:
                self.status, self.message = "stopped", "정해 둔 최대 시간이 지나 멈췄습니다."
                return
            began = time.time()
            try:
                code, reason = self._connect_once(level)
            except RuntimeError as e:                          # 음성 인식 쪽이 설정을 거절한 경우
                if level < 2 and time.time() - began < 15:
                    level += 1
                    continue
                self.status, self.message = "error", str(e)
                return
            except (OSError, ValueError) as e:
                code, reason = 0, str(e)
            if self.stop_event.is_set():
                break
            if code == 4402:
                self.status, self.message = "error", "크레딧이 부족합니다."
                return
            if code == 1008:
                self.status, self.message = "error", "연결이 거절됐습니다: " + reason
                return
            quick_fails = quick_fails + 1 if time.time() - began < 20 else 0
            if quick_fails >= 5:
                self.status = "error"
                self.message = "방송이 끝났거나 소리를 받지 못했습니다." if reason == "finished" else "연결이 계속 끊깁니다: " + reason
                return
            with self.lock:
                self.status, self.message = "starting", "다시 연결하는 중입니다."
            self.stop_event.wait(3 if code != 1013 else 10)
        self.status, self.message = "stopped", ""
