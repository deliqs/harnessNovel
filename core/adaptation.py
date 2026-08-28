import os
import re


DEFAULT_FORBIDDEN_TERMS = [
    "\u897f\u6e38",
    "\u65b9\u5bf8\u5c71",
    "\u659c\u6708\u4e09\u661f\u6d1e",
    "\u83e9\u63d0\u7956\u5e08",
    "\u5b59\u609f\u7a7a",
    "\u5510\u4e09\u85cf",
    "\u4e1c\u6c49\u4e09\u85cf",
    "\u53d6\u7ecf\u4eba",
    "\u91d1\u8749\u5b50",
    "\u82b1\u679c\u5c71",
    "\u5982\u610f\u91d1\u7b8d\u68d2",
    "\u91d1\u7b8d\u68d2",
    "\u5927\u54c1\u5929\u4ed9\u8bc0",
    "\u7d2b\u9704\u7384\u771f\u609f\u5143\u529f",
]

TERM_EXTRACT_STOPWORDS = {
    "\u7ae0\u7eb2",
    "\u6b63\u6587",
    "\u6279\u6b21\u6458\u8981",
    "\u5377\u7eb2",
    "\u540e\u7eed\u751f\u6210",
    "\u53c2\u8003\u5c0f\u8bf4",
    "\u6620\u5c04\u8bf4\u660e",
    "\u8f93\u51fa",
    "\u5904\u7406\u65b9\u5f0f",
    "chapter outline",
    "draft",
    "batch summary",
    "volume outline",
    "later generation",
    "reference novel",
    "mapping notes",
    "output",
    "handling",
}


def _read_file(path):
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def _candidate_files(ws, volume=None):
    adaptation_dir = os.path.join(ws.file_system, "adaptation")
    rewrite_dir = os.path.join(ws.file_system, "rewrite_maps")

    names = [
        "rewrite_map.md",
        "rewrite_map.txt",
        "rewrite_map.jsonl",
    ]
    if volume is not None:
        names.extend([
            f"rewrite_map_vol_{volume:02d}.md",
            f"rewrite_map_vol_{volume:02d}.txt",
            f"rewrite_map_vol_{volume:02d}.jsonl",
            f"vol_{volume:02d}_map.md",
            f"vol_{volume:02d}_map.txt",
            f"vol_{volume:02d}_map.jsonl",
        ])

    for base in [adaptation_dir, rewrite_dir]:
        for name in names:
            yield os.path.join(base, name)


def load_rewrite_map(ws, volume=None):
    """Load global and volume-specific rewrite map text, if present."""
    parts = []
    seen = set()
    for path in _candidate_files(ws, volume):
        if path in seen:
            continue
        seen.add(path)
        content = _read_file(path)
        if content:
            parts.append(f"[Source: {path}]\n{content}")
    return "\n\n---\n\n".join(parts) if parts else "(rewrite map not found; follow the new novel outline and worldview.)"


def _load_forbidden_term_files(ws, volume=None):
    adaptation_dir = os.path.join(ws.file_system, "adaptation")
    rewrite_dir = os.path.join(ws.file_system, "rewrite_maps")
    names = [
        "forbidden_terms.txt",
        "forbidden_terms.md",
        "forbidden_terms.jsonl",
    ]
    if volume is not None:
        names.extend([
            f"forbidden_terms_vol_{volume:02d}.txt",
            f"forbidden_terms_vol_{volume:02d}.md",
            f"forbidden_terms_vol_{volume:02d}.jsonl",
        ])

    chunks = []
    for base in [adaptation_dir, rewrite_dir]:
        for name in names:
            content = _read_file(os.path.join(base, name))
            if content:
                chunks.append(content)
    return chunks


def extract_forbidden_terms_from_text(text):
    """Extract quoted or delimited forbidden terms from rewrite-map style text."""
    if not text:
        return []

    terms = []
    for line in text.splitlines():
        stripped = line.strip()
        if not (
            stripped.startswith("\u7981\u6b62\u6b8b\u7559\u53c2\u8003\u5143\u7d20")
            or stripped.startswith("\u786c\u7981\u7528\u5143\u7d20")
            or stripped.startswith("\u4e0d\u5f97\u76f4\u63a5\u51fa\u73b0")
            or stripped.startswith("\u7981\u7528\u8bcd")
            or stripped.lower().startswith("forbidden")
        ):
            continue

        quoted = re.findall(r"[“\"']([^”\"']+)[”\"']", line)
        bracketed = re.findall(r"《([^》]+)》", line)
        terms.extend(quoted)
        terms.extend(bracketed)

        if "：" in line:
            tail = line.split("：", 1)[1]
        elif ":" in line:
            tail = line.split(":", 1)[1]
        else:
            tail = ""
        for item in re.split(r"[、,，/；;]", tail):
            item = item.strip(" \t。.!！?？[]【】（）()“”\"'")
            if (
                2 <= len(item) <= 20
                and item not in TERM_EXTRACT_STOPWORDS
                and not any(x in item for x in [
                    "\u5fc5\u987b", "\u6539\u5199", "\u66ff\u6362", "\u51fa\u73b0",
                    "\u53c2\u8003", "\u8f93\u51fa",
                    "must", "rewrite", "replace", "appear", "reference", "output",
                ])
            ):
                terms.append(item)

    return _dedupe_terms(terms)


def _dedupe_terms(terms):
    seen = set()
    result = []
    for term in terms:
        term = str(term).strip()
        if not term or term in seen:
            continue
        seen.add(term)
        result.append(term)
    return result


def load_forbidden_terms(ws, volume=None):
    """Load configured forbidden terms plus high-risk default reference leftovers."""
    chunks = _load_forbidden_term_files(ws, volume)
    rewrite_map = load_rewrite_map(ws, volume)
    terms = []
    for chunk in chunks:
        for line in chunk.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            terms.extend(re.split(r"[、,，/；;]", line))
    terms.extend(extract_forbidden_terms_from_text(rewrite_map))
    terms.extend(DEFAULT_FORBIDDEN_TERMS)
    return _dedupe_terms(terms)


def format_forbidden_terms(terms):
    if not terms:
        return "(no explicit forbidden terms. Still avoid inheriting the reference novel's proper nouns and event causality.)"
    return "\n".join(f"- {term}" for term in terms)


def scan_forbidden_terms(text, terms, exempt_line_patterns=None):
    """Return forbidden terms found in text. Optional line patterns exempt reference-comparison lines."""
    if not text or not terms:
        return []
    exempt_line_patterns = tuple(exempt_line_patterns or [])
    found = []
    for line in text.splitlines():
        if exempt_line_patterns and any(pat in line for pat in exempt_line_patterns):
            continue
        for term in terms:
            if term and term in line:
                found.append(term)
    return _dedupe_terms(found)


def append_adaptation_report(ws, title, message):
    report_dir = os.path.join(ws.file_system, "adaptation")
    os.makedirs(report_dir, exist_ok=True)
    report_path = os.path.join(report_dir, "adaptation_report.md")
    with open(report_path, "a", encoding="utf-8") as f:
        f.write(f"\n# {title}\n\n{message.strip()}\n")
    return report_path
