"""답변 자료 찾기.

자료 폴더의 문서(HWPX, PDF, 엑셀, CSV, 글 파일)를 읽어 대목 단위로 쪼개 두고,
질문이 들어오면 관련 대목을 찾아 준다. 찾기는 이 PC 안에서만 이뤄지며 자료를 밖으로 보내지 않는다.

- HWPX, 엑셀(xlsx), CSV, TXT/MD는 파이썬 기본 기능만으로 읽는다.
- PDF는 pypdf가 설치돼 있을 때만 읽는다(pip install pypdf). 글자가 없는 스캔본은 읽지 못한다.
- 구형 HWP는 읽지 못한다. 한글에서 HWPX로 다시 저장해야 한다.
"""
import csv
import hashlib
import io
import json
import math
import os
import re
import threading
import time
import zipfile
from array import array
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
    rows = []
    for row in csv.reader(io.StringIO(text)):
        rows.append(row)
        if len(rows) >= MAX_ROWS:
            break
    return table_chunks(rows, source, lambda n: f"{n}행")


def read_text(path, source):
    raw = Path(path).read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError:
        text = raw.decode("cp949", "replace")
    return group(text.splitlines(), source, lambda a, b: f"{a}번째 줄")


PDF_PAGES_PER_TASK = 25    # PDF는 쪽 읽기가 느려서 이만큼씩 나눠 여러 코어에서 읽는다


def pdf_pages(path):
    try:
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError("PDF를 읽으려면 pypdf가 필요합니다 (pip install pypdf)")
    return len(PdfReader(str(path)).pages)


def read_pdf(path, source, first=0, last=None):
    from pypdf import PdfReader
    pages = PdfReader(str(path)).pages
    out = []
    for n in range(first, len(pages) if last is None else min(last, len(pages))):
        lines = (pages[n].extract_text() or "").splitlines()
        out += group(lines, source, lambda a, b, p=n + 1: f"{p}쪽")
    return out


def run_task(task):
    """파일 하나(또는 PDF의 쪽 범위 하나)를 읽는다. 다른 프로세스에서도 불리므로 모듈 맨 위에 둔다."""
    path, source, first, last = task
    try:
        if first is None:
            return task, READERS[Path(path).suffix.lower()](path, source), ""
        return task, read_pdf(path, source, first, last), ""
    except Exception as e:                     # 한 파일이 깨져도 나머지는 읽는다
        return task, [], str(e)[:200]


READERS = {".hwpx": read_hwpx, ".xlsx": read_xlsx, ".csv": read_csv, ".txt": read_text, ".md": read_text,
           ".pdf": lambda path, source: read_pdf(path, source)}
NOTES = {".hwp": "구형 HWP는 읽지 못합니다. 한글에서 HWPX로 다시 저장해 주세요.",
         ".xls": "구형 XLS는 읽지 못합니다. XLSX나 CSV로 다시 저장해 주세요.",
         ".docx": "DOCX는 아직 읽지 못합니다. PDF나 글 파일로 저장해 주세요."}


# ---------- 찾기 ----------

# 질문에서 '묻는 말투'는 답이 든 대목을 찾는 데 방해만 된다. (설문 문항처럼 말투가 같은 대목이 걸려 나온다.)
ASK_WORDS = re.compile(
    r"얼마나|얼마|어떻게|어느 ?정도|무엇|무슨|어떤|언제|어디|왜|몇|"
    r"됩니까|입니까|습니까|합니까|십니까|나요|인가요|는지요|인지요|지요|"
    r"말씀해 ?주\S*|답변해 ?주\S*|설명해 ?주\S*|알려 ?주\S*|주시기 ?바랍니다|주십시오|주세요|"
    r"과장님|국장님|소장님|팀장님|위원장님|그러면|그런데|그래서|혹시|지금|현재|관련해서|대해서|대하여|있는지|되는지|하는지|"
    r"관해서?|관하여|관한|대한|대해|관련|질문|생각\S*|말씀\S*|의견|입장|설명|답변")
