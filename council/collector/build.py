#!/usr/bin/env python3
"""수집한 회의록(.cache/)을 화면에서 쓰는 자료(data/)로 바꾼다.

회의록 본문을 발언 단위로 읽어, 의원 한 명의 질의와 그에 대한 공무원
답변을 한 묶음으로 만들고, 답변자의 직함에서 부서명을 뽑아 색인한다.

사용법:  python build.py
"""
import html
import json
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache"
DATA = ROOT / "data"

# 직함이 이 낱말 하나뿐이면 앞에 붙은 기관명이 부서명이다. 예) "용인도시공사 사장"
BARE_TITLES = {"사장", "이사장", "대표이사", "원장", "센터장", "관장", "소장", "본부장",
               "단장", "국장", "처장", "실장", "팀장", "부장", "과장", "대표", "회장",
               "사무국장", "사무처장", "상임이사", "총장", "교장"}
HEAD_TITLES = ("대표이사", "상임이사", "이사장", "사장", "회장")
# 끝의 '장'을 떼면 부서명이 되는 직함. 예) 행정과장 → 행정과
UNIT_SUFFIX = re.compile(r"(과|국|실|소|팀|단|본부|원|관|센터|구청|청|동|읍|면|처|부|담당|도서관|박물관|사업소)장$")

# 답변에서 '하겠다'고 한 문장을 찾는다. 인사·보고 같은 의례적 표현은 뺀다.
PROMISE = re.compile(
    r"(검토|조치|시정|보완|개선|제출|확인|반영|마련|추진|정비|점검|협의|노력|챙기|챙겨|살펴|강구|수정|바로잡|신경|"
    r"개정|변경|조정|파악|조사|분석|준비|수립|강화|확대|홍보|관리)[^.\n]{0,22}(?<![되있없같알])겠습니다")
REQUEST = re.compile(r"자료[^.\n]{0,40}(제출|주시|주십|주세요|부탁|요청|요구|보내|갖다|가져다)")


# 즉답하지 못하고 확인·별도 보고로 넘긴 답변
STUCK = re.compile(
    r"확인해 ?보(고|겠|도록)|확인 ?후에?|파악해 ?보|파악(을|이|은)? ?(못|안)|자료를? ?(안|못) ?가지고|"
    r"정확(한|히)[^.\n]{0,15}(모르|기억)|(따로|별도로?|추후에?|서면으로) ?(보고|제출|말씀|설명)")

# 어느 부서에나 나오는 단골 질의 주제: (이름, 질의에서 찾을 표현, 미리 준비할 것)
TOPICS = [
    ("예산 집행·이월·불용", r"집행률|집행 ?잔액|불용|이월", "사업별 집행률, 불용·이월액과 사유, 연말 집행 전망"),
    ("수의계약·관내 업체", r"수의계약|관내 ?업체|쪼개기|분할 ?발주", "수의계약 건수·금액, 관내 업체 비율, 같은 업체와의 반복 계약 여부"),
    ("용역", r"용역", "용역 목록과 금액, 결과물 활용 실적, 비슷한 용역의 중복 여부"),
    ("위원회 운영", r"위원회[^.\n]{0,25}(개최|운영|미개최|위촉|구성|서면|심의)", "소관 위원회별 개최 횟수, 서면 심의 비율, 위촉 현황(성별·중복 위촉)"),
    ("민원", r"민원", "민원 건수와 유형, 처리 기간, 반복·집단 민원의 진행 상황"),
    ("홍보", r"홍보", "홍보 수단별 실적과 예산, 이용률·인지도 등 효과"),
    ("보조금·민간위탁", r"보조금|위탁|수탁", "보조금 정산·점검 실적, 수탁기관 선정 절차, 성과평가 결과"),
    ("인력·채용", r"기간제|공무직|채용|결원|정원|인력", "정원과 현원, 결원 사유, 기간제·공무직 채용 절차 준수 여부"),
    ("성과·실적·만족도", r"성과|실적|만족도|이용률", "성과지표 달성률, 이용 실적 추이, 만족도 조사 결과"),
    ("감사 지적 후속 조치", r"종합감사|감사 ?지적|지적 ?사항|기관경고|환수", "상급기관·자체 감사 지적사항과 조치 결과, 재발 방지 대책"),
    ("안전", r"안전", "시설 점검 결과, 사고 현황, 보완 계획"),
    ("조례·규정", r"조례|규칙|규정|지침", "소관 조례 정비 계획, 조례에 정한 계획·위원회의 이행 여부"),
    ("공모사업·국도비", r"공모|국비|도비|국도비", "공모 신청·선정 실적, 국도비 확보액, 매칭 부담"),
    ("중복·형평성", r"중복|형평|편중", "유사 사업 현황, 지역·대상별 배분 근거"),
]
TOPIC_RES = [re.compile(t[1]) for t in TOPICS]
LONG_PERCENTILE = 0.9  # 문답 길이 상위 10%를 '길게 이어진 문답'으로 본다


