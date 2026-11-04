#!/usr/bin/env python3
"""农业育种 PDF 的 Markdown 语义切分、嵌入与 Milvus 写入。"""

from __future__ import annotations

import re
import time
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path

import requests
from pymilvus import (
    Collection,
    CollectionSchema,
    DataType,
    FieldSchema,
    MilvusClient,
    connections,
    utility,
)
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

# ============================================================
# 常量
# ============================================================
CHUNK_TARGET_CHARS = 1200
CHUNK_MAX_CHARS = 1500
CHUNK_MIN_CHARS = 300
SEMANTIC_WINDOW_CHARS = 200
CHUNK_OVERLAP_RATIO = 0.15

VECTOR_DIM = 2560
HNSW_M = 32
HNSW_EF_CONSTRUCTION = 800
API_MAX_CONCURRENCY = 10
API_MAX_RETRIES = 3

EXPECTED_MILVUS_HOST = "127.0.0.1"
EXPECTED_MILVUS_PORT = 19534
EXPECTED_MILVUS_DATABASE = "agri_pdf_memories"
EXPECTED_MILVUS_COLLECTION = "agri_breeding_pdf_chunk"
ALLOWED_EMBEDDING_MODELS = {"qwen3-embed"}

PICTURE_RE = re.compile(
    r"(\*\*==>\s*picture\s*.*?\s*intentionally omitted\s*<==\*\*)|"
    r"(\*\*-----\s*Start of picture text\s*-----\*\*[\s\S]*?"
    r"\*\*-----\s*End of picture text\s*-----\*\*)",
    re.IGNORECASE,
)
DEFAULT_EXCLUDED_HEADERS = {
    "references", "bibliography", "acknowledgements", "acknowledgments",
    "supplementary materials", "supplementary data", "funding",
    "figure legends", "author contributions",
}
LATEX_INLINE_RE = re.compile(r'\$.+?\$')
LATEX_BLOCK_RE = re.compile(r'\$\$.+?\$\$', re.DOTALL)
LATEX_PAREN_RE = re.compile(r'\\\(.+?\\\)')
LATEX_BRACKET_RE = re.compile(r'\\\[.+?\\\]', re.DOTALL)

console = Console()

# ============================================================
# MMD 解析器: Lexer + Parser → ParsedDocument
# ============================================================

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")


@dataclass
class Section:
    title: str
    level: int
    content_lines: list[str] = field(default_factory=list)
    children: list["Section"] = field(default_factory=list)
    line_start: int = 0
    line_end: int = 0

    @property
    def content_text(self) -> str:
        return "\n".join(self.content_lines)


@dataclass
class Paragraph:
    text: str
    line_start: int
    line_end: int
    section_path: str = ""
    char_start: int = 0
    char_end: int = 0


@dataclass
class ParsedDocument:
    raw_text: str
    sections: list[Section] = field(default_factory=list)
    paragraphs: list[Paragraph] = field(default_factory=list)
    reference_line: int = -1


@dataclass
class Chunk:
    id: str = ""
    pdf_index: str = ""
    chunk_index: int = 0
    text: str = ""
    section_path: str = ""
    char_start: int = 0
    char_end: int = 0


@dataclass
class SemanticSpan:
    char_start: int
    char_end: int
    section_path: str


# ---- Lexer ----

def _tokenize(lines: list[str]) -> list[tuple[str, str, int]]:
    """返回 [(type, content, line_index), ...]"""
    tokens = []
    for i, line in enumerate(lines):
        m = HEADING_RE.match(line)
        if m:
            tokens.append(("HEADING", line, i))
        elif line.strip() == "":
            tokens.append(("BLANK", "", i))
        else:
            tokens.append(("TEXT", line, i))
    tokens.append(("EOF", "", len(lines)))
    return tokens


# ---- Parser ----

