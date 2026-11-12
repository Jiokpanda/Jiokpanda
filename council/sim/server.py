#!/usr/bin/env python3
"""모의 감사·생중계 받아쓰기용 로컬 서버.

이 PC에서만 접속할 수 있는 작은 서버를 띄워, 브리핑 화면을 보여 주고
'모의 감사' 탭의 대화와 '생중계' 탭의 소리를 AI 서비스(팩트챗 API 게이트웨이)로 전달한다.
API 키는 이 PC의 sim/config.json 또는 환경 변수에만 두며 화면으로 보내지 않는다.
파이썬 기본 기능만 사용한다.

사용법:
    1) sim/config.example.json 을 sim/config.json 으로 복사해 api_key, model 을 채운다.
    2) python sim/server.py
    3) 브라우저에서 http://127.0.0.1:8765 를 연다.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import docs  # noqa: E402  (같은 폴더의 답변 자료 찾기 모듈)
import live  # noqa: E402  (같은 폴더의 생중계 받아쓰기 모듈)

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
DEFAULTS = {
    "base_url": "https://factchat-cloud.mindlogic.ai/v1/gateway",
    "chat_path": "/chat/completions/",
    "models_path": "/models/?type=llm",
    "stt_path": "/soniox/transcribe-websocket",
    "rerank_path": "/rerank/",
    "rerank_model": "qwen3-rerank",
    "rerank_candidates": 40,        # AI로 다시 고를 때 보내는 후보 대목 수
    "expand_query": True,           # AI로 다시 고를 때 검색어를 먼저 넓힐지(질문만 전송)
    "materials_dir": "materials",   # 답변 자료 폴더(council 폴더 기준 상대 경로 또는 전체 경로)
    "live_max_minutes": 240,   # 생중계 받아쓰기를 켜 둘 수 있는 최대 시간(크레딧 보호)
    "api_key": "",
    "model": "",
    "temperature": None,   # 최신 모델 다수가 이 값을 받지 않으므로 기본은 보내지 않는다
    "port": 8765,
}
MAX_BODY = 400_000       # 화면에서 받는 요청 크기 한도(바이트)
HIDDEN = ("/sim/config", "/.cache", "/.git", "/materials")   # 화면으로 내주지 않는 경로
SESSION = {"live": None}                        # 지금 돌고 있는 생중계 받아쓰기
INDEX = {"docs": None}                          # 읽어 둔 답변 자료


def materials(cfg):
    folder = Path(cfg["materials_dir"])
    folder = folder if folder.is_absolute() else ROOT / folder
    if INDEX["docs"] is None or INDEX["docs"].folder != folder:
        INDEX["docs"] = docs.Index(folder, cache_dir=ROOT / ".cache" / "materials")
        INDEX["docs"].start()          # 뒤에서 읽는다. 화면은 진행 상황을 물어 가며 기다린다.
    return INDEX["docs"]


def loaded(index, seconds=20):
    """자료를 처음 읽는 중이면 끝날 때까지(최대 seconds초) 기다렸다가 돌려준다."""
    import time
    until = time.time() + seconds
    while index.loading and not index.chunks and time.time() < until:
        time.sleep(0.2)
    return index


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
        if path == "/api/docs/status":
            return self.send_json(materials(cfg).status())
        if path == "/api/live/channels":
            try:
                return self.send_json({"channels": live.channels(), "ffmpeg": bool(live.find_ffmpeg())})
            except OSError as e:
                return self.send_json({"error": "생중계 목록을 읽지 못했습니다.", "detail": str(e)}, 502)
        if path == "/api/live/state":
            session = SESSION["live"]
            after = self.path.partition("after=")[2]
            if not session:
                return self.send_json({"status": "idle", "segments": [], "last_id": 0})
            return self.send_json(session.snapshot(int(after) if after.isdigit() else 0))
        if path == "/api/models":
            status, body = call_api(cfg, cfg["models_path"])
            try:
                names = sorted(m["id"] for m in json.loads(body).get("data", []))
            except (ValueError, AttributeError, KeyError, TypeError):
                names = []
            return self.send_json({"models": names, "status": status, "detail": "" if names else body[:500]})
        return self.send_error(404)

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_BODY:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return None

    def do_POST(self):
        path = self.path.split("?")[0]
        if path not in ("/api/chat", "/api/live/start", "/api/live/stop", "/api/docs/reload", "/api/docs/search", "/api/docs/refine", "/api/docs/answer", "/api/live/audio"):
            return self.send_error(404)
        if not self.local_only():
            return self.send_json({"error": "이 PC에서 연 화면의 요청만 받습니다."}, 403)
        cfg = load_config()
        if path == "/api/live/stop":
            if SESSION["live"]:
                SESSION["live"].stop()
            return self.send_json({"ok": True})
        if path == "/api/docs/reload":
            index = materials(cfg)
            index.start()
            return self.send_json(index.status())
        if path == "/api/live/audio":                      # 마이크 소리 조각(글자가 아니라 소리 그대로)
            length = int(self.headers.get("Content-Length") or 0)
            data = self.rfile.read(length) if 0 < length <= MAX_BODY else b""
            session = SESSION["live"]
            if session and session.mic and data:
                session.feed(data)
            return self.send_json({"ok": bool(session and session.mic), "status": session.status if session else "idle"})
        body = self.read_json()
        if not isinstance(body, dict):
            return self.send_json({"error": "요청 형식이 올바르지 않습니다."}, 400)
        if path == "/api/docs/search":
            return self.send_json({"results": loaded(materials(cfg)).search(str(body.get("q") or ""), int(body.get("k") or 5))})
        if path == "/api/docs/refine":
            # 화면에서 AI를 켠 경우에만 호출된다. ① 질문으로 검색어를 넓히고 ② 넓게 찾은 후보를 재정렬 모델로 다시 고른다.
            if not cfg["api_key"]:
                return self.send_json({"error": "sim/config.json 에 api_key 를 넣어 주세요."}, 400)
            q, index, terms, note = str(body.get("q") or "")[:1000], loaded(materials(cfg)), [], ""
            if cfg["expand_query"] and cfg["model"]:
                ask = ("다음 질문의 답이 적혀 있을 내부 문서(현황 보고, 통계표, 계획서, 규정)에 실제로 쓰였을 법한 낱말을 "
                       "8개 이내로, 쉼표로만 구분해 적어 주세요. 설명은 쓰지 않습니다.\n질문: " + q)
                status, raw = call_api(cfg, cfg["chat_path"], {"model": cfg["model"], "messages": [{"role": "user", "content": ask}]})
                try:
                    text = json.loads(raw)["choices"][0]["message"]["content"]
                    terms = [t.strip(" .·-\"'") for t in re.split(r"[,\n]", text) if 1 < len(t.strip()) <= 20][:10]
                except (ValueError, KeyError, IndexError, TypeError):
                    note = f"검색어 넓히기 실패(응답 코드 {status})"
            found = index.search(q, int(cfg["rerank_candidates"]), extra=terms, cutoff=0)
            if len(found) > 1:
                texts = [(c["file"].rsplit("/", 1)[-1] + " | " + (c["title"] + " | " if c["title"] and c["title"] not in c["text"] else "") + c["text"])[:1500] for c in found]
                status, raw = call_api(cfg, cfg["rerank_path"], {"model": cfg["rerank_model"], "query": q, "documents": texts, "top_n": 5})
                try:
                    order = json.loads(raw).get("results") or []
                    picked = [dict(found[int(r["index"])], relevance=round(float(r.get("relevance_score", 0)), 2)) for r in order]
                    if picked:
                        return self.send_json({"results": picked, "terms": terms, "reranked": True, "sent": len(texts), "note": note})
                    note = (note + " " if note else "") + "재정렬 결과가 비어 있습니다."
                except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                    note = (note + " " if note else "") + f"재정렬 실패(응답 코드 {status}): {raw[:200]}"
            return self.send_json({"results": found[:5], "terms": terms, "reranked": False, "sent": 0, "note": note})
        if path == "/api/docs/answer":
            # 화면에서 'AI로 답변 요지 만들기'를 켠 경우에만 호출된다. 찾은 대목만 AI 서비스로 보낸다.
            if not cfg["api_key"] or not cfg["model"]:
                return self.send_json({"error": "sim/config.json 에 api_key 와 model 을 넣어 주세요."}, 400)
            passages = [str(x)[:1500] for x in body.get("passages") or []][:6]
            prompt = ("아래 [자료]만 근거로 [질문]에 대한 답변 요지를 2~3문장으로 정리해 주세요.\n"
                      "- 자료에 있는 수치와 표현만 쓰고, 자료에 없는 내용은 쓰지 않습니다.\n"
                      "- 문장 끝에 근거가 된 자료 번호를 [1]처럼 붙입니다.\n"
                      "- 자료로 답할 수 없으면 '준비된 자료에 없음'이라고만 답합니다.\n\n"
                      "[질문]\n" + str(body.get("q") or "")[:1000] + "\n\n[자료]\n" +
                      "\n".join(f"[{i}] {t}" for i, t in enumerate(passages, 1)))
            status, raw = call_api(cfg, cfg["chat_path"], {"model": cfg["model"], "messages": [{"role": "user", "content": prompt}]})
            try:
                return self.send_json({"text": json.loads(raw)["choices"][0]["message"]["content"]})
            except (ValueError, KeyError, IndexError, TypeError):
                return self.send_json({"error": f"AI 서비스가 답하지 않았습니다(응답 코드 {status}).", "detail": raw[:800]}, 502)
        if path == "/api/live/start":
            url = str(body.get("url") or "")
            mic = url == "mic"
            if not cfg["api_key"]:
                return self.send_json({"error": "sim/config.json 에 api_key 를 넣어 주세요."}, 400)
            if not mic and not re.match(r"https?://[^\s]+\.m3u8", url):
                return self.send_json({"error": "방송 주소가 올바르지 않습니다."}, 400)
            if SESSION["live"]:
                SESSION["live"].stop()
            SESSION["live"] = live.LiveSession(cfg, url, name=str(body.get("name") or ""),
                                               terms=[str(t) for t in body.get("terms") or []][:60],
                                               hints=[str(t) for t in body.get("hints") or []],
                                               max_minutes=int(cfg["live_max_minutes"]), mic=mic)
            SESSION["live"].start()
            return self.send_json({"ok": True, "max_minutes": int(cfg["live_max_minutes"])})
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            return self.send_json({"error": "요청 형식이 올바르지 않습니다."}, 400)
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
