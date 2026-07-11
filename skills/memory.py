"""Segundo cérebro v2 — memória híbrida: FTS5 (léxica) + embeddings (semântica).

Busca híbrida por Reciprocal Rank Fusion (RRF): robusta, sem tuning de pesos.
Embeddings via fastembed (ONNX local, multilíngue, sem torch); se indisponível,
degrada para FTS5; se FTS5 faltar no SQLite, degrada para LIKE. O banco do v1
é migrado automaticamente (mesma tabela `memories`).
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import datetime

from skills.registry import ToolSpec


class Embedder:
    def __init__(self, cfg: dict):
        emb = cfg.get("memory", {}).get("embeddings", {})
        self.available = False
        self._model = None
        if not emb.get("enabled", True):
            return
        try:
            from fastembed import TextEmbedding
            self._model = TextEmbedding(model_name=emb.get(
                "model", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"))
            self.available = True
        except Exception as e:  # noqa: BLE001
            print(f"[memória] embeddings indisponíveis ({type(e).__name__}); "
                  f"usando só busca por texto.")

    def embed(self, texts: list[str]):
        import numpy as np
        vecs = list(self._model.embed(texts))
        return np.array(vecs, dtype=np.float32)


class SecondBrain:
    def __init__(self, db_path: str, embedder: Embedder):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.emb = embedder
        self.fts_ok = False
        self._lock = threading.Lock()   # acessada por múltiplas worker threads das tools
        self._init()

    # ------------------------------------------------------------------ #
    def _init(self) -> None:
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                kind TEXT NOT NULL DEFAULT 'note',
                text TEXT NOT NULL,
                tags TEXT DEFAULT '')"""
        )
        try:   # migração: coluna de importância (v3)
            self.conn.execute("ALTER TABLE memories ADD COLUMN importance INTEGER DEFAULT 1")
        except sqlite3.OperationalError:
            pass
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS vectors (id INTEGER PRIMARY KEY, vec BLOB)"
        )
        try:
            self.conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts "
                "USING fts5(text, tags)"
            )
            self.fts_ok = True
            # backfill de linhas antigas (migração do v1)
            missing = self.conn.execute(
                "SELECT id, text, tags FROM memories WHERE id NOT IN "
                "(SELECT rowid FROM memories_fts)"
            ).fetchall()
            for r in missing:
                self.conn.execute(
                    "INSERT INTO memories_fts(rowid, text, tags) VALUES (?, ?, ?)",
                    (r["id"], r["text"], r["tags"]),
                )
        except sqlite3.OperationalError:
            self.fts_ok = False
        self.conn.commit()

    # ------------------------------------------------------------------ #
    def save(self, text: str, kind: str = "note", tags: str = "", importance: int = 1) -> int:
        text = (text or "").strip()
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO memories (ts, kind, text, tags, importance) VALUES (?, ?, ?, ?, ?)",
                (time.time(), kind, text, (tags or "").strip(), int(importance or 1)),
            )
            mid = int(cur.lastrowid)
            if self.fts_ok:
                self.conn.execute(
                    "INSERT INTO memories_fts(rowid, text, tags) VALUES (?, ?, ?)",
                    (mid, text, tags or ""),
                )
            if self.emb.available:
                try:
                    vec = self.emb.embed([text])[0]
                    self.conn.execute(
                        "INSERT OR REPLACE INTO vectors (id, vec) VALUES (?, ?)",
                        (mid, vec.tobytes()),
                    )
                except Exception:  # noqa: BLE001
                    pass
            self.conn.commit()
        return mid

    def delete(self, mem_id: int) -> bool:
        with self._lock:
            cur = self.conn.execute("DELETE FROM memories WHERE id = ?", (mem_id,))
            if self.fts_ok:
                self.conn.execute("DELETE FROM memories_fts WHERE rowid = ?", (mem_id,))
            self.conn.execute("DELETE FROM vectors WHERE id = ?", (mem_id,))
            self.conn.commit()
        return cur.rowcount > 0

    def recent(self, limit: int = 10) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM memories ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._fmt(r) for r in rows]

    # ------------------------------------------------------------------ #
    def search(self, query: str, k: int = 6) -> list[dict]:
        """Busca híbrida via Reciprocal Rank Fusion (RRF, k=60)."""
        query = (query or "").strip()
        if not query:
            return []
        with self._lock:                    # _fts_ids/_vec_ids leem a conn sob o mesmo lock
            scores: dict[int, float] = {}
            for rank, mid in enumerate(self._fts_ids(query, k * 3)):
                scores[mid] = scores.get(mid, 0.0) + 1.0 / (60 + rank)
            for rank, mid in enumerate(self._vec_ids(query, k * 3)):
                scores[mid] = scores.get(mid, 0.0) + 1.0 / (60 + rank)
            if not scores:
                return []
            cand = sorted(scores, key=scores.get, reverse=True)[: k * 2]
            rows = self.conn.execute(
                f"SELECT * FROM memories WHERE id IN ({','.join('?' * len(cand))})", cand
            ).fetchall()
        by_row = {r["id"]: r for r in rows}

        def _combined(i):   # RRF + reforço leve por importância (desempate)
            r = by_row.get(i)
            imp = (r["importance"] if r is not None and "importance" in r.keys() else 1) or 1
            return scores[i] + 0.005 * imp

        ranked = sorted((i for i in cand if i in by_row), key=_combined, reverse=True)[:k]
        return [self._fmt(by_row[i]) for i in ranked]

    def _fts_ids(self, query: str, limit: int) -> list[int]:
        words = [w for w in query.split() if len(w) > 1][:12]
        if not words:
            return []
        if self.fts_ok:
            match = " OR ".join('"' + w.replace('"', '""') + '"' for w in words)
            try:
                rows = self.conn.execute(
                    "SELECT rowid FROM memories_fts WHERE memories_fts MATCH ? "
                    "ORDER BY rank LIMIT ?", (match, limit),
                ).fetchall()
                return [r[0] for r in rows]
            except sqlite3.OperationalError:
                pass
        like = f"%{query}%"
        rows = self.conn.execute(
            "SELECT id FROM memories WHERE text LIKE ? OR tags LIKE ? "
            "ORDER BY ts DESC LIMIT ?", (like, like, limit),
        ).fetchall()
        return [r[0] for r in rows]

    def _vec_ids(self, query: str, limit: int) -> list[int]:
        if not self.emb.available:
            return []
        try:
            import numpy as np
            qv = self.emb.embed([query])[0].astype(np.float32)
            d = int(qv.shape[0])
            rows = self.conn.execute("SELECT id, vec FROM vectors LIMIT 20000").fetchall()
            # descarta vetores de dimensão divergente (ex.: modelo de embedding trocado)
            good = [(r[0], r[1]) for r in rows if len(r[1]) == 4 * d]
            if not good:
                return []
            ids = [g[0] for g in good]
            mat = np.frombuffer(b"".join(g[1] for g in good),
                                dtype=np.float32).reshape(len(good), d)
            qn = qv / (np.linalg.norm(qv) + 1e-9)
            mn = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)
            sims = mn @ qn
            order = np.argsort(-sims)[:limit]
            return [ids[i] for i in order]
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def _fmt(r: sqlite3.Row) -> dict:
        return {"id": r["id"],
                "quando": datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M"),
                "tipo": r["kind"], "texto": r["text"], "tags": r["tags"]}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def _fmt_list(items: list[dict]) -> str:
    if not items:
        return "(nada na memória sobre isso)"
    return "\n".join(f"#{m['id']} [{m['quando']}] ({m['tipo']}) {m['texto']}" for m in items)