def _parse_mmd(text: str, excluded_headers: set[str] | None = None) -> ParsedDocument:
    lines = text.split("\n")
    tokens = _tokenize(lines)
    doc = ParsedDocument(raw_text=text)
    char_index = _build_char_offset_index(text)

    # Phase 1: Flat sections
    flat_sections: list[Section] = []
    heading_indices = [(ti, tk[1], tk[2]) for ti, tk in enumerate(tokens)
                       if tk[0] == "HEADING"]
    for hi, (ti, heading_line, line_idx) in enumerate(heading_indices):
        m = HEADING_RE.match(heading_line)
        level = len(m.group(1))
        title = m.group(2).strip()
        sec = Section(title=title, level=level, line_start=line_idx)
        # end line = until next same-or-higher level heading
        end_line = len(lines)
        for hj in range(hi + 1, len(heading_indices)):
            _, nxt_line, nxt_idx = heading_indices[hj]
            nm = HEADING_RE.match(nxt_line)
            nxt_level = len(nm.group(1))
            if nxt_level <= level:
                end_line = nxt_idx
                break
        sec.line_end = end_line
        sec.content_lines = lines[line_idx + 1 : end_line]
        flat_sections.append(sec)

    # Phase 2: Treeify (stack-based)
    root_sections: list[Section] = []
    stack: list[Section] = []
    for sec in flat_sections:
        while stack and stack[-1].level >= sec.level:
            stack.pop()
        if stack:
            stack[-1].children.append(sec)
        else:
            root_sections.append(sec)
        stack.append(sec)
    doc.sections = root_sections

    # Phase 3 & 4: Paragraph split (by blank lines) with section attribution
    for sec in flat_sections:
        if re.match(r'^References?|Bibliography$', sec.title, re.IGNORECASE):
            doc.reference_line = sec.line_start
            break

    def _add_paragraphs(start_char: int, end_char: int, section_path: str):
        raw = text[start_char:end_char]
        for match in re.finditer(r'\S[\s\S]*?(?=\n\s*\n|\Z)', raw):
            rel_start = match.start()
            rel_end = match.end()
            segment = raw[rel_start:rel_end]
            left_trim = len(segment) - len(segment.lstrip())
            right_trim = len(segment.rstrip())
            para_start = start_char + rel_start + left_trim
            para_end = start_char + rel_start + right_trim
            para_text = text[para_start:para_end]
            if not para_text:
                continue
            doc.paragraphs.append(Paragraph(
                text=para_text,
                line_start=_char_to_line_start(char_index, para_start),
                line_end=_char_to_line_end(char_index, para_end),
                section_path=section_path,
                char_start=para_start,
                char_end=para_end,
            ))

    # Build section path. Only direct body text before the first child heading
    # belongs to the parent section; child sections are parsed separately.
    def _assign_path(sec: Section, parent_path: str):
        sp = f"{parent_path}/{sec.title}" if parent_path else sec.title
        direct_end_line = min((child.line_start for child in sec.children), default=sec.line_end)
        _add_paragraphs(
            _line_to_char(char_index, sec.line_start + 1, len(text)),
            _line_to_char(char_index, direct_end_line, len(text)),
            sp,
        )

        for child in sec.children:
            _assign_path(child, sp)

    if root_sections:
        first_heading_line = min(sec.line_start for sec in flat_sections)
        if first_heading_line > 0:
            _add_paragraphs(0, _line_to_char(char_index, first_heading_line, len(text)), "body")
        for sec in root_sections:
            _assign_path(sec, "")
    else:
        _add_paragraphs(0, len(text), "body")

    # Phase 5: Exclude configured non-retrieval sections and their descendants.
    if doc.reference_line >= 0:
        doc.paragraphs = [p for p in doc.paragraphs if p.line_start < doc.reference_line]
    excluded_headers = excluded_headers or DEFAULT_EXCLUDED_HEADERS
    normalized_headers = {header.strip().casefold() for header in excluded_headers}
    doc.paragraphs = [
        paragraph
        for paragraph in doc.paragraphs
        if not any(
            part.strip().casefold() in normalized_headers
            for part in paragraph.section_path.split("/")
        )
    ]

    return doc


# ============================================================
# 三级切分器 + v2.0 后处理
# ============================================================

def _is_latex_region(text: str, pos: int) -> bool:
    """检查 pos 是否在 LaTeX 公式内部。"""
    for pattern in [LATEX_BLOCK_RE, LATEX_INLINE_RE, LATEX_PAREN_RE, LATEX_BRACKET_RE]:
        for m in pattern.finditer(text):
            if m.start() <= pos < m.end():
                return True
    return False


def _is_structured_block(text: str) -> bool:
    """Lists and tables must only be split at line boundaries."""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    table_lines = sum("|" in line for line in lines)
    list_lines = sum(bool(re.match(r"\s*(?:[-*+] |\d+[.)] )", line)) for line in lines)
    return table_lines >= 2 or list_lines >= 2


def _pick_closest(candidates: list[int], target: int) -> int | None:
    return min(candidates, key=lambda pos: (abs(pos - target), -pos)) if candidates else None


