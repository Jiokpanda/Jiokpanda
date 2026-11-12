"""답변 자료 찾기.

자료 폴더의 문서(HWPX, PDF, 엑셀, CSV, 글 파일)를 읽어 대목 단위로 쪼개 두고,
질문이 들어오면 관련 대목을 찾아 준다. 찾기는 이 PC 안에서만 이뤄지며 자료를 밖으로 보내지 않는다.

- HWPX, 엑셀(xlsx), CSV, TXT/MD는 파이썬 기본 기능만으로 읽는다.
- PDF는 pypdf가 설치돼 있을 때만 읽는다(pip install pypdf). 글자가 없는 스캔본은 읽지 못한다.
- 구형 HWP는 읽지 못한다. 한글에서 HWPX로 다시 저장해야 한다.
"""
import csv
import io
import math
import re
import threading
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from xml.etree import ElementTree

CHUNK_CHARS = 500          # 한 대목의 대략적인 길이
MAX_ROWS = 20000           # 표 한 장에서 읽는 최대 줄 수
# 제목으로 보는 줄. ○, -, · 로 시작하는 줄은 제목 아래 딸린 내용으로 본다.
HEADING = re.compile(r"^\s*(제\s*\d+\s*[장절]|[0-9]{1,2}[.)]\s|[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩIVX]+[.)]\s|[가-하][.)]\s|[□■◆◇【\[<〈]|Q\s*\d*[.:)]|질의\s*\d|질문\s*\d)")
ARTICLE = re.compile(r"^\s*제\s*\d+\s*조(\s*의\s*\d+)?\s*[(（]")   # 조례·규정의 조문은 길어도 조 단위로 나눈다


def local(tag):
    return tag.rsplit("}", 1)[-1]


def squeeze(text):
    return re.sub(r"[ \t\xa0]+", " ", text).strip()


def group(lines, source, place):
    """글줄을 제목 단위로 묶어 대목으로 만든다. 제목처럼 보이는 줄에서 새 대목을 시작한다."""
    chunks, buf, title, start = [], [], "", 1
    def flush(n):
        if buf:
            chunks.append({"file": source, "loc": place(start, n), "title": title, "text": "\n".join(buf)})
    for n, line in enumerate(lines, 1):
        line = squeeze(line)
        if not line:
            continue
        article = ARTICLE.match(line)
        heading = article or (len(line) <= 60 and HEADING.match(line))
        if buf and (heading or sum(len(b) for b in buf) + len(line) > CHUNK_CHARS):
            flush(n - 1)
            buf, start = [], n
        if heading:
            title = line[:40] if article else line
        if not buf:
            start = n
        buf.append(line)
    flush(len(lines))
    return chunks


def table_chunks(rows, source, place):
    """표를 줄 단위 대목으로 만든다. 첫 줄을 머리글로 보고 '머리글: 값' 꼴로 적는다."""
    rows = [[squeeze(c) for c in r] for r in rows if any(squeeze(c) for c in r)]
    if not rows:
        return []
    head = rows[0]
    if len(rows) == 1 or len(head) == 1:
        return group([" | ".join(c for c in r if c) for r in rows], source, lambda a, b: place(a))
    out = []
    for n, row in enumerate(rows[1:MAX_ROWS], 2):
        cells = [(f"{head[i]}: {c}" if i < len(head) and head[i] and head[i] != c else c) for i, c in enumerate(row) if c]
        if cells:
            out.append({"file": source, "loc": place(n), "title": " | ".join(h for h in head if h)[:80], "text": " | ".join(cells)})
    return out


# ---------- 형식별 읽기 ----------