def clean(fragment):
    """회의록 HTML 조각을 줄바꿈이 살아 있는 글로 바꾼다."""
    s = re.sub(r"(?i)<br\s*/?>|</p>|<hr\s*/?>", "\n", fragment)
    s = html.unescape(re.sub(r"<[^>]+>", "", s)).replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\s*\n\s*", "\n", s).strip()


def dept_of(title):
    """답변자 직함에서 부서명을 뽑는다. '행정과장' → '행정과'."""
    t = re.sub(r"\s*(직무대리|직무대행|권한대행|대행|대리)\s*", " ", title).strip()
    words = t.split()
    if not words:
        return ""
    if len(words) > 1 and words[-1] in BARE_TITLES:
        return " ".join(words[:-1])
    last = words[-1]
    if UNIT_SUFFIX.search(last):
        words[-1] = last[:-1]
    else:
        # 기관명에 직함이 붙어 있는 경우. 예) 용인도시공사사장 → 용인도시공사
        for tail in HEAD_TITLES:
            if last.endswith(tail) and len(last) > len(tail) + 2:
                words[-1] = last[:-len(tail)]
                break
    return " ".join(words)


def sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.?!])\s+|\n", text) if s.strip()]


def parse(doc):
    """회의록 한 건을 발언 목록으로 읽는다."""
    start = doc.find("id='minutes-body'")
    if start < 0:
        return []
    blocks = []
    for part in doc[start:].split("<div class='contents-block")[1:]:
        head, _, rest = part.partition(">")
        inner = rest.split("</div>", 1)[0]
        idx = re.search(r"data-con_idx='(\d+)'", head)
        code = re.search(r"data-member_code='([^']*)'", head)
        label = re.match(r"\s*<strong>(.*?)</strong>", inner, re.S)
        if "speaker-block" not in head or not label:
            continue
        who = clean(label.group(1)).lstrip("○").strip()
        text = clean(inner[label.end():])
        if not text:
            continue
        blocks.append({"idx": int(idx.group(1)) if idx else 0,
                       "code": code.group(1) if code else "", "who": who, "text": text})
    return blocks