def _find_semantic_split(
    text: str,
    start: int,
    target: int,
    max_end: int,
    window: int,
    prefer_line_break: bool,
) -> int:
    """Find a natural boundary near target without exceeding max_end."""
    search_start = max(start + 1, target - window)
    search_end = min(max_end, target + window)
    if search_start >= search_end:
        return target

    patterns = []
    if prefer_line_break:
        patterns.append(re.compile(r"\n"))
    patterns.extend([
        re.compile(r"[。！？!?](?:[\"”’')\]]*)"),
        re.compile(r"[；;：:，,]"),
        re.compile(r"\s+"),
    ])

    for pattern in patterns:
        candidates = []
        for match in pattern.finditer(text, search_start, search_end):
            position = match.end()
            if position <= start or position > max_end:
                continue
            if not _is_latex_region(text, max(start, position - 1)):
                candidates.append(position)
        candidate = _pick_closest(candidates, target)
        if candidate is not None:
            return candidate
    return target


def _split_long_paragraph(
    paragraph: Paragraph,
    target_chars: int,
    max_chars: int,
    semantic_window: int,
) -> list[SemanticSpan]:
    if len(paragraph.text) <= max_chars:
        return [SemanticSpan(paragraph.char_start, paragraph.char_end, paragraph.section_path)]

    spans: list[SemanticSpan] = []
    position = 0
    prefer_line_break = _is_structured_block(paragraph.text)
    while position < len(paragraph.text):
        max_end = min(len(paragraph.text), position + max_chars)
        if max_end == len(paragraph.text):
            spans.append(SemanticSpan(
                paragraph.char_start + position,
                paragraph.char_start + max_end,
                paragraph.section_path,
            ))
            break

        target = min(position + target_chars, max_end)
        split_at = _find_semantic_split(
            paragraph.text, position, target, max_end, semantic_window, prefer_line_break,
        )
        if split_at <= position:
            split_at = max_end
        spans.append(SemanticSpan(
            paragraph.char_start + position,
            paragraph.char_start + split_at,
            paragraph.section_path,
        ))
        position = split_at
    return spans


def _group_section_spans(
    spans: list[SemanticSpan],
    source_text: str,
    target_chars: int,
    min_chars: int,
    max_chars: int,
) -> list[SemanticSpan]:
    """Merge neighboring semantic spans up to the target length within one section."""
    if not spans:
        return []

    groups: list[SemanticSpan] = []
    start = spans[0].char_start
    end = spans[0].char_end
    section_path = spans[0].section_path

    for span in spans[1:]:
        candidate_len = span.char_end - start
        current_len = end - start
        if candidate_len <= target_chars or (current_len < min_chars and candidate_len <= max_chars):
            end = span.char_end
            continue
        groups.append(SemanticSpan(start, end, section_path))
        start = span.char_start
        end = span.char_end

    groups.append(SemanticSpan(start, end, section_path))

    if len(groups) >= 2:
        previous = groups[-2]
        last = groups[-1]
        if last.char_end - last.char_start < min_chars and last.char_end - previous.char_start <= max_chars:
            groups[-2] = SemanticSpan(previous.char_start, last.char_end, section_path)
            groups.pop()
    return groups


def _build_semantic_spans(
    text: str,
    target_chars: int,
    min_chars: int,
    max_chars: int,
    semantic_window: int,
    excluded_headers: set[str] | None,
) -> list[SemanticSpan]:
    doc = _parse_mmd(text, excluded_headers)
    paragraph_spans: list[SemanticSpan] = []
    for paragraph in doc.paragraphs:
        paragraph_spans.extend(_split_long_paragraph(
            paragraph, target_chars, max_chars, semantic_window,
        ))

    grouped: list[SemanticSpan] = []
    section_spans: list[SemanticSpan] = []
    current_section = None
    for span in sorted(paragraph_spans, key=lambda item: item.char_start):
        if current_section is not None and span.section_path != current_section:
            grouped.extend(_group_section_spans(
                section_spans, text, target_chars, min_chars, max_chars,
            ))
            section_spans = []
        section_spans.append(span)
        current_section = span.section_path
    grouped.extend(_group_section_spans(
        section_spans, text, target_chars, min_chars, max_chars,
    ))
    return grouped


def _build_char_offset_index(text: str) -> list[int]:
    idx = [0]
    for match in re.finditer("\n", text):
        idx.append(match.end())
    return idx