# 낱말 끝에 붙은 조사와 '~하다' 꼴 어미. 떼고 나서 줄기만 맞춰 본다.
PARTICLE = re.compile(r"(으로서|으로써|에서는|에서도|에서|에게|으로|까지|부터|이나|이란|이라는|처럼|마다|은|는|이|가|을|를|의|에|도|과|와|로|만)$")
VERB_TAIL = re.compile(r"^(.{2,}?)(하[가-힣]*|되[가-힣]*|했[가-힣]*|됐[가-힣]*)$")


def stems(core):
    """질문에서 내용 낱말의 줄기를 순서대로 뽑는다. 예) '플랫폼 예산은' → ['플랫폼', '예산']"""
    out = []
    for word in re.findall(r"[가-힣A-Za-z0-9]{2,}", core):
        if re.match(r"[가-힣]", word):
            word = PARTICLE.sub("", word) if len(PARTICLE.sub("", word)) >= 2 else word
            m = VERB_TAIL.match(word)
            word = m.group(1) if m else word
        if len(word) >= 2 and word not in out:
            out.append(word)
    return out[-6:]
# 같은 뜻으로 섞여 쓰이는 낱말. 질문에 하나가 나오면 나머지도 함께 찾는다.
SYNONYMS = [
    ("사용자", "이용자", "가입자", "사용 인원", "이용 인원", "접속자", "계정"),
    ("예산", "사업비", "소요액", "소요예산", "예산액"),
    ("실적", "현황", "통계", "추이"),
    ("비용", "금액", "요금", "단가"),
    ("직원", "공무원", "인력"),
    ("만족도", "만족"),
    ("집행률", "집행액", "집행 현황"),
    ("민원", "불편", "건의"),
    ("교육", "연수", "수료"),
    ("계획", "방안", "대책"),
    ("잔여", "잔액", "남은", "남았", "잔량"),
]
WANTS_NUMBER = re.compile(r"얼마|몇|수는|수가|현황|규모|비율|률|금액|예산|건수|인원|통계|실적")
LOOKS_LIKE_QUESTION = re.compile(r"\?|십니까|습니까|입니까|주십시오|선택해 ?주")
# 수치로 치는 것: 단위가 붙은 숫자. '3개월', '2026년' 같은 기간·날짜는 설문지나 머리말에도 흔해서 뺀다.
HAS_FIGURE = re.compile(r"\d[\d,.]*\s*(명|건|원|천원|백만원|억|%|％|회|개(?!월)|곳|대|권|시간)")

def grams(text):
    """한글은 띄어쓰기와 조사가 제각각이라 두 글자씩 겹쳐 자른 조각으로 맞춘다."""
    out = []
    for word in re.findall(r"[가-힣]+|[A-Za-z]+|\d[\d,.]*", text.lower()):
        if re.match(r"[가-힣]", word):
            out += [word[i:i + 2] for i in range(len(word) - 1)] or [word]
        else:
            out.append(word)
    return out


# ---------- 뜻으로 찾기(임베딩) ----------

class EmbedError(Exception):
    def __init__(self, status, detail):
        super().__init__(f"응답 코드 {status}: {detail}")
        self.status = status


def chunk_text(c):
    """임베딩 모델에 보내는 글. 대목만 보내면 무슨 문서의 일부인지 모르므로 파일 이름과 문서 첫머리를 앞에 붙인다."""
    name = c["file"].rsplit("/", 1)[-1].rsplit(".", 1)[0]
    doc = c.get("doc", "")
    head = c["title"] if c["title"] and c["title"] not in c["text"] else ""
    lines = [name] + ([doc] if doc and doc not in c["text"] else []) + ([head] if head else []) + [c["text"]]
    return "\n".join(lines)[:1500]


def chunk_key(c):
    return hashlib.sha1(chunk_text(c).encode("utf-8")).hexdigest()[:20]


