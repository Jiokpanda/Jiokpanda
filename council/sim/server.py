#!/usr/bin/env python3
"""모의 감사용 로컬 서버.

이 PC에서만 접속할 수 있는 작은 서버를 띄워, 브리핑 화면을 보여 주고
'모의 감사' 탭의 대화를 AI 서비스(OpenAI 호환 API, 예: 팩트챗)로 전달한다.
API 키는 이 PC의 sim/config.json 또는 환경 변수에만 두며 화면으로 보내지 않는다.
파이썬 기본 기능만 사용한다.

사용법:
    1) sim/config.example.json 을 sim/config.json 으로 복사해 api_key, model 을 채운다.
    2) python sim/server.py
    3) 브라우저에서 http://127.0.0.1:8765 를 연다.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
DEFAULTS = {
    "base_url": "https://factchat-cloud.mindlogic.ai/v1/gateway",
    "chat_path": "/chat/completions",
    "models_path": "/models",
    "api_key": "",
    "model": "",
    "temperature": None,   # 최신 모델 다수가 이 값을 받지 않으므로 기본은 보내지 않는다
    "port": 8765,
}
MAX_BODY = 400_000       # 화면에서 받는 요청 크기 한도(바이트)
HIDDEN = ("/sim/config", "/.cache", "/.git")   # 화면으로 내주지 않는 경로


def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")))  # 메모장이 붙이는 BOM 허용
    for key, env in (("api_key", "FACTCHAT_API_KEY"), ("base_url", "FACTCHAT_BASE_URL"), ("model", "FACTCHAT_MODEL")):
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    cfg["base_url"] = cfg["base_url"].rstrip("/")
    return cfg


def call_api(cfg, path, payload=None):
    """AI 서비스에 요청하고 (상태 코드, 본문)을 돌려준다. 차단·오류 응답도 그대로 전달한다."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(cfg["base_url"] + path, data=data, method="POST" if data else "GET", headers={
        "Authorization": "Bearer " + cfg["api_key"], "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as res:
            return res.status, res.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:
        return 0, str(e)


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):   # 대화 내용이 터미널에 남지 않도록 요청 줄만 간단히 적는다
        sys.stderr.write("%s %s\n" % (self.command, self.path.split("?")[0]))

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def local_only(self):
        """다른 웹사이트가 이 서버를 몰래 호출하지 못하게, 이 PC의 이 주소에서 온 요청만 받는다."""
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        origin = self.headers.get("Origin")
        return self.headers.get("Host") in allowed and (origin is None or origin.split("//")[-1] in allowed)

    def do_GET(self):
        path = self.path.split("?")[0]
        if any(path.startswith(h) for h in HIDDEN):
            return self.send_error(404)
        if not path.startswith("/api/"):
            return super().do_GET()
        if not self.local_only():
            return self.send_json({"error": "이 PC에서 연 화면의 요청만 받습니다."}, 403)
        cfg = load_config()
        if path == "/api/status":
            return self.send_json({"ok": True, "configured": bool(cfg["api_key"]), "model": cfg["model"],
                                   "service": cfg["base_url"].split("//")[-1].split("/")[0]})
        if path == "/api/models":
            status, body = call_api(cfg, cfg["models_path"])
            try:
                names = sorted(m["id"] for m in json.loads(body).get("data", []))
            except (ValueError, AttributeError, KeyError, TypeError):
                names = []
            return self.send_json({"models": names, "status": status, "detail": "" if names else body[:500]})
        return self.send_error(404)

    def do_POST(self):
        if self.path.split("?")[0] != "/api/chat":
            return self.send_error(404)
        if not self.local_only():
            return self.send_json({"error": "이 PC에서 연 화면의 요청만 받습니다."}, 403)
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_BODY:
            return self.send_json({"error": "요청이 비었거나 너무 큽니다."}, 400)
        try:
            messages = json.loads(self.rfile.read(length).decode("utf-8"))["messages"]
            assert isinstance(messages, list) and messages
        except (ValueError, KeyError, AssertionError):
            return self.send_json({"error": "요청 형식이 올바르지 않습니다."}, 400)
        cfg = load_config()
        if not cfg["api_key"] or not cfg["model"]:
            return self.send_json({"error": "sim/config.json 에 api_key 와 model 을 넣어 주세요."}, 400)
        payload = {"model": cfg["model"], "messages": messages}
        if cfg["temperature"] is not None:
            payload["temperature"] = cfg["temperature"]
        status, body = call_api(cfg, cfg["chat_path"], payload)
        try:
            text = json.loads(body)["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            # 보안 프로그램이나 서비스가 막은 경우 그 응답을 그대로 보여 준다.
            return self.send_json({"error": f"AI 서비스가 답하지 않았습니다(응답 코드 {status}).", "detail": body[:800]}, 502)
        return self.send_json({"text": text})


def main():
    cfg = load_config()
    server = ThreadingHTTPServer(("127.0.0.1", int(cfg["port"])), partial(Handler, directory=str(ROOT)))
    print(f"브라우저에서 http://127.0.0.1:{cfg['port']} 를 여세요. 끝내려면 Ctrl+C.")
    if not cfg["api_key"] or not cfg["model"]:
        print("주의: sim/config.json 에 api_key 와 model 이 없어 모의 감사는 아직 동작하지 않습니다.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