def _line_to_char(char_index: list[int], line_num: int, text_len: int) -> int:
    if line_num < 0:
        return 0
    if line_num < len(char_index):
        return char_index[line_num]
    return text_len


def _char_to_line_start(char_index: list[int], char_pos: int) -> int:
    if not char_index:
        return 0
    return max(0, bisect_right(char_index, char_pos) - 1)


def _char_to_line_end(char_index: list[int], char_pos: int) -> int:
    if char_pos <= 0 or not char_index:
        return 0
    return bisect_right(char_index, char_pos - 1)


def _overlap_chars(chunk: Chunk, overlap_ratio: float) -> int:
    if overlap_ratio == 0:
        return 0
    return min(len(chunk.text), max(1, round(len(chunk.text) * overlap_ratio)))


def _chunk_id(pdf_index: str, chunk_index: int) -> str:
    """Build a chunk ID from the PDF filename without its .pdf suffix."""
    source_id = pdf_index[:-4] if pdf_index.casefold().endswith(".pdf") else pdf_index
    return f"{source_id}_{chunk_index}"


def _validate_chunks_against_source(
    chunks: list[Chunk],
    source_text: str,
    pdf_index: str,
    overlap_ratio: float,
):
    previous: Chunk | None = None
    for index, chunk in enumerate(chunks):
        if chunk.chunk_index != index or chunk.id != _chunk_id(pdf_index, index):
            raise ValueError(f"{pdf_index} chunk ID or index is not continuous at {index}")
        if chunk.pdf_index != pdf_index:
            raise ValueError(f"{pdf_index} chunk {index} has mismatched pdf_index")
        if not (0 <= chunk.char_start < chunk.char_end <= len(source_text)):
            raise ValueError(
                f"{pdf_index} chunk {index} has invalid range "
                f"{chunk.char_start}-{chunk.char_end} for source length {len(source_text)}"
            )
        if chunk.text != source_text[chunk.char_start:chunk.char_end]:
            raise ValueError(
                f"{pdf_index} chunk {index} does not match "
                f"source[{chunk.char_start}:{chunk.char_end}]"
            )
        if previous is not None:
            raw_overlap = previous.char_end - chunk.char_start
            actual_overlap = max(0, raw_overlap)
            if previous.section_path == chunk.section_path:
                expected_overlap = _overlap_chars(previous, overlap_ratio)
                if actual_overlap != expected_overlap:
                    raise ValueError(
                        f"{pdf_index} chunks {index - 1}/{index} overlap "
                        f"{actual_overlap}, expected {expected_overlap}"
                    )
            elif raw_overlap > 0:
                raise ValueError(f"{pdf_index} chunks cross section boundaries with overlap")
        previous = chunk


# ---- 主切分入口 ----

def split_document(
    text: str,
    pdf_index: str,
    target_chars: int = CHUNK_TARGET_CHARS,
    min_chars: int = CHUNK_MIN_CHARS,
    max_chars: int = CHUNK_MAX_CHARS,
    semantic_window: int = SEMANTIC_WINDOW_CHARS,
    overlap_ratio: float = CHUNK_OVERLAP_RATIO,
    excluded_headers: set[str] | None = None,
) -> list[Chunk]:
    """Create source-aligned semantic chunks with intra-section percentage overlap."""
    if not 0 <= overlap_ratio < 1:
        raise ValueError("overlap_ratio must be in [0, 1)")
    if not 0 < min_chars <= target_chars <= max_chars:
        raise ValueError("chunk sizes must satisfy 0 < min_chars <= target_chars <= max_chars")

    base_spans = _build_semantic_spans(
        text, target_chars, min_chars, max_chars, semantic_window, excluded_headers,
    )
    chunks: list[Chunk] = []
    previous: Chunk | None = None
    for span in base_spans:
        char_start = span.char_start
        if previous is not None and previous.section_path == span.section_path:
            char_start = previous.char_end - _overlap_chars(previous, overlap_ratio)
        chunk = Chunk(
            pdf_index=pdf_index,
            text=text[char_start:span.char_end],
            section_path=span.section_path or "body",
            char_start=char_start,
            char_end=span.char_end,
        )
        chunks.append(chunk)
        previous = chunk

    for index, chunk in enumerate(chunks):
        chunk.chunk_index = index
        chunk.id = _chunk_id(pdf_index, index)

    _validate_chunks_against_source(chunks, text, pdf_index, overlap_ratio)
    return chunks