def unit(vec):
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class Vectors:
    """대목의 벡터를 이 PC의 파일 두 개(열쇠 목록, 숫자 덩어리)에 담아 둔다. 별도 벡터DB는 쓰지 않는다."""

    def __init__(self, cache_dir, model):
        tag = re.sub(r"[^A-Za-z0-9._-]", "_", model)
        self.keys_path = Path(cache_dir) / f"vectors_{tag}.json"
        self.data_path = Path(cache_dir) / f"vectors_{tag}.bin"
        self.model, self.dim, self.keys, self.row, self.data = model, 0, [], {}, array("f")
        self.matrix = None
        try:
            meta = json.loads(self.keys_path.read_text(encoding="utf-8"))
            data = array("f")
            data.frombytes(self.data_path.read_bytes())
            if meta["dim"] and len(data) == meta["dim"] * len(meta["keys"]):
                self.dim, self.keys, self.data = meta["dim"], meta["keys"], data
                self.row = {k: i for i, k in enumerate(self.keys)}
        except (OSError, ValueError, KeyError):
            pass

    def add(self, key, vec):
        if not self.dim:
            self.dim = len(vec)
        if len(vec) != self.dim or key in self.row:
            return
        self.row[key] = len(self.keys)
        self.keys.append(key)
        self.data.extend(unit(vec))
        self.matrix = None

    def save(self):
        self.keys_path.parent.mkdir(parents=True, exist_ok=True)
        self.data_path.write_bytes(self.data.tobytes())
        self.keys_path.write_text(json.dumps({"model": self.model, "dim": self.dim, "keys": self.keys}), encoding="utf-8")

    def clear(self):
        for path in (self.keys_path, self.data_path):
            try:
                path.unlink()
            except OSError:
                pass
        self.dim, self.keys, self.row, self.data, self.matrix = 0, [], {}, array("f"), None

    def nearest(self, qvec, rows, n):
        """rows(대목 번호 → 벡터 줄 번호) 가운데 질문 벡터와 가장 가까운 n개를 (대목 번호, 가까운 정도)로 돌려준다."""
        if not self.dim or len(qvec) != self.dim:
            return []
        q = unit(qvec)
        try:                                   # numpy가 있으면 그것으로, 없으면 파이썬만으로 계산한다
            import numpy
            if self.matrix is None:
                self.matrix = numpy.frombuffer(self.data, dtype=numpy.float32).reshape(-1, self.dim)
            sims = self.matrix @ numpy.asarray(q, dtype=numpy.float32)
            scored = [(i, float(sims[r])) for i, r in rows]
        except ImportError:
            dot = getattr(math, "sumprod", None) or (lambda a, b: sum(x * y for x, y in zip(a, b)))
            d, data = self.dim, self.data
            scored = [(i, dot(q, data[r * d:(r + 1) * d])) for i, r in rows]
        scored.sort(key=lambda kv: -kv[1])
        return scored[:n]


