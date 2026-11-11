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
# 행감 전에 열린 일반 회의(업무보고, 추경·결산 심사 등). 최근 대수만 받는다.
REGULAR = {
    "A011": "본회의",
    "C011": "의회운영위원회",
    "C023": "자치행정위원회",
    "C042": "문화복지위원회",
    "C043": "경제환경위원회",
    "C051": "도시건설위원회",
    "E011": "예산결산특별위원회",
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


class NotFound(Exception):
    pass


def fetch(url, tries=12, want_url=False):
    """연결 단계에서 자주 끊기므로, 한 번 맺은 연결을 계속 쓰고 끊기면 다시 맺는다.

    want_url=True이면 (본문, 최종 주소)를 돌려준다. 문서 뷰어처럼 다른 주소로
    넘겨 주는 경우 최종 주소가 필요하다.
    """
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
            if res.status in (301, 302, 303) and res.getheader("Location"):
                path = urllib.parse.urljoin(BASE + path, res.getheader("Location"))[len(BASE):]
                continue
            if res.status == 404:
                raise NotFound(url)
            if res.status != 200:
                raise OSError(f"HTTP {res.status}")
            time.sleep(DELAY)
            text = data.decode("utf-8", "replace")
            return (text, BASE + path) if want_url else text
        except NotFound:
            raise
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
                "committee": COMMITTEES.get(code) or REGULAR[code],
                "audit": code in COMMITTEES,
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


def table_rows(doc):
    """목록 화면의 첫 표를 줄 단위로 돌려준다."""
    table = doc[doc.find("<table"):]
    return re.findall(r"(?is)<tr>(.*?)</tr>", table[:table.find("</table>")])[1:]


def collect_extras(term):
    """의원 프로필에 쓸 5분 자유발언·시정질문·의안 목록을 받는다."""
    speeches = {}
    for kind, path in (("free", "free.do"), ("qna", "qna2.do")):
        page = 1
        while True:
            doc = fetch(f"{BASE}/kr/minutes/{path}?th_sch={term}&page={page}")
            found = 0
            for row in table_rows(doc):
                link = re.search(r'minutes\.do\?uid=(\d+)&(?:amp;)?mem_sch=(\d*)&(?:amp;)?con_idx_sch=(\d+)[^"]*"[^>]*>(.*?)</a>', row, re.S)
                if not link:
                    continue
                found += 1
                cells = [strip(c) for c in re.findall(r"(?is)<td[^>]*>(.*?)</td>", row)]
                key = (kind, link.group(1), link.group(3))
                speeches[key] = {"kind": kind, "uid": int(link.group(1)), "member": link.group(2),
                                 "idx": int(link.group(3)), "title": strip(link.group(4)),
                                 "date": cells[-1].replace(".", "-")}
            if found < 10:
                break
            page += 1
    bills, page = {}, 1
    while True:
        doc = fetch(f"{BASE}/kr/bill.do?th_sch={term}&page={page}")
        found = 0
        for row in table_rows(doc):
            link = re.search(r"billview\.do\?uid=(\d+)", row)
            cells = [strip(c) for c in re.findall(r"(?is)<td[^>]*>(.*?)</td>", row)]
            if not link or len(cells) < 5:
                continue
            found += 1
            bills[int(link.group(1))] = {"uid": int(link.group(1)), "no": cells[0], "title": cells[1],
                                         "lead": cells[2], "date": cells[4]}
        if found < 10:
            break
        page += 1
    (CACHE / "extras.json").write_text(json.dumps(
        {"term": int(term), "speeches": list(speeches.values()), "bills": list(bills.values())},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"5분 자유발언·시정질문 {len(speeches)}건, 의안 {len(bills)}건")


def collect_reports(years):
    """행정사무감사 게시판에서 결과보고서와 최근 계획서를 찾아, 문서 뷰어가 변환한 HTML을 받는다."""
    out_dir = CACHE / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    posts, page = [], 1
    while True:
        doc = fetch(f"{BASE}/kr/inspectionBBS.do?page={page}")
        found = re.findall(r'href="(/kr/inspectionBBSview\.do\?uid=([0-9A-F]+)[^"]*)"[^>]*>\s*([^<]+?)\s*</a>', doc)
        if not found:
            break
        posts += [(uid, strip(title)) for _, uid, title in found]
        total = re.search(r"전체게시물\s*:\s*(\d+)", strip(doc))
        if not total or len(posts) >= int(total.group(1)):
            break
        page += 1

    reports = []
    plan_years = [int(y) for y in re.findall(r"(20\d\d)년도", " ".join(t for _, t in posts if "계획서" in t))]
    for uid, title in posts:
        year = re.search(r"(20\d\d)년도", title)
        if not year:
            continue
        if "결과보고서" in title and int(year.group(1)) in years:
            kind = "result"
        elif "계획서" in title and int(year.group(1)) == max(plan_years):
            kind = "plan"   # 가장 최근 계획서만(감사 일정과 감사위원 명단)
        else:
            continue
        view = fetch(f"{BASE}/kr/inspectionBBSview.do?uid={uid}")
        # 첨부파일 이름 바로 뒤에 '바로보기' 뷰어 링크가 붙어 있다.
        files = re.findall(r"download\.do\?bbs_id=inspection&(?:amp;)?uid=[0-9A-F]+' title='([^']+)'.*?"
                           r"bbsViewer\.do\?uid=(\d+)", view, re.S)
        print(f"{title}: 첨부 {len(files)}개", flush=True)
        for name, vid in files:
            name = html.unescape(name)
            folder = out_dir / vid
            info = {"kind": kind, "year": int(year.group(1)), "post": uid, "title": title, "file": name, "viewer": int(vid)}
            reports.append(info)
            if (folder / "done").exists():
                continue
            folder.mkdir(exist_ok=True)
            try:
                _, final = fetch(f"{BASE}/common/synap/bbsViewer.do?uid={vid}", want_url=True)
                q = urllib.parse.parse_qs(urllib.parse.urlsplit(final).query)
                base = BASE + q["rs"][0] + q["fn"][0]
                index = fetch(base + ".xml")
                pages = int(re.search(r"<filespage_cnt>(\d+)<", index).group(1))
                for n in range(1, pages + 1):
                    (folder / f"{n}.html").write_text(fetch(f"{base}.files/{n}.html"), encoding="utf-8")
                (folder / "done").write_text(str(pages))
            except (NotFound, KeyError, AttributeError) as e:
                print(f"  변환본을 받지 못함: {name} ({e})", flush=True)
    (CACHE / "reports.json").write_text(json.dumps(reports, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"결과보고서·계획서 파일 {len(reports)}개")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--terms", default="9,10", help="수집할 대수(쉼표로 구분)")
    ap.add_argument("--regular-terms", default="10", help="일반 회의록까지 받을 대수(쉼표로 구분)")
    ap.add_argument("--reports-only", action="store_true", help="결과보고서·계획서만 받기")
    args = ap.parse_args()
    (CACHE / "minutes").mkdir(parents=True, exist_ok=True)
    if args.reports_only:
        return collect_reports(range(2022, 2100))

    meetings = {}
    for term in [t.strip() for t in args.terms.split(",") if t.strip()]:
        for code, name in COMMITTEES.items():
            found = list_meetings(term, code)
            print(f"제{term}대 {name} 행정사무감사: {len(found)}건", flush=True)
            for m in found:
                meetings[m["uid"]] = m
    regular = [t.strip() for t in args.regular_terms.split(",") if t.strip()]
    for term in regular:
        for code, name in REGULAR.items():
            found = list_meetings(term, code)
            print(f"제{term}대 {name}: {len(found)}건", flush=True)
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
    if regular:
        collect_extras(max(regular, key=int))
    collect_reports({int(m["date"][:4]) for m in meetings.values() if m["audit"]} or range(2022, 2100))


if __name__ == "__main__":
    sys.exit(main())