def clean_text(text: str) -> str:
    text = PICTURE_RE.sub("", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


# ============================================================
# 嵌入 API
# ============================================================

def embed_texts(texts: list, api_url: str, model_name: str, api_key: str,
                batch_size: int = 32, expected_dim: int = VECTOR_DIM) -> list:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    all_embeddings = []

    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        payload = {"model": model_name, "input": batch}
        for attempt in range(API_MAX_RETRIES):
            try:
                resp = requests.post(api_url, headers=headers, json=payload, timeout=120)
                resp.raise_for_status()
                data = resp.json()
                batch_embs = []
                for item in data["data"]:
                    emb = item["embedding"]
                    if len(emb) != expected_dim:
                        raise ValueError(f"Dimension mismatch: {len(emb)} != {expected_dim}")
                    batch_embs.append(emb)
                all_embeddings.extend(batch_embs)
                break
            except Exception as e:
                if attempt == API_MAX_RETRIES - 1:
                    console.print(f"[red]API error (batch {i}): {e}[/red]")
                    raise
                wait = 2 ** attempt
                console.print(f"[yellow]Retry {attempt+1}/{API_MAX_RETRIES} for batch {i}, wait {wait}s[/yellow]")
                time.sleep(wait)

    return all_embeddings


# ============================================================
# Milvus 操作
# ============================================================

def ensure_database(client: MilvusClient, db_name: str):
    dbs = client.list_databases()
    if db_name not in dbs:
        client.create_database(db_name)
        console.print(f"[green]Created database:[/green] {db_name}")
    if hasattr(client, "use_database"):
        client.use_database(db_name)
    else:
        client.using_database(db_name)


def validate_runtime_config(milvus_cfg: dict, embed_cfg: dict):
    host = str(milvus_cfg.get("host", ""))
    port = int(milvus_cfg.get("port", 0))
    database = milvus_cfg.get("database", "")
    collection = milvus_cfg.get("collection", "")
    model_name = embed_cfg.get("model_name", "")
    dimension = int(embed_cfg.get("dimension", 0))

    if (
        host != EXPECTED_MILVUS_HOST
        or port != EXPECTED_MILVUS_PORT
        or database != EXPECTED_MILVUS_DATABASE
        or collection != EXPECTED_MILVUS_COLLECTION
    ):
        raise ValueError(
            "Milvus target is not allowed. Expected "
            f"{EXPECTED_MILVUS_HOST}:{EXPECTED_MILVUS_PORT}/"
            f"{EXPECTED_MILVUS_DATABASE}.{EXPECTED_MILVUS_COLLECTION}, got "
            f"{host}:{port}/{database}.{collection}"
        )
    if collection == "scientific_fulltext_chunks":
        raise ValueError("Refusing to write to scientific_fulltext_chunks")
    if model_name not in ALLOWED_EMBEDDING_MODELS:
        raise ValueError(
            f"Embedding model {model_name!r} is not allowed; "
            f"expected one of {sorted(ALLOWED_EMBEDDING_MODELS)}"
        )
    if model_name == "qwen3-embed" and dimension != VECTOR_DIM:
        raise ValueError(f"qwen3-embed dimension must be {VECTOR_DIM}, got {dimension}")


def validate_collection_dimension(collection: Collection, expected_dim: int):
    for field in collection.schema.fields:
        if field.name == "text_embed":
            dim = int(field.params.get("dim", 0))
            if dim != int(expected_dim):
                raise ValueError(
                    f"Milvus text_embed dimension mismatch: {dim} != {expected_dim}"
                )
            return
    raise ValueError("Milvus collection missing vector field text_embed")


def validate_collection_schema(collection: Collection, expected_dim: int):
    """Reject legacy collections before any insert can mix incompatible rows."""
    validate_collection_dimension(collection, expected_dim)
    expected_fields = {
        "id", "pdf_index", "chunk_index", "text_embed",
        "text", "char_start", "char_end", "section_path",
    }
    actual_fields = {field.name for field in collection.schema.fields}
    if actual_fields != expected_fields:
        missing = sorted(expected_fields - actual_fields)
        extra = sorted(actual_fields - expected_fields)
        raise ValueError(
            "Milvus collection schema is incompatible with the semantic chunk schema. "
            f"Missing fields: {missing}; extra fields: {extra}. "
            "Use the configured new collection or run with --rebuild only after backup."
        )
    primary_fields = [field.name for field in collection.schema.fields if field.is_primary]
    if primary_fields != ["id"]:
        raise ValueError(f"Milvus primary key must be id, got {primary_fields}")


def create_collection(client: MilvusClient, collection_name: str, dim: int,
                      rebuild: bool = False):
    if utility.has_collection(collection_name):
        if rebuild:
            utility.drop_collection(collection_name)
            console.print(f"[yellow]Dropped collection:[/yellow] {collection_name}")
        else:
            col = Collection(collection_name)
            validate_collection_schema(col, dim)
            console.print(f"[dim]Collection already exists:[/dim] {collection_name}")
            return

    fields = [
        FieldSchema(name="id", dtype=DataType.VARCHAR, max_length=512, is_primary=True),
        FieldSchema(name="pdf_index", dtype=DataType.VARCHAR, max_length=256),
        FieldSchema(name="chunk_index", dtype=DataType.INT64),
        FieldSchema(name="text_embed", dtype=DataType.FLOAT_VECTOR, dim=dim),
        FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=65535),
        FieldSchema(name="char_start", dtype=DataType.INT64),
        FieldSchema(name="char_end", dtype=DataType.INT64),
        FieldSchema(name="section_path", dtype=DataType.VARCHAR, max_length=1024),
    ]

    schema = CollectionSchema(fields, description="Agricultural breeding PDF semantic chunks")
    Collection(collection_name, schema=schema)
    col = Collection(collection_name)

    col.create_index("text_embed", {
        "index_type": "HNSW",
        "metric_type": "COSINE",
        "params": {"M": HNSW_M, "efConstruction": HNSW_EF_CONSTRUCTION},
    })
    console.print(f"[green]Created collection:[/green] {collection_name} ({len(fields)} fields)")