class Index:
    def __init__(self, folder, cache_dir=None, embed_model=""):
        self.folder = Path(folder)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.lock = threading.Lock()
        self.chunks, self.files = [], []
        self.posting, self.lengths, self.average = {}, [], 1.0
        self.asking, self.figures, self.same = [], [], []     # 대목별: 질문 모양인지, 수치가 몇 개인지, 같은 내용 묶음
        self.file_of, self.file_size, self.group_of, self.groups = [], [], [], []   # 대목이 든 파일, 파일별 대목 수, 폴더 묶음
        self.loading, self.progress, self.seconds = False, {"done": 0, "total": 0, "current": ""}, 0.0
        self.vectors = Vectors(self.cache_dir, embed_model) if self.cache_dir and embed_model else None
        self.rows = []                                   # (대목 번호, 벡터 줄 번호): 벡터가 있는 대목만
        self.embedding = {"building": False, "done": 0, "total": 0, "skipped": 0, "error": ""}
        self.embed_stop = threading.Event()

    def start(self):
        """뒤에서 읽기 시작한다. 읽는 동안에도 상태 조회와 (이전 색인으로) 찾기는 된다."""
        with self.lock:
            if self.loading:
                return
            self.loading = True
            self.progress = {"done": 0, "total": 0, "current": ""}
        threading.Thread(target=self.load, daemon=True).start()

    def _cache_path(self, path):
        if not self.cache_dir:
            return None
        st = path.stat()
        key = f"v1|{path}|{st.st_mtime_ns}|{st.st_size}"
        return self.cache_dir / (hashlib.sha1(key.encode("utf-8")).hexdigest() + ".json")

    def load(self):
        began = time.time()
        with self.lock:
            self.loading = True
        paths = sorted(p for p in self.folder.rglob("*") if p.is_file() and not p.name.startswith(("~", "."))) \
            if self.folder.is_dir() else []
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        results, notes, tasks, parts = {}, {}, [], {}
        for path in paths:
            name = str(path.relative_to(self.folder)).replace("\\", "/")
            ext = path.suffix.lower()
            if ext in NOTES:
                notes[name] = NOTES[ext]
                continue
            if ext not in READERS:
                continue
            cached = self._cache_path(path)
            if cached and cached.exists():                    # 바뀌지 않은 파일은 지난번에 읽은 것을 쓴다
                try:
                    results[name] = json.loads(cached.read_text(encoding="utf-8"))
                    continue
                except (ValueError, OSError):
                    pass
            if ext == ".pdf":
                try:
                    count = pdf_pages(path)
                except Exception as e:
                    notes[name] = str(e)[:200]
                    continue
                ranges = [(i, min(i + PDF_PAGES_PER_TASK, count)) for i in range(0, count, PDF_PAGES_PER_TASK)]
                parts[name] = {"path": path, "need": len(ranges), "got": {}, "error": ""}
                tasks += [(str(path), name, a, b) for a, b in ranges]
            else:
                parts[name] = {"path": path, "need": 1, "got": {}, "error": ""}
                tasks.append((str(path), name, None, None))

        def finish(task, chunks, error):
            name, first = task[1], task[2] or 0
            part = parts[name]
            part["got"][first] = chunks
            part["error"] = part["error"] or error
            with self.lock:
                self.progress = {"done": self.progress["done"] + 1, "total": len(tasks), "current": name}
            if len(part["got"]) == part["need"]:
                merged = [c for k in sorted(part["got"]) for c in part["got"][k]]
                if part["error"]:
                    notes[name] = part["error"]
                elif not merged and name.lower().endswith(".pdf"):
                    notes[name] = "글자를 찾지 못했습니다(스캔한 문서일 수 있습니다)"
                else:
                    results[name] = merged
                    cached = self._cache_path(part["path"])
                    if cached:
                        try:
                            cached.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
                        except OSError:
                            pass

        with self.lock:
            self.progress = {"done": 0, "total": len(tasks), "current": ""}
        done = set()
        if len(tasks) > 1 and (os.cpu_count() or 1) > 1:
            try:                                              # 여러 코어로 나눠 읽는다. 안 되는 환경이면 하나씩 읽는다.
                from concurrent.futures import ProcessPoolExecutor, as_completed
                with ProcessPoolExecutor(max_workers=min(8, os.cpu_count() or 1)) as pool:
                    for fut in as_completed([pool.submit(run_task, t) for t in tasks]):
                        task, chunks, error = fut.result()
                        finish(task, chunks, error)
                        done.add(task)
            except Exception:
                pass
        for task in tasks:
            if task not in done:
                finish(*run_task(task))

        chunks, files = [], []
        for path in paths:
            name = str(path.relative_to(self.folder)).replace("\\", "/")
            if name in results:
                files.append({"name": name, "chunks": len(results[name]), "error": ""})
                chunks += results[name]
            elif name in notes:
                files.append({"name": name, "chunks": 0, "error": notes[name]})
        first = {}                                   # 파일별 문서 첫머리(제목 줄)
        for c in chunks:
            if c["file"] not in first:
                first[c["file"]] = squeeze((c["text"].splitlines() or [""])[0])[:70]
            c["doc"] = first[c["file"]]
        posting, lengths, asking, figures, same = defaultdict(list), [], [], [], []
        for i, c in enumerate(chunks):
            name = c["file"].rsplit("/", 1)[-1].rsplit(".", 1)[0]          # 파일 이름도 찾는 대상에 넣는다
            counts = Counter(grams(name + " " + c["title"] + " " + c["text"]))
            lengths.append(sum(counts.values()))
            for g, n in counts.items():
                posting[g].append((i, n))
            marks, nums = len(LOOKS_LIKE_QUESTION.findall(c["text"])), len(HAS_FIGURE.findall(c["text"]))
            asking.append(marks >= 1 and nums <= marks)           # 물음만 있고 수치는 거의 없는 대목(설문지 등)
            figures.append(nums)
            same.append(re.sub(r"\s", "", c["text"])[:160])
        with self.lock:
            self.chunks, self.files = chunks, files
            self.posting, self.lengths = dict(posting), lengths
            self.asking, self.figures, self.same = asking, figures, same
            # 대목이 어느 파일·어느 폴더에 들었는지. 폴더 묶음은 모든 파일이 함께 쓰는 윗폴더를 뗀 첫 폴더 이름이다.
            names = sorted({c["file"] for c in chunks})
            number = {n: i for i, n in enumerate(names)}
            parts = [n.split("/")[:-1] for n in names]
            skip = 0
            while parts and all(len(p) > skip for p in parts) and len({p[skip] for p in parts}) == 1:
                skip += 1
            label = {n: (p[skip] if len(p) > skip else "(폴더 없음)") for n, p in zip(names, parts)}
            self.file_of = [number[c["file"]] for c in chunks]
            self.file_size = [0] * len(names)
            for f in self.file_of:
                self.file_size[f] += 1
            self.group_of = [label[c["file"]] for c in chunks]
            counts = Counter(self.group_of)
            self.groups = [{"name": g, "chunks": n} for g, n in sorted(counts.items())]
            self._align()
            self.average = (sum(lengths) / len(lengths)) if lengths else 1.0
            self.loading, self.seconds = False, round(time.time() - began, 1)
        return self.status()

    def _align(self):
        """읽어 둔 대목과 저장된 벡터를 짝짓는다(잠금을 쥔 상태에서 부른다)."""
        self.rows = []
        if self.vectors and self.vectors.dim:
            row = self.vectors.row
            self.rows = [(i, row[k]) for i, k in ((i, chunk_key(c)) for i, c in enumerate(self.chunks)) if k in row]

    def embed_start(self, embed):
        """벡터가 없는 대목을 묶음으로 보내 벡터를 받아 온다. embed(글 목록) → 벡터 목록."""
        with self.lock:
            if not self.vectors or self.embedding["building"] or self.loading:
                return
            have = self.vectors.row
            todo, seen = [], set()
            for c in self.chunks:
                k = chunk_key(c)
                if k not in have and k not in seen:
                    seen.add(k)
                    todo.append((k, chunk_text(c)))
            self.embedding = {"building": True, "done": 0, "total": len(todo), "skipped": 0, "error": ""}
            self.embed_stop.clear()

        def send(batch, depth=0):
            """묶음이 거절되면 반으로 나눠 다시 보내, 문제가 되는 대목만 건너뛴다."""
            for attempt in range(6):
                if self.embed_stop.is_set():
                    return
                try:
                    vecs = embed([t for _, t in batch])
                    with self.lock:
                        for (k, _), v in zip(batch, vecs):
                            self.vectors.add(k, v)
                        self.embedding["done"] += len(batch)
                    return
                except EmbedError as e:
                    if e.status == 429 or e.status >= 500 or e.status == 0:     # 잠시 뒤 다시
                        time.sleep(4 + attempt * 4)
                        continue
                    if e.status in (401, 402, 403, 404) and depth == 0 and not self.vectors.keys:
                        raise                                               # 키·크레딧·모델 문제는 계속해도 소용없다
                    if len(batch) > 1:
                        half = len(batch) // 2
                        send(batch[:half], depth + 1)
                        send(batch[half:], depth + 1)
                    else:
                        with self.lock:
                            self.embedding["done"] += 1
                            self.embedding["skipped"] += 1
                            self.embedding["error"] = str(e)[:200]
                    return
            with self.lock:
                self.embedding["done"] += len(batch)
                self.embedding["skipped"] += len(batch)

        def work():
            try:
                for n in range(0, len(todo), 48):
                    if self.embed_stop.is_set():
                        break
                    send(todo[n:n + 48])
                    if n % 960 == 0:
                        with self.lock:
                            self.vectors.save()                      # 중간에 끊겨도 이어서 할 수 있게 틈틈이 저장
                    time.sleep(0.6)                                  # 게이트웨이의 분당 요청 한도 안쪽으로
            except EmbedError as e:
                with self.lock:
                    self.embedding["error"] = str(e)[:300]
            with self.lock:
                self.vectors.save()
                self._align()
                self.embedding["building"] = False
        threading.Thread(target=work, daemon=True).start()

    def embed_clear(self):
        self.embed_stop.set()
        with self.lock:
            if self.vectors:
                self.vectors.clear()
            self.rows = []
            self.embedding = {"building": False, "done": 0, "total": 0, "skipped": 0, "error": ""}

    def status(self):
        with self.lock:
            return {"folder": str(self.folder), "exists": self.folder.is_dir(), "files": self.files, "chunks": len(self.chunks),
                    "loading": self.loading, "progress": dict(self.progress), "seconds": self.seconds, "groups": self.groups,
                    "embed": dict(self.embedding, model=self.vectors.model if self.vectors else "", have=len(self.rows),
                                  dim=self.vectors.dim if self.vectors else 0)}

    def _having(self, word):
        """낱말(또는 같은 뜻의 낱말)이 들어 있는 대목 번호 모음."""
        found = set()
        flat = re.sub(r"\s", "", word)
        variants = {word}
        for group in SYNONYMS:
            if any(re.sub(r"\s", "", w) == flat for w in group):
                variants.update(group)
        for v in variants:
            pieces = set(grams(v))
            hit = None
            for g in pieces:
                ids = {i for i, _ in self.posting.get(g, ())}
                hit = ids if hit is None else hit & ids
                if not hit:
                    break
            found |= hit or set()
        return found

    def search(self, query, k=5, extra=(), cutoff=0.3, qvec=None, exclude=()):
        """질문에 답이 될 만한 대목을 돌려준다.

        - 질문의 말투는 떼고 내용 낱말만으로 찾되, 같은 뜻의 낱말도 함께 찾는다.
        - 대목이 든 문서도 본다. '플랫폼 예산'을 물으면, 대목에는 '예산'만 있어도 그 문서가 플랫폼을 다루는
          문서이면 플랫폼도 맞은 것으로 쳐 준다. 반대로 질문의 마지막 낱말(묻는 대상)이 대목에 없으면 뒤로 보낸다.
        - 물음만 있는 대목(설문 문항 등)은 뒤로, 수치를 묻는 질문이면 수치가 든 대목을 앞으로 보낸다.
        - 내용이 같은 대목은 하나만 남긴다.
        extra: 추가로 찾을 낱말(예: AI가 넓혀 준 검색어). cutoff: 1위 점수의 이 비율에 못 미치면 버린다(0이면 안 버림).
        qvec: 질문의 벡터. 주면 뜻이 가까운 대목도 함께 찾아 합친다. exclude: 찾지 않을 폴더 묶음 이름.
        """
        query = squeeze(query)
        core = squeeze(ASK_WORDS.sub(" ", query)) or query
        weights = Counter()
        for g in grams(core):
            weights[g] += 1.0
        for g in grams(core[-60:]):                  # 끝부분이 실제 물음인 경우가 많다
            weights[g] += 0.5
        flat = re.sub(r"\s", "", core)
        for group in SYNONYMS:
            if any(re.sub(r"\s", "", w) in flat for w in group):
                for w in group:
                    for g in grams(w):
                        weights[g] = max(weights[g], 0.7)
        for term in extra:
            for g in grams(str(term)):
                weights[g] = max(weights[g], 0.8)
        wants_number = bool(WANTS_NUMBER.search(query))
        words = stems(core)
        skip = set(exclude)
        with self.lock:
            total, scores = len(self.chunks), defaultdict(float)
            idf = {}
            for g, w in weights.items():
                hits = self.posting.get(g)
                if not hits:
                    continue
                idf[g] = math.log(1 + (total - len(hits) + 0.5) / (len(hits) + 0.5))
                for i, n in hits:
                    scores[i] += w * idf[g] * n * 2.2 / (n + 1.2 * (0.25 + 0.75 * self.lengths[i] / self.average))
            if skip:
                scores = defaultdict(float, {i: sc for i, sc in scores.items() if self.group_of[i] not in skip})
            # 문서 맥락: 낱말이 대목에는 없어도 그 문서 곳곳에 나오면(문서의 3% 이상, 2곳 이상) 일부 맞은 것으로 친다.
            if len(words) > 1 and scores:
                having = {w: self._having(w) for w in words}
                per_file = {}
                for w, ids in having.items():
                    count = Counter(self.file_of[i] for i in ids)
                    per_file[w] = {f: min(0.8, 5.0 * n / self.file_size[f]) for f, n in count.items()
                                   if n >= 2 and n / self.file_size[f] >= 0.03}
                worth = {w: sum(weights[g] * idf.get(g, 0.0) for g in set(grams(w))) for w in words}
                target, whole = words[-1], sum(worth.values()) or 1.0
                for i in scores:
                    f, covered = self.file_of[i], 0.0
                    for w in words:
                        credit = 1.0 if i in having[w] else per_file[w].get(f, 0.0)
                        covered += credit * worth[w]
                        if i not in having[w]:
                            scores[i] += credit * worth[w]
                    # 질문의 낱말 가운데 대목에도 문서에도 없는 것이 많을수록 뒤로 보낸다(드문 낱말일수록 크게).
                    scores[i] *= 0.35 + 0.65 * covered / whole
                    if having[target] and i not in having[target]:
                        scores[i] *= 0.45                 # 묻는 대상이 대목에 없으면 답일 가능성이 낮다
            via = {}
            if qvec and self.rows:
                # 낱말 점수와 뜻 점수를 각각 0~1로 맞춘 뒤 6:4로 더한다. 한쪽에서만 높아도 올라오고,
                # 둘 다 높으면 더 올라온다. (등수만 쓰면 점수 차이가 사라져 뚜렷한 1위가 묻힌다.)
                near = self.vectors.nearest(qvec, self.rows, 150)
                words = sorted(scores.items(), key=lambda kv: -kv[1])[:150]
                fused = defaultdict(float)
                best = words[0][1] if words else 1.0
                for i, sc in words:
                    fused[i] += 0.6 * sc / best
                    via[i] = "낱말"
                if near:
                    high, low = near[0][1], near[-1][1]
                    span = (high - low) or 1.0
                    for i, sim in near:
                        part = 0.4 * (sim - low) / span
                        if part > 0.16:
                            via[i] = "낱말+뜻" if i in via else "뜻"
                        fused[i] += part
                scores = fused
            # 물음만 있는 대목(설문 문항 등)은 뒤로, 수치가 든 대목은 앞으로. 뜻으로 찾아도 설문 문항은 질문과
            # 가깝게 나오므로, 합친 뒤에 적용해야 한다.
            for i in scores:
                scores[i] *= 1.0 + min(self.figures[i], 4) * (0.08 if wants_number else 0.03)
                if self.asking[i]:
                    scores[i] *= 0.4
            out, seen = [], {}
            for i, sc in sorted(scores.items(), key=lambda kv: -kv[1]):
                key = self.same[i]
                if key in seen:                      # 같은 내용이 여러 파일에 있으면 처음 것에 파일 수만 더한다
                    seen[key]["copies"] += 1
                    continue
                if out and sc < out[0]["score"] * cutoff:
                    break
                item = dict(self.chunks[i], score=round(sc, 4), id=i, copies=0, via=via.get(i, ""))
                seen[key] = item
                out.append(item)
                if len(out) >= k:
                    break
            return out