def threads_of(blocks):
    """발언을 '의원 한 명의 질의 + 공무원 답변' 묶음으로 나눈다."""
    out, cur = [], None

    def close():
        nonlocal cur
        if cur and cur["d"]:
            out.append(cur)
        cur = None

    for b in blocks:
        if b["code"]:  # 의원 발언
            chair = "위원장" in b["who"]
            name = re.sub(r"(부?위원장|위원|의원|대리|직무)", "", b["who"]).strip()
            if not cur or cur["m"] != b["code"]:
                close()
                cur = {"i": b["idx"], "m": b["code"], "n": name, "chair": chair, "d": [], "b": []}
            cur["b"].append(["m", b["who"], b["text"], b["idx"]])
        else:  # 공무원 등 답변
            words = b["who"].split()
            title = " ".join(words[:-1]) if len(words) > 1 else b["who"]
            dept = dept_of(title)
            if not cur:
                cur = {"i": b["idx"], "m": "", "n": "", "chair": True, "d": [], "b": []}
            if dept and dept not in cur["d"]:
                cur["d"].append(dept)
            cur["b"].append(["o", b["who"], b["text"], b["idx"], dept])
    close()

    for t in out:
        longest = max((len(b[2]) for b in t["b"] if b[0] == "o"), default=0)
        # 위원장이 이끄는 묶음은 답변이 길면 업무보고(report), 짧으면 진행·질의(chair),
        # 그 밖의 의원이 이끄는 묶음은 질의(qa)로 본다.
        if t.pop("chair"):
            t["k"] = "report" if longest > 600 else "chair"
        else:
            t["k"] = "qa"
        t["p"], t["r"], t["u"], t["tp"] = [], [], [], []
        if t["k"] == "report":
            continue
        asked = "\n".join(b[2] for b in t["b"] if b[0] == "m")
        t["tp"] = [i for i, rx in enumerate(TOPIC_RES) if rx.search(asked)]
        for b in t["b"]:
            for s in sentences(b[2]):
                if b[0] == "o" and STUCK.search(s):
                    t["u"].append([b[4], s, b[3]])
                if b[0] == "o" and PROMISE.search(s) and len(s) > 12:
                    t["p"].append([b[4], s, b[3]])
                elif b[0] == "m" and REQUEST.search(s):
                    t["r"].append([s, b[3]])
    return out


# ---------- 결과보고서(지적사항) ----------

COMMITTEE_NAMES = ["의회운영위원회", "자치행정위원회", "문화복지위원회", "경제환경위원회", "도시건설위원회"]
KINDS = ("시정", "처리", "건의")


def grid_tables(doc):
    """HTML 표를 병합 칸(rowspan/colspan)을 풀어 2차원 목록으로 읽는다."""
    out = []
    for table in re.findall(r"(?is)<table\b.*?</table>", doc):
        grid, pending = [], {}
        for r, tr in enumerate(re.findall(r"(?is)<tr\b.*?</tr>", table)):
            cells = re.findall(r"(?is)<td\b([^>]*)>(.*?)</td>", tr)
            row, c, i = [], 0, 0
            while i < len(cells) or (r, c) in pending:
                if (r, c) in pending:
                    row.append(pending.pop((r, c)))
                    c += 1
                    continue
                attrs, inner = cells[i]
                i += 1
                cs = re.search(r'colspan="?(\d+)', attrs)
                rs = re.search(r'rowspan="?(\d+)', attrs)
                text = clean(inner)
                for _ in range(int(cs.group(1)) if cs else 1):
                    row.append(text)
                    for rr in range(1, int(rs.group(1)) if rs else 1):
                        pending[(r + rr, c)] = text
                    c += 1
            grid.append(row)
        out.append(grid)
    return out


def dept_key(name):
    """회의록과 보고서의 부서 표기를 맞춰 보기 위한 열쇠. 화면(index.html)과 같은 규칙."""
    return re.sub(r"\(.*?\)|\s", "", name).replace("구청", "구")


def report_depts(cell):
    """보고서의 담당부서 칸을 부서 열쇠 목록으로 바꾼다."""
    t = re.sub(r"\([^)]*\)?", "", cell)          # (16건), (의정팀) 같은 덧붙임 제거
    t = re.sub(r"\s", "", t)
    t = re.sub(r"보건소(?=.)", "", t)             # 처인구보건소보건정책과 → 처인구보건정책과
    m = re.match(r"(.*?)(\d),(\d)과$", t)        # 처인구도시건축1,2과
    parts = [m.group(1) + m.group(2) + "과", m.group(1) + m.group(3) + "과"] if m else \
        [x for x in re.split(r"[,·ㆍ/]", t) if x]
    every = re.match(r"(각구청?|3개구청?)(.+)", parts[0]) if parts else None
    keys = []
    for i, part in enumerate(parts):
        if every:
            rest = every.group(2) if i == 0 else part
            keys += [gu + rest for gu in ("처인구", "기흥구", "수지구")]
        else:
            keys.append(part.replace("구청", "구"))
    return keys