def get_existing_sources(collection) -> set:
    existing = set()
    try:
        collection.load()
        offset = 0
        batch_size = 10000
        while True:
            results = collection.query(
                expr="chunk_index >= 0",
                output_fields=["pdf_index"],
                limit=batch_size,
                offset=offset,
            )
            if not results:
                break
            for r in results:
                existing.add(r["pdf_index"])
            if len(results) < batch_size:
                break
            offset += batch_size
    except Exception:
        pass
    return existing


def chunk_to_row(chunk: Chunk, embedding: list[float]) -> dict:
    """Build exactly the eight fields defined by the semantic chunk schema."""
    if len(chunk.id) > 512 or len(chunk.pdf_index) > 256:
        raise ValueError("Chunk ID or pdf_index exceeds the configured Milvus VARCHAR limit")
    if len(chunk.text) > 65535 or len(chunk.section_path) > 1024:
        raise ValueError("Chunk text or section_path exceeds the configured Milvus VARCHAR limit")
    return {
        "id": chunk.id,
        "pdf_index": chunk.pdf_index,
        "chunk_index": chunk.chunk_index,
        "text_embed": embedding,
        "text": chunk.text,
        "char_start": chunk.char_start,
        "char_end": chunk.char_end,
        "section_path": chunk.section_path,
    }


def _milvus_string_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _select_md_files(paths_cfg: dict, filenames: set | None) -> dict[str, Path]:
    """Select one MD file per PDF, preferring the configured PDF->MD relative path."""
    md_dir = Path(paths_cfg["md_output_dir"]).resolve()
    source_dir = Path(paths_cfg.get("pdf_source_dir", "")).resolve()
    selected: dict[str, Path] = {}

    if source_dir.exists():
        for pdf_path in sorted(source_dir.rglob("*.pdf")):
            pdf_filename = pdf_path.name
            if filenames is not None and pdf_filename not in filenames:
                continue
            md_path = md_dir / pdf_path.relative_to(source_dir).with_suffix(".md")
            selected[pdf_filename] = md_path

    if not md_dir.exists():
        return selected

    # Fallback for ad-hoc MD-only runs or files not present under pdf_source_dir.
    md_files = sorted(
        md_dir.rglob("*.md"),
        key=lambda p: (len(p.relative_to(md_dir).parts), str(p)),
    )
    for md_path in md_files:
        pdf_filename = md_path.stem + ".pdf"
        if filenames is not None and pdf_filename not in filenames:
            continue
        selected.setdefault(pdf_filename, md_path)

    return selected


# ============================================================
# 批量向量化入口（供 pipeline 调用）
# ============================================================

