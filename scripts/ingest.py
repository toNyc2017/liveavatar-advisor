#!/usr/bin/env python3
"""
Ingest .txt and .docx files from annuity_docs/ into the ChromaDB collection.

Usage:
    python scripts/ingest.py              # skip files already in the collection
    python scripts/ingest.py --force-all  # re-ingest everything

Drop new files into annuity_docs/ and run. Already-ingested files (matched by
filename) are skipped, so you only pay embedding API cost for genuinely new content.
"""

import argparse
import os
import sys
from pathlib import Path

import chromadb
from chromadb.utils.embedding_functions import OpenAIEmbeddingFunction
from dotenv import load_dotenv
from docx import Document as DocxDocument

# ── config ────────────────────────────────────────────────────────────────────

ROOT            = Path(__file__).resolve().parent.parent
DOCS_DIR        = ROOT / "annuity_docs"
CHROMA_PATH     = str(ROOT / "chroma_db")
COLLECTION_NAME = "annuity_docs"
EMBEDDING_MODEL = "text-embedding-3-small"
CHUNK_SIZE      = 1000
CHUNK_OVERLAP   = 200

# ── text helpers ──────────────────────────────────────────────────────────────

def read_txt(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def read_docx(path: Path) -> str:
    doc = DocxDocument(str(path))
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())


def extract_text(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext == ".txt":
        return read_txt(path)
    if ext == ".docx":
        return read_docx(path)
    return None  # unsupported extension — skip silently


def chunk_text(text: str) -> list[str]:
    chunks, start = [], 0
    while start < len(text):
        chunks.append(text[start : start + CHUNK_SIZE])
        start += CHUNK_SIZE - CHUNK_OVERLAP
    return [c for c in chunks if c.strip()]

# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest annuity docs into ChromaDB.")
    parser.add_argument(
        "--force-all", action="store_true",
        help="Re-ingest all files, even ones already in the collection.",
    )
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        sys.exit("OPENAI_API_KEY not set in .env")

    if not DOCS_DIR.exists():
        sys.exit(f"annuity_docs/ not found at {DOCS_DIR}")

    ef = OpenAIEmbeddingFunction(api_key=api_key, model_name=EMBEDDING_MODEL)
    client = chromadb.PersistentClient(path=CHROMA_PATH)
    col = client.get_or_create_collection(name=COLLECTION_NAME, embedding_function=ef)
    print(f"Collection '{COLLECTION_NAME}': {col.count()} chunks before run\n")

    # Build set of already-ingested basenames from stored metadata
    already_ingested: set[str] = set()
    if not args.force_all:
        existing = col.get(include=["metadatas"])
        for meta in existing["metadatas"]:
            src = (meta or {}).get("source", "")
            if src:
                already_ingested.add(os.path.basename(src))
        print(f"Already ingested: {len(already_ingested)} source files — these will be skipped")
        print("(run with --force-all to re-ingest everything)\n")

    candidates = sorted(
        p for p in DOCS_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in {".txt", ".docx"}
    )
    print(f"Found {len(candidates)} .txt/.docx files in annuity_docs/\n")

    skipped = new_files = new_chunks = errors = 0

    for path in candidates:
        if not args.force_all and path.name in already_ingested:
            skipped += 1
            continue

        try:
            text = extract_text(path)
        except Exception as exc:
            print(f"  ERROR  {path.name}: {exc}")
            errors += 1
            continue

        if not text or not text.strip():
            print(f"  EMPTY  {path.name} — skipping")
            skipped += 1
            continue

        chunks = chunk_text(text)
        col.upsert(
            documents=chunks,
            ids=[f"{path.name}::chunk_{i}" for i in range(len(chunks))],
            metadatas=[{"source": str(path)} for _ in chunks],
        )
        print(f"  +  {path.name}  ({len(chunks)} chunks)")
        new_files += 1
        new_chunks += len(chunks)

    print(f"\n{'─'*60}")
    print(f"  Ingested : {new_files} files, {new_chunks} new chunks")
    print(f"  Skipped  : {skipped} (already present or empty)")
    print(f"  Errors   : {errors}")
    print(f"  Total    : {col.count()} chunks in collection")
    print(f"{'─'*60}")


if __name__ == "__main__":
    main()
