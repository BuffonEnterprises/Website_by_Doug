"""Skills de web: busca DuckDuckGo + leitura de páginas."""
from __future__ import annotations

from skills.registry import ToolSpec


def h_web_search(args: dict, ctx) -> str:
    query = args.get("query", "")
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
    except ImportError:
        return "[erro] busca indisponível — pip install ddgs"
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=int(args.get("max_results", 6))))
    except Exception as e:  # noqa: BLE001
        return f"[erro] busca falhou: {e}"
    if not results:
        return "(nada encontrado)"
    return "\n".join(
        f"- {r.get('title', '')}\n  {r.get('href') or r.get('url', '')}\n  {r.get('body', '')}"
        for r in results
    )


def h_web_fetch(args: dict, ctx) -> str:
    url = args.get("url", "")
    try:
        import requests
    except ImportError:
        return "[erro] pip install requests"
    try:
        resp = requests.get(url, timeout=25, stream=True,
                            headers={"User-Agent": "Jarvis/2.0 (personal assistant)"})
        resp.raise_for_status()
        total, chunks = 0, []
        for chunk in resp.iter_content(16384):
            chunks.append(chunk)
            total += len(chunk)
            if total > 3_000_000:      # teto ~3MB: evita pico de memória com página hostil
                break
        text = b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        return f"[erro] não abriu: {e}"
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(text, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "nav", "footer"]):
            tag.decompose()
        text = " ".join(soup.get_text(" ").split())
    except ImportError:
        pass
    return text[:8000] + ("\n[...truncado...]" if len(text) > 8000 else "")


def tool_specs() -> list[ToolSpec]:
    return [
        ToolSpec("web_search", "Busca na web (DuckDuckGo).",
                 {"type": "object", "properties": {
                     "query": {"type": "string"},
                     "max_results": {"type": "integer"}},
                  "required": ["query"]},
                 "read", h_web_search),
        ToolSpec("web_fetch", "Abre uma URL e devolve o texto principal.",
                 {"type": "object", "properties": {"url": {"type": "string"}},
                  "required": ["url"]},
                 "read", h_web_fetch),
    ]