def findings_of(report, folder):
    """결과보고서 한 파일에서 지적사항 표를 찾아 읽는다."""
    out = []
    named = [c for c in COMMITTEE_NAMES if c in report["file"]]
    committee = named[0] if named else ""
    pages = int((folder / "done").read_text())
    dept = ""  # 표가 쪽을 넘어가면 부서 칸이 비어 있으므로 앞의 부서를 이어 쓴다
    for n in range(1, pages + 1):
        doc = (folder / f"{n}.html").read_text(encoding="utf-8")
        if not named:  # 통합본: 표 밖 글에 마지막으로 나온 위원회 이름을 따른다
            outside = re.sub(r"\s", "", clean(re.sub(r"(?is)<table\b.*?</table>", "", doc)))
            seen = [(outside.rfind(c), c) for c in COMMITTEE_NAMES if c in outside]
            if seen:
                committee = max(seen)[1]
        for grid in grid_tables(doc):
            if len(grid) < 2 or "지적" not in "".join(grid[0]):
                continue
            head = [re.sub(r"\s", "", h) for h in grid[0]]
            sub = [re.sub(r"\s", "", h) for h in grid[1]]
            try:
                c_dept = next(i for i, h in enumerate(head) if "부서" in h or "기관" in h)
                c_text = next(i for i, h in enumerate(head) if "지적" in h)
            except StopIteration:
                continue
            kind_cols = {k: sub.index(k) for k in KINDS if k in sub}
            c_kind = next((i for i, h in enumerate(head) if "비고" in h or "처리의견" in h), None)
            for row in grid[1:]:
                if len(row) <= max(c_dept, c_text) or re.sub(r"\s", "", row[c_text]) in ("", head[c_text], sub[c_text]):
                    continue
                if row[c_dept].strip():
                    dept = row[c_dept]
                kind = ""
                if kind_cols:
                    kind = next((k for k, i in kind_cols.items() if i < len(row) and row[i].strip()), "")
                elif c_kind is not None and c_kind < len(row):
                    kind = next((k for k in KINDS if k in row[c_kind]), "")
                text = re.sub(r"\s*\n\s*", " ", row[c_text]).strip()
                if out and out[-1]["t"] == text and out[-1]["raw_cell"] == dept:
                    continue  # 병합된 칸 때문에 같은 지적사항이 줄마다 반복되는 경우
                out.append({
                    "raw_cell": dept, "y": report["year"], "c": committee,
                    "raw": re.sub(r"\s+", " ", re.sub(r"\(\d+건\)", "", dept)).strip(),
                    "d": report_depts(dept), "k": kind,
                    "t": text,
                    "v": report["viewer"], "p": report["post"],
                })
    for f in out:
        del f["raw_cell"]
    return out


def build_findings(depts):
    path = CACHE / "reports.json"
    findings = []
    if path.exists():
        for report in json.loads(path.read_text(encoding="utf-8")):
            folder = CACHE / "reports" / str(report["viewer"])
            if (folder / "done").exists():
                findings += findings_of(report, folder)
    findings.sort(key=lambda f: -f["y"])
    (DATA / "findings.js").write_text(
        "window.COUNCIL_FINDINGS=" + json.dumps(findings, ensure_ascii=False, separators=(",", ":")) + ";\n",
        encoding="utf-8")
    known = {dept_key(d) for d in depts}
    matched = sum(1 for f in findings if known.intersection(f["d"]))
    print(f"결과보고서 지적사항 {len(findings)}건 (회의록 부서와 이름이 맞는 것 {matched}건)")
    return findings