def read_hwpx(path, source):
    out = []
    with zipfile.ZipFile(path) as z:
        names = sorted(n for n in z.namelist() if re.match(r"Contents/section\d+\.xml$", n))
        for name in names:
            root = ElementTree.fromstring(z.read(name))
            lines, table_no = [], 0

            def text_of(node):          # 표 안의 글은 표에서 따로 읽으므로 건너뛴다
                parts = []
                def walk(e):
                    for child in e:
                        if local(child.tag) == "tbl":
                            continue
                        if local(child.tag) == "t":
                            parts.append("".join(child.itertext()))
                        walk(child)
                walk(node)
                return "".join(parts)

            def tables_in(node):
                found = []
                def walk(e):
                    for child in e:
                        if local(child.tag) == "tbl":
                            found.append(child)
                        else:
                            walk(child)
                walk(node)
                return found

            for para in [e for e in root if local(e.tag) == "p"]:
                line = text_of(para)
                if line.strip():
                    lines.append(line)
                for tbl in tables_in(para):
                    table_no += 1
                    grid = defaultdict(dict)
                    for tr in [e for e in tbl if local(e.tag) == "tr"]:
                        for tc in [e for e in tr if local(e.tag) == "tc"]:
                            addr = next((e for e in tc if local(e.tag) == "cellAddr"), None)
                            span = next((e for e in tc if local(e.tag) == "cellSpan"), None)
                            cell = " ".join(squeeze("".join(t.itertext())) for t in tc.iter() if local(t.tag) == "t").strip()
                            if addr is None:
                                continue
                            r0, c0 = int(addr.get("rowAddr", 0)), int(addr.get("colAddr", 0))
                            rs = int(span.get("rowSpan", 1)) if span is not None else 1
                            cs = int(span.get("colSpan", 1)) if span is not None else 1
                            for r in range(r0, r0 + rs):       # 병합된 칸은 걸친 줄마다 같은 글을 넣는다
                                for c in range(c0, c0 + cs):
                                    grid[r][c] = cell
                    width = max((max(cols) for cols in grid.values() if cols), default=-1) + 1
                    rows = [[grid[r].get(c, "") for c in range(width)] for r in sorted(grid)]
                    if len(rows) == 1 and width == 1:          # 한 칸짜리 표는 글 상자처럼 쓰인 것
                        lines.append(rows[0][0])
                    else:
                        out += table_chunks(rows, source, lambda n, t=table_no: f"표 {t}, {n}번째 줄")
            out += group(lines, source, lambda a, b: f"{a}번째 문단")
    return out


def read_xlsx(path, source):
    out = []
    ns = lambda e: local(e.tag)
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ElementTree.fromstring(z.read("xl/sharedStrings.xml")):
                shared.append("".join(t.text or "" for t in si.iter() if ns(t) == "t"))
        book = ElementTree.fromstring(z.read("xl/workbook.xml"))
        sheets = [e.get("name") for e in book.iter() if ns(e) == "sheet"]
        files = sorted((n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n)),
                       key=lambda n: int(re.search(r"(\d+)\.xml", n).group(1)))
        for i, name in enumerate(files):
            title = sheets[i] if i < len(sheets) else f"시트 {i + 1}"
            rows = []
            for row in ElementTree.fromstring(z.read(name)).iter():
                if ns(row) != "row":
                    continue
                cells = {}
                for c in row:
                    ref = re.match(r"[A-Z]+", c.get("r", "A"))
                    col = 0
                    for ch in (ref.group(0) if ref else "A"):
                        col = col * 26 + ord(ch) - 64
                    v = next((e for e in c if ns(e) == "v"), None)
                    if c.get("t") == "s" and v is not None:
                        text = shared[int(v.text)] if v.text and int(v.text) < len(shared) else ""
                    elif c.get("t") == "inlineStr":
                        text = "".join(t.text or "" for t in c.iter() if ns(t) == "t")
                    else:
                        text = v.text if v is not None and v.text else ""
                    cells[col] = text
                if cells:
                    rows.append([cells.get(k, "") for k in range(1, max(cells) + 1)])
                if len(rows) >= MAX_ROWS:
                    break
            out += table_chunks(rows, source, lambda n, t=title: f"{t} 시트, {n}행")
    return out


def read_csv(path, source):
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "cp949", "utf-16"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeError:
            continue
    else:
        text = raw.decode("utf-8", "replace")
    rows = list(csv.reader(io.StringIO(text)))[:MAX_ROWS]
    return table_chunks(rows, source, lambda n: f"{n}행")


def read_text(path, source):
    raw = Path(path).read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError:
        text = raw.decode("cp949", "replace")
    return group(text.splitlines(), source, lambda a, b: f"{a}번째 줄")


