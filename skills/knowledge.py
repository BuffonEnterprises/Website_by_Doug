"""Base de conhecimento (RAG) sobre os arquivos do Leo.

Indexa pastas/arquivos (código, docs, notas) em chunks com embeddings e permite
busca semântica — o Jarvis passa a "conhecer" os projetos dele. Reusa o Embedder
da memória; sem embeddings, degrada para busca textual (LIKE).
"""
from __future__ import annotations

import os
import sqlite3
import threading

from skills.registry import ToolSpec

_TEXT_EXT = {".py", ".md", ".txt", ".json", ".js", ".ts", ".tsx", ".jsx", ".html",
             ".css", ".yaml", ".yml", ".toml", ".csv", ".rst", ".java", ".go", ".rs",
             ".c", ".cpp", ".h", ".hpp", ".sh", ".rb", ".php", ".sql", ".ipynb", ".xml"}
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".next",
              "dist", "build", ".idea", ".vscode", "target"}
_MAX_FILES = 800
_MAX_BYTES = 200_000
_CHUNK = 900


def _esc_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _scope(root: str):
    """WHERE que casa o arquivo exato OU tudo sob a pasta — sem pegar irmãos
    (ex.: '/a/api' não apaga '/a/api-legacy') e sem tratar '_'/'%' como curinga."""
    base = root.rstrip("/")
    return ("path = ? OR path LIKE ? ESCAPE '\\'", (base, _esc_like(base) + "/%"))


def _chunk_text(text: str, size: int = _CHUNK) -> list[str]:
    text = text.replace("\r", "")
    out, cur = [], []
    length = 0
    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        if length + len(para) > size and cur:
            out.append("\n\n".join(cur))
            cur, length = [], 0
        cur.append(para)
        length += len(para)
    if cur:
        out.append("\n\n".join(cur))
    return [c for c in out if c.strip()][:400]


class KnowledgeBase:
    def __init__(self, db_path: str, embedder):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.emb = embedder
        self._lock = threading.Lock()
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS kb (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT, chunk TEXT, vec BLOB)"""
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS kb_path ON kb(path)")
        self.conn.commit()

    # ------------------------------------------------------------------ #
    def index(self, root: str) -> str:
        root = os.path.realpath(os.path.expanduser(root))
        if not os.path.exists(root):
            return f"[erro] caminho não encontrado: {root}"
        files = self._collect(root)
        if not files:
            return "(nenhum arquivo de texto encontrado)"
        # lê + embeda FORA do lock (não bloqueia buscas concorrentes por minutos)
        prepared = []   # (path, chunk, blob)
        for fp in files:
            try:
                with open(fp, "r", errors="replace") as f:
                    content = f.read(_MAX_BYTES)
            except Exception:  # noqa: BLE001
                continue
            chunks = _chunk_text(content)
            vecs = self._embed(chunks)
            for i, ch in enumerate(chunks):
                prepared.append((fp, ch, vecs[i].tobytes() if vecs is not None else None))
        clause, params = _scope(root)
        with self._lock:
            self.conn.execute(f"DELETE FROM kb WHERE {clause}", params)
            self.conn.executemany(
                "INSERT INTO kb (path, chunk, vec) VALUES (?, ?, ?)", prepared)
            self.conn.commit()
        return f"[ok] indexei {len(files)} arquivo(s) em {len(prepared)} trechos de {root}"

    def _collect(self, root: str) -> list[str]:
        if os.path.isfile(root):
            return [root] if os.path.splitext(root)[1].lower() in _TEXT_EXT else []
        found = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
            for fn in filenames:
                if os.path.splitext(fn)[1].lower() in _TEXT_EXT:
                    found.append(os.path.join(dirpath, fn))
                    if len(found) >= _MAX_FILES:
                        return found
        return found

    def _embed(self, chunks: list[str]):
        if not self.emb.available or not chunks:
            return None
        try:
            return self.emb.embed(chunks)
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------ #
    def search(self, query: str, k: int = 6) -> str:
        query = (query or "").strip()
        if not query:
            return "(consulta vazia)"
        rows = self._semantic(query, k) if self.emb.available else []
        if not rows:
            rows = self._textual(query, k)
        if not rows:
            return "(nada encontrado na base de conhecimento)"
        return "\n\n".join(f"[{os.path.basename(p)}]\n{c[:600]}" for p, c in rows)

    def _semantic(self, query: str, k: int):
        try:
            import numpy as np
            qv = self.emb.embed([query])[0].astype(np.float32)
            d = int(qv.shape[0])
            with self._lock:
                rows = self.conn.execute(
                    "SELECT path, chunk, vec FROM kb WHERE vec IS NOT NULL LIMIT 40000"
                ).fetchall()
            good = [(r["path"], r["chunk"], r["vec"]) for r in rows if len(r["vec"]) == 4 * d]
            if not good:
                return []
            mat = np.frombuffer(b"".join(g[2] for g in good),
                                dtype=np.float32).reshape(len(good), d)
            qn = qv / (np.linalg.norm(qv) + 1e-9)
            mn = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)
            order = np.argsort(-(mn @ qn))[:k]
            return [(good[i][0], good[i][1]) for i in order]
        except Exception:  # noqa: BLE001
            return []

    def _textual(self, query: str, k: int):
        like = f"%{query}%"
        with self._lock:
            rows = self.conn.execute(
                "SELECT path, chunk FROM kb WHERE chunk LIKE ? LIMIT ?", (like, k)
            ).fetchall()
        return [(r["path"], r["chunk"]) for r in rows]

    def forget(self, root: str) -> str:
        root = os.path.realpath(os.path.expanduser(root))
        clause, params = _scope(root)
        with self._lock:
            cur = self.conn.execute(f"DELETE FROM kb WHERE {clause}", params)
            self.conn.commit()
        return f"[ok] removi {cur.rowcount} trechos de {root}"

    def stats(self) -> str:
        with self._lock:
            n = self.conn.execute("SELECT COUNT(*), COUNT(DISTINCT path) FROM kb").fetchone()
        return f"{n[0]} trechos de {n[1]} arquivos indexados"

    # ------------------------------------------------------------------ #
    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec("knowledge_index",
                     "Indexa uma pasta ou arquivo na base de conhecimento (RAG) para "
                     "você poder consultar depois. Ex.: um projeto de código ou pasta de notas.",
                     {"type": "object", "properties": {"path": {"type": "string"}},
                      "required": ["path"]},
                     "low", lambda a, c: self.index(a.get("path", ""))),
            ToolSpec("knowledge_search",
                     "Busca semântica na base de conhecimento indexada (arquivos do Leo).",
                     {"type": "object", "properties": {"query": {"type": "string"}},
                      "required": ["query"]},
                     "read", lambda a, c: self.search(a.get("query", ""))),
            ToolSpec("knowledge_forget", "Remove uma pasta/arquivo da base de conhecimento.",
                     {"type": "object", "properties": {"path": {"type": "string"}},
                      "required": ["path"]},
                     "medium", lambda a, c: self.forget(a.get("path", "")),
                     lambda a: f"Vou remover {a.get('path', '')} da base de conhecimento."),
        ]