def main():
    meta = json.loads((CACHE / "meetings.json").read_text(encoding="utf-8"))
    (DATA / "m").mkdir(parents=True, exist_ok=True)
    for old in (DATA / "m").glob("*.js"):
        old.unlink()

    depts = defaultdict(lambda: {"n": 0, "years": defaultdict(int), "uids": []})
    meetings = []
    topic_stat = [{"n": 0, "depts": set(), "cand": []} for _ in TOPICS]
    lengths = []
    for m in meta["meetings"]:
        path = CACHE / "minutes" / f"{m['uid']}.html"
        if not path.exists():
            continue
        threads = threads_of(parse(path.read_text(encoding="utf-8")))
        year = int(m["date"][:4])
        m = dict(m, year=year, threads=len(threads))
        meetings.append(m)
        if not threads:
            continue
        for t in threads:
            if t["k"] == "qa":
                lengths.append(len(t["b"]))
                for i in t["tp"]:
                    st = topic_stat[i]
                    st["n"] += 1
                    st["depts"].update(t["d"])
                    for b in t["b"]:
                        if b[0] != "m":
                            continue
                        for sent in sentences(b[2]):
                            if 40 <= len(sent) <= 170 and TOPIC_RES[i].search(sent) and \
                                    re.search(r"\?$|주세요\.?$|바랍니다\.?$|부탁드\S*$", sent):
                                st["cand"].append({"u": m["uid"], "i": b[3], "m": t["m"], "n": t["n"],
                                                   "d": t["d"][0], "y": year, "s": sent})
            for d in t["d"]:
                e = depts[d]
                if t["k"] == "qa":
                    e["n"] += 1
                    e["years"][year] += 1
                if m["uid"] not in e["uids"]:
                    e["uids"].append(m["uid"])
        body = json.dumps({"threads": threads}, ensure_ascii=False, separators=(",", ":"))
        (DATA / "m" / f"{m['uid']}.js").write_text(
            f"COUNCIL_MEETING({m['uid']},{body});\n", encoding="utf-8")

    index = {
        "generated": date.today().isoformat(),
        "meetings": meetings,
        "depts": {k: v for k, v in sorted(depts.items())},
        "currentMembers": meta.get("current_members", {}),
    }
    (DATA / "index.js").write_text(
        "window.COUNCIL_INDEX=" + json.dumps(index, ensure_ascii=False, separators=(",", ":")) + ";\n",
        encoding="utf-8")
    # 주제별로 다른 부서가 실제 받은 질문 예시를 고른다(최근 연도, 부서·의원이 겹치지 않게).
    current = set(meta.get("current_members", {}))
    topics = []
    for (name, _, prep), st in zip(TOPICS, topic_stat):
        picked, seen_d, seen_m = [], set(), defaultdict(int)
        for c in sorted(st["cand"], key=lambda c: (-c["y"], c["m"] not in current, -len(c["s"]))):
            if c["d"] in seen_d or seen_m[c["m"]] >= 2:
                continue
            seen_d.add(c["d"])
            seen_m[c["m"]] += 1
            picked.append(c)
            if len(picked) == 12:
                break
        topics.append({"name": name, "prep": prep, "n": st["n"], "depts": len(st["depts"]), "ex": picked})
    lengths.sort()
    long_at = lengths[int(len(lengths) * LONG_PERCENTILE)] if lengths else 0
    (DATA / "topics.js").write_text(
        "window.COUNCIL_TOPICS=" + json.dumps({"topics": topics, "longAt": long_at, "qa": len(lengths)},
                                              ensure_ascii=False, separators=(",", ":")) + ";\n", encoding="utf-8")
    print("단골 주제:", ", ".join(f"{t['name']} {t['n']}" for t in topics))
    build_findings(depts)
    size = sum(p.stat().st_size for p in DATA.rglob("*.js")) / 1e6
    print(f"회의록 {len(meetings)}건, 부서명 {len(depts)}개, 자료 크기 {size:.1f}MB")


if __name__ == "__main__":
    sys.exit(main())
