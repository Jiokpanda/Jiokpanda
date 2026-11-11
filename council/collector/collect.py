#!/usr/bin/env python3
"""용인특례시의회 행정사무감사 회의록 수집기.

의회 홈페이지(https://council.yongin.go.kr)에 공개된 회의록 목록과 본문을
받아 .cache/ 폴더에 저장한다. 이미 받은 파일은 다시 받지 않는다.
파이썬 기본 기능만 사용하므로 별도 설치가 필요 없다.

사용법:  python collect.py            (제9대·제10대 행감 회의록)
         python collect.py --terms 8,9,10
"""
import argparse
import html
import http.client
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

BASE = "https://council.yongin.go.kr"
ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache"
# 행정사무감사 회의록은 위원회 코드가 J로 시작한다.
COMMITTEES = {
    "J011": "의회운영위원회",
    "J023": "자치행정위원회",
    "J042": "문화복지위원회",
    "J043": "경제환경위원회",
    "J051": "도시건설위원회",
}
DELAY = 1.0  # 요청 사이 간격(초). 의회 서버에 부담을 주지 않기 위함.


_conn = None


def _connect():
    """의회 서버에 연결한다. HTTPS_PROXY가 설정돼 있으면 그 프록시를 거친다."""
    host = urllib.parse.urlsplit(BASE).hostname
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy:
        p = urllib.parse.urlsplit(proxy)
        conn = http.client.HTTPSConnection(p.hostname, p.port or 80, timeout=20)
        conn.set_tunnel(host, 443)
    else:
        conn = http.client.HTTPSConnection(host, 443, timeout=20)
    return conn


def fetch(url, tries=12):
    """연결 단계에서 자주 끊기므로, 한 번 맺은 연결을 계속 쓰고 끊기면 다시 맺는다."""
    global _conn
    path = url[len(BASE):]
    last = None
    for i in range(tries):
        try:
            if _conn is None:
                _conn = _connect()
            _conn.request("GET", path, headers={"User-Agent": "Mozilla/5.0", "Connection": "keep-alive"})
            res = _conn.getresponse()
            data = res.read()
            if res.status != 200:
                raise OSError(f"HTTP {res.status}")
            time.sleep(DELAY)
            return data.decode("utf-8", "replace")
        except (http.client.HTTPException, OSError) as e:
            last = e
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None
            time.sleep(min(1 + i, 5))
    raise RuntimeError(f"받지 못함: {url} ({last})")


def strip(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def list_meetings(term, code):
    """검색 결과 표에서 회의록 목록을 읽는다(쪽 단위로 끝까지)."""
    out, page = [], 1
    while True:
        q = urllib.parse.urlencode({
            "cl_sch": "CT", "th_sch": term, "sess_sch": "all",
            "cmt_sch": code, "mem_sch": "", "schwrd": "", "page": page,
        })
        doc = fetch(f"{BASE}/kr/minutes/search.do?{q}")
        seg = doc[doc.find('id="search_result"'):]
        seg = seg[:seg.find('class="charge"')]
        rows = 0
        for row in re.findall(r"(?is)<tr>(.*?)</tr>", seg):
            uid = re.search(r"minutes\.do\?uid=(\d+)", row)
            if not uid:
                continue
            rows += 1
            cells = [strip(c) for c in re.findall(r"(?is)<td[^>]*>(.*?)</td>", row)]
            title = re.search(r'title="([^"]*)"', row)
            target = re.search(r"피감사기관\s*:\s*([^)]*)\)", html.unescape(title.group(1)) if title else "")
            out.append({
                "uid": int(uid.group(1)),
                "term": int(re.sub(r"\D", "", cells[0])),
                "session": int(re.sub(r"\D", "", cells[1])),
                "day": cells[2],
                "committee": COMMITTEES[code],
                "code": code,
                "date": cells[4].replace(".", "-"),
                "target": target.group(1).strip() if target else "",
            })
        total = re.search(r"/\s*<strong>(\d+)</strong>", seg)
        total = int(total.group(1)) if total else 0
        have = sum(1 for m in out)
        if rows == 0 or have >= total:
            return out
        page += 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--terms", default="9,10", help="수집할 대수(쉼표로 구분)")
    args = ap.parse_args()
    (CACHE / "minutes").mkdir(parents=True, exist_ok=True)

    meetings = {}
    for term in [t.strip() for t in args.terms.split(",") if t.strip()]:
        for code, name in COMMITTEES.items():
            found = list_meetings(term, code)
            print(f"제{term}대 {name} 행정사무감사: {len(found)}건", flush=True)
            for m in found:
                meetings[m["uid"]] = m

    done = 0
    for uid in sorted(meetings):
        path = CACHE / "minutes" / f"{uid}.html"
        if not path.exists() or path.stat().st_size < 5000:
            doc = fetch(f"{BASE}/viewer/minutes.do?uid={uid}")
            if "minutes-body" not in doc:
                print(f"  본문 없음: {uid}", flush=True)
                continue
            path.write_text(doc, encoding="utf-8")
        done += 1
        if done % 20 == 0:
            print(f"  본문 {done}/{len(meetings)}", flush=True)

    # 현역 의원 명단(검색 화면의 의원 선택 상자)도 함께 저장한다.
    doc = fetch(f"{BASE}/kr/minutes/search.do")
    sel = re.search(r'(?is)<select name="mem_sch".*?</select>', doc)
    members = dict(re.findall(r"<option value='(\d+)'[^>]*>([^<]+)</option>", sel.group(0))) if sel else {}

    (CACHE / "meetings.json").write_text(
        json.dumps({"meetings": sorted(meetings.values(), key=lambda m: (m["date"], m["uid"])),
                    "current_members": members}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    print(f"완료: 회의록 {done}건, 현역 의원 {len(members)}명")


if __name__ == "__main__":
    sys.exit(main())
