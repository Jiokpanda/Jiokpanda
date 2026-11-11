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
# 끝의 '장'을 떼면 부서명이 되는 직함. 예) 행정과장 → 행정과
UNIT_SUFFIX = re.compile(r"(과|국|실|소|팀|단|본부|원|관|센터|구청|청|동|읍|면|처|부|담당|도서관|박물관|사업소)장$")

# 답변에서 '하겠다'고 한 문장을 찾는다. 인사·보고 같은 의례적 표현은 뺀다.
PROMISE = re.compile(
    r"(검토|조치|시정|보완|개선|제출|확인|반영|마련|추진|정비|점검|협의|노력|챙기|챙겨|살펴|강구|수정|바로잡|신경|"
    r"개정|변경|조정|파악|조사|분석|준비|수립|강화|확대|홍보|관리)[^.\n]{0,22}겠습니다")
REQUEST = re.compile(r"자료[^.\n]{0,40}(제출|주시|주십|주세요|주셨|부탁|요청|요구|보내|갖다|가져다|받아)")


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
        t["p"], t["r"] = [], []
        if t["k"] == "report":
            continue
        for b in t["b"]:
            for s in sentences(b[2]):
                if b[0] == "o" and PROMISE.search(s) and len(s) > 12:
                    t["p"].append([b[4], s, b[3]])
                elif b[0] == "m" and REQUEST.search(s):
                    t["r"].append([s, b[3]])
    return out


def main():
    meta = json.loads((CACHE / "meetings.json").read_text(encoding="utf-8"))
    (DATA / "m").mkdir(parents=True, exist_ok=True)
    for old in (DATA / "m").glob("*.js"):
        old.unlink()

    depts = defaultdict(lambda: {"n": 0, "years": defaultdict(int), "uids": []})
    meetings = []
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
            for d in t["d"]:
                e = depts[d]
                if t["k"] != "report":
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
    size = sum(p.stat().st_size for p in DATA.rglob("*.js")) / 1e6
    print(f"회의록 {len(meetings)}건, 부서명 {len(depts)}개, 자료 크기 {size:.1f}MB")


if __name__ == "__main__":
    sys.exit(main())