def read_pdf(path, source):
    try:
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError("PDF를 읽으려면 pypdf가 필요합니다 (pip install pypdf)")
    out = []
    for n, page in enumerate(PdfReader(str(path)).pages, 1):
        lines = (page.extract_text() or "").splitlines()
        out += group(lines, source, lambda a, b, p=n: f"{p}쪽")
    if not out:
        raise RuntimeError("글자를 찾지 못했습니다(스캔한 문서일 수 있습니다)")
    return out


READERS = {".hwpx": read_hwpx, ".xlsx": read_xlsx, ".csv": read_csv, ".txt": read_text, ".md": read_text, ".pdf": read_pdf}
NOTES = {".hwp": "구형 HWP는 읽지 못합니다. 한글에서 HWPX로 다시 저장해 주세요.",
         ".xls": "구형 XLS는 읽지 못합니다. XLSX나 CSV로 다시 저장해 주세요.",
         ".docx": "DOCX는 아직 읽지 못합니다. PDF나 글 파일로 저장해 주세요."}


# ---------- 찾기 ----------

def grams(text):
    """한글은 띄어쓰기와 조사가 제각각이라 두 글자씩 겹쳐 자른 조각으로 맞춘다."""
    out = []
    for word in re.findall(r"[가-힣]+|[A-Za-z]+|\d[\d,.]*", text.lower()):
        if re.match(r"[가-힣]", word):
            out += [word[i:i + 2] for i in range(len(word) - 1)] or [word]
        else:
            out.append(word)
    return out


class Index:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.lock = threading.Lock()
        self.chunks, self.files = [], []
        self.posting, self.lengths, self.average = {}, [], 1.0

    def load(self):
        chunks, files = [], []
        paths = sorted(p for p in self.folder.rglob("*") if p.is_file() and not p.name.startswith(("~", "."))) \
            if self.folder.is_dir() else []
        for path in paths:
            name = str(path.relative_to(self.folder)).replace("\\", "/")
            ext = path.suffix.lower()
            if ext in READERS:
                try:
                    got = READERS[ext](path, name)
                    files.append({"name": name, "chunks": len(got), "error": ""})
                    chunks += got
                except Exception as e:                 # 한 파일이 깨져도 나머지는 읽는다
                    files.append({"name": name, "chunks": 0, "error": str(e)[:200]})
            elif ext in NOTES:
                files.append({"name": name, "chunks": 0, "error": NOTES[ext]})
        posting, lengths = defaultdict(list), []
        for i, c in enumerate(chunks):
            counts = Counter(grams(c["title"] + " " + c["text"]))
            lengths.append(sum(counts.values()))
            for g, n in counts.items():
                posting[g].append((i, n))
        with self.lock:
            self.chunks, self.files = chunks, files
            self.posting, self.lengths = dict(posting), lengths
            self.average = (sum(lengths) / len(lengths)) if lengths else 1.0
        return self.status()

    def status(self):
        with self.lock:
            return {"folder": str(self.folder), "exists": self.folder.is_dir(), "files": self.files, "chunks": len(self.chunks)}

    def search(self, query, k=5):
        """질문과 가장 관련 있는 대목을 돌려준다. 질문의 끝부분(실제 물음)에 무게를 더 둔다."""
        query = squeeze(query)
        weights = Counter(grams(query))
        for g in grams(query[-80:]):
            weights[g] += 1
        with self.lock:
            total, scores = len(self.chunks), defaultdict(float)
            for g, w in weights.items():
                hits = self.posting.get(g)
                if not hits:
                    continue
                idf = math.log(1 + (total - len(hits) + 0.5) / (len(hits) + 0.5))
                for i, n in hits:
                    scores[i] += w * idf * n * 2.2 / (n + 1.2 * (0.25 + 0.75 * self.lengths[i] / self.average))
            best = sorted(scores.items(), key=lambda kv: -kv[1])[:k]
            top = best[0][1] if best else 0
            return [dict(self.chunks[i], score=round(s, 2), id=i) for i, s in best if s >= top * 0.35]