def vectorize_all(config: dict, api_key: str, rebuild: bool = False,
                  filenames: set | None = None) -> list[dict]:
    """
    扫描 MD 文件，按语义切分和 15% 相邻重叠向量化入库。
    filenames: 只处理这些 PDF 文件名对应的 MD 文件。
    返回 [{pdf_filename, success, chunk_count, error}, ...]
    """
    milvus_cfg = config["milvus"]
    embed_cfg = config["embedding"]
    chunk_cfg = config.get("chunking", {})
    paths_cfg = config["paths"]

    validate_runtime_config(milvus_cfg, embed_cfg)

    target_chars = int(chunk_cfg.get("target_chars", CHUNK_TARGET_CHARS))
    min_chars = int(chunk_cfg.get("min_chars", CHUNK_MIN_CHARS))
    max_chars = int(chunk_cfg.get("max_chars", CHUNK_MAX_CHARS))
    semantic_window = int(chunk_cfg.get("semantic_window_chars", SEMANTIC_WINDOW_CHARS))
    overlap_ratio = float(chunk_cfg.get("overlap_ratio", CHUNK_OVERLAP_RATIO))
    excluded_headers = set(config.get("exclude_headers", DEFAULT_EXCLUDED_HEADERS))

    results = []
    handled: set[str] = set()

    def add_result(result: dict):
        results.append(result)
        handled.add(result["pdf_filename"])

    # --- 扫描并选择 MD 文件 ---
    md_dir = Path(paths_cfg["md_output_dir"]).resolve()
    if not md_dir.exists():
        console.print(f"[red]MD directory not found:[/red] {md_dir}")
        for pdf_filename in sorted(filenames or []):
            add_result({
                "pdf_filename": pdf_filename, "success": False, "chunk_count": 0,
                "error": f"MD directory not found: {md_dir}",
            })
        return results

    selected_files = _select_md_files(paths_cfg, filenames)
    discovered_count = len(list(md_dir.rglob("*.md")))
    console.print(
        f"Found [bold]{discovered_count}[/bold] MD files in {md_dir}; "
        f"selected [bold]{len(selected_files)}[/bold] for this run"
    )

    if filenames is not None:
        for pdf_filename in sorted(set(filenames) - set(selected_files)):
            add_result({
                "pdf_filename": pdf_filename, "success": False, "chunk_count": 0,
                "error": "MD file not found for requested PDF",
            })

    if not selected_files:
        console.print("[yellow]No MD files selected for vectorization.[/yellow]")
        return results

    # --- 连接 Milvus ---
    client = None
    connected = False
    try:
        uri = f"http://{milvus_cfg['host']}:{milvus_cfg['port']}"
        client = MilvusClient(uri=uri)
        ensure_database(client, milvus_cfg["database"])
        connections.connect(
            alias="default",
            host=milvus_cfg["host"],
            port=milvus_cfg["port"],
            db_name=milvus_cfg["database"],
        )
        connected = True
        console.print(
            f"[bold]Connected to Milvus:[/bold] "
            f"{milvus_cfg['host']}:{milvus_cfg['port']}/{milvus_cfg['database']}"
        )
        create_collection(client, milvus_cfg["collection"], embed_cfg["dimension"], rebuild)
        collection = Collection(milvus_cfg["collection"])
        validate_collection_schema(collection, embed_cfg["dimension"])
        existing_pdf_indices = get_existing_sources(collection)
        console.print(f"  Existing documents in collection: {len(existing_pdf_indices)}")
    except Exception as e:
        console.print(f"[red]Milvus setup error: {e}[/red]")
        for pdf_filename in sorted(set(selected_files) - handled):
            add_result({
                "pdf_filename": pdf_filename, "success": False, "chunk_count": 0,
                "error": f"Milvus setup: {e}",
            })
        if connected:
            connections.disconnect("default")
        if client is not None and hasattr(client, "close"):
            client.close()
        return results

    # 过滤
    new_files: list[tuple[str, Path]] = []
    for pdf_filename, md_path in sorted(selected_files.items()):
        if pdf_filename in handled:
            continue
        if pdf_filename in existing_pdf_indices and not rebuild and filenames is None:
            add_result({
                "pdf_filename": pdf_filename, "success": True, "chunk_count": 0,
                "error": None, "already_indexed": True,
            })
            continue
        if not md_path.exists():
            add_result({
                "pdf_filename": pdf_filename, "success": False, "chunk_count": 0,
                "error": f"MD file not found: {md_path}",
            })
            continue
        new_files.append((pdf_filename, md_path))

    if not new_files:
        console.print("[green]All MD files already indexed. Nothing to do.[/green]")
        connections.disconnect("default")
        if client is not None and hasattr(client, "close"):
            client.close()
        return results

    console.print(f"  New files to process: [bold]{len(new_files)}[/bold]")

    # --- 分块（每个文件独立，保持顺序）---
    all_chunks: list[Chunk] = []
    file_chunk_map: dict[str, dict] = {}

    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(), TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(), console=console,
    ) as progress:
        task = progress.add_task("Semantic chunking", total=len(new_files))

        for pdf_filename, md_path in new_files:
            try:
                text = md_path.read_text(encoding="utf-8")
            except Exception as e:
                console.print(f"[red]Read error {md_path}:[/red] {e}")
                add_result({
                    "pdf_filename": pdf_filename, "success": False, "chunk_count": 0,
                    "error": f"Read error: {e}",
                })
                progress.advance(task)
                continue

            text = clean_text(text)
            if not text:
                add_result({
                    "pdf_filename": pdf_filename, "success": False, "chunk_count": 0,
                    "error": "Empty MD after cleaning",
                })
                progress.advance(task)
                continue

            try:
                chunks = split_document(
                    text, pdf_filename, target_chars, min_chars, max_chars,
                    semantic_window, overlap_ratio, excluded_headers,
                )
            except Exception as e:
                console.print(f"[red]Split error {md_path}:[/red] {e}")
                add_result({
                    "pdf_filename": pdf_filename, "success": False, "chunk_count": 0,
                    "error": f"Split error: {e}",
                })
                progress.advance(task)
                continue

            if not chunks:
                add_result({
                    "pdf_filename": pdf_filename, "success": False, "chunk_count": 0,
                    "error": "No chunks generated",
                })
                progress.advance(task)
                continue

            all_chunks.extend(chunks)
            file_chunk_map[pdf_filename] = {
                "chunk_count": len(chunks),
            }
            progress.advance(task)

    console.print(f"Total chunks after semantic splitting: [bold]{len(all_chunks)}[/bold]")

    if not all_chunks:
        connections.disconnect("default")
        if client is not None and hasattr(client, "close"):
            client.close()
        return results

    # --- 调用嵌入 API ---
    texts = [c.text for c in all_chunks]
    batch_size = embed_cfg.get("batch_size", 32)

    console.print(f"Calling embedding API ({len(texts)} texts, batch={batch_size})...")
    try:
        embeddings = embed_texts(texts, embed_cfg["api_url"], embed_cfg["model_name"],
                                 api_key, batch_size, embed_cfg["dimension"])
        if len(embeddings) != len(all_chunks):
            raise ValueError(f"Embedding count mismatch: {len(embeddings)} != {len(all_chunks)}")
    except Exception as e:
        console.print(f"[red]Embedding error: {e}[/red]")
        for pdf_fn, info in file_chunk_map.items():
            add_result({
                "pdf_filename": pdf_fn, "success": False, "chunk_count": 0,
                "error": f"Embedding: {e}",
            })
        connections.disconnect("default")
        if client is not None and hasattr(client, "close"):
            client.close()
        return results

    # --- 组装入库行 ---
    rows = [chunk_to_row(chunk, embedding) for chunk, embedding in zip(all_chunks, embeddings)]

    # --- 按文档先删后插 ---
    pdf_indices = set(row["pdf_index"] for row in rows)
    console.print(f"  Documents to upsert: [bold]{len(pdf_indices)}[/bold]")

    for pdf_index in pdf_indices:
        try:
            collection.delete(f'pdf_index == "{_milvus_string_literal(pdf_index)}"')
        except Exception:
            pass

    console.print(f"Inserting [bold]{len(rows)}[/bold] rows into Milvus...")
    try:
        for i in range(0, len(rows), 100):
            batch = rows[i : i + 100]
            collection.insert(batch)
            collection.flush()
    except Exception as e:
        console.print(f"[red]Insert error: {e}[/red]")
        for pdf_fn, info in file_chunk_map.items():
            add_result({
                "pdf_filename": pdf_fn, "success": False, "chunk_count": 0,
                "error": f"Insert: {e}",
            })
        connections.disconnect("default")
        if client is not None and hasattr(client, "close"):
            client.close()
        return results

    collection.load()
    console.print(f"[bold green]Done![/bold green] Collection: {collection.num_entities} entities.")

    for pdf_fn, info in file_chunk_map.items():
        add_result({
            "pdf_filename": pdf_fn,
            "success": True,
            "chunk_count": info["chunk_count"],
            "error": None,
        })

    connections.disconnect("default")
    if client is not None and hasattr(client, "close"):
        client.close()
    return results