def tool_specs() -> list[ToolSpec]:
    def save(args, ctx):
        mid = ctx.memory.save(args.get("text", ""), kind=args.get("kind", "note"),
                              tags=args.get("tags", ""),
                              importance=int(args.get("importance", 1) or 1))
        return f"[ok] memória #{mid} salva"

    def reflect(args, ctx):
        facts = args.get("facts")
        if isinstance(facts, str):
            facts = [facts]
        elif not isinstance(facts, list):
            facts = []
        ids = [ctx.memory.save(f, kind="reflection", importance=3)
               for f in facts if isinstance(f, str) and f.strip()]
        return f"[ok] {len(ids)} aprendizado(s) consolidado(s) na memória"

    def search(args, ctx):
        return _fmt_list(ctx.memory.search(args.get("query", "")))

    def recent(args, ctx):
        return _fmt_list(ctx.memory.recent(int(args.get("limit", 10))))

    def forget(args, ctx):
        ok = ctx.memory.delete(int(args.get("id", 0)))
        return "[ok] esquecido" if ok else "[erro] id não encontrado"

    return [
        ToolSpec("memory_save",
                 "Salva algo no segundo cérebro (memória de longo prazo): preferências, "
                 "decisões, fatos, pessoas, projetos, tarefas. kind: note|preference|"
                 "person|project|task.",
                 {"type": "object", "properties": {
                     "text": {"type": "string"},
                     "kind": {"type": "string"},
                     "tags": {"type": "string"},
                     "importance": {"type": "integer", "description": "1 (comum) a 3 (crucial)"}},
                  "required": ["text"]},
                 "low", save),
        ToolSpec("memory_search", "Busca híbrida (semântica + texto) no segundo cérebro.",
                 {"type": "object", "properties": {"query": {"type": "string"}},
                  "required": ["query"]},
                 "read", search),
        ToolSpec("memory_recent", "Lista as memórias mais recentes.",
                 {"type": "object", "properties": {"limit": {"type": "integer"}},
                  "required": []},
                 "read", recent),
        ToolSpec("memory_forget", "Apaga uma memória pelo id.",
                 {"type": "object", "properties": {"id": {"type": "integer"}},
                  "required": ["id"]},
                 "medium", forget,
                 lambda a: f"Vou apagar a memória #{a.get('id')} pra sempre."),
        ToolSpec("memory_reflect",
                 "Consolida uma lista de aprendizados salientes na memória de longo prazo "
                 "(importância alta). Use ao fim de uma conversa importante para destilar o "
                 "que vale lembrar.",
                 {"type": "object", "properties": {
                     "facts": {"type": "array", "items": {"type": "string"}}},
                  "required": ["facts"]},
                 "low", reflect),
    ]
