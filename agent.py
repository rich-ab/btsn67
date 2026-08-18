"""
Harnyx SN67 miner — AtlasQuorum v1.

A fresh, original evidence-first agent.  It uses a small quorum workflow instead
of a champion-style tool loop: generate diverse search lanes, collect and fetch
primary-looking sources, ask the model for a cited draft, run a targeted evidence
repair pass, then emit schema-safe output with compact citation slices.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from time import monotonic
from typing import Any
from urllib.parse import urlparse

from harnyx_miner_sdk.api import fetch_page, llm_chat, search_web, tooling_info
from harnyx_miner_sdk.decorators import entrypoint
from harnyx_miner_sdk.query import CitationRef, CitationSlice, Query, Response

VERSION = "atlas-quorum-v1.0"

SEARCH_PROVIDER = "parallel"
LLM_PROVIDERS = ("openrouter", "chutes")

WALL_S = 255.0
TAIL_S = 9.0
SEARCH_S = 18.0
FETCH_S = 18.0
CHAT_S = 48.0
REPAIR_S = 32.0
SCHEMA_S = 28.0

SEARCH_NUM = 8
MAX_SEARCHES = 6
MAX_FETCHES = 9
MAX_ROWS = 28
SNIPPET_CHARS = 6200
DIGEST_CHARS = 76000
ANSWER_CHARS = 48000
MAX_CITES = 18
MAX_CITE_TOTAL = 108000
CITE_TARGET = 5200

PREFERRED_MODELS = (
    "z-ai/glm-5.2",
    "openai/gpt-oss-120b",
    "deepseek/deepseek-v3.2",
    "z-ai/glm-5",
    "google/gemini-2.5-flash",
    "deepseek-ai/DeepSeek-V3.2-TEE",
    "Qwen/Qwen3.6-27B-TEE",
)

STATE: dict[str, Any] = {"models": {}}

_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'.-]{2,}")
_STOP = set(
    "the and that for from with this those these which what when where why how who whom whose "
    "are was were has have had been being into about over under between during before after "
    "using only answer question provide official result results include following based according".split()
)
_CITE = re.compile(r"\[([0-9][0-9,\s-]*)\]")
_NUM = re.compile(r"(?<!\[)\b\d[\d,]*(?:\.\d+)?%?\b")


def _left(deadline: float) -> float:
    return deadline - monotonic()


def _clean(text: str) -> str:
    return " ".join((text or "").split())


def _clip(text: str, n: int) -> str:
    value = (text or "").strip()
    return value if len(value) <= n else value[: max(0, n - 2)] + " …"


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")
    except Exception:
        return ""


def _terms(text: str, limit: int = 32) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for token in _WORD.findall((text or "").lower()):
        if token in _STOP or len(token) < 3:
            continue
        if token not in seen:
            seen.add(token)
            out.append(token)
        if len(out) >= limit:
            break
    return out


def _score_text(text: str, terms: list[str]) -> int:
    low = (text or "").lower()
    return sum(1 for term in terms if term in low)


def _question_flags(question: str) -> dict[str, bool]:
    q = question.lower()
    return {
        "set": bool(re.search(r"\b(which|what|list|all|how many|count|identify)\b", q)),
        "rank": bool(re.search(r"\b(first|last|largest|smallest|highest|lowest|most|least|rank|oldest|youngest)\b", q)),
        "source_limited": bool(re.search(r"\b(using only|according to|from the .* (?:site|database|report|filing)|solely)\b", q)),
        "bare": bool(re.search(r"\b(output only|answer only|exact text|return only|provide only)\b", q)),
    }


@dataclass
class Row:
    receipt_id: str
    result_id: str
    title: str
    url: str
    text: str
    kind: str
    spans: list[tuple[int, int]] = field(default_factory=list)


class Notebook:
    def __init__(self, question: str) -> None:
        self.question = question
        self.rows: list[Row] = []
        self.seen_urls: set[str] = set()

    def add(self, row: Row) -> int | None:
        if len(self.rows) >= MAX_ROWS or not row.receipt_id or not row.result_id or not row.text.strip():
            return None
        key = row.url.lower().split("#", 1)[0]
        if key and key in self.seen_urls and row.kind == "fetch":
            return None
        if key:
            self.seen_urls.add(key)
        self.rows.append(row)
        return len(self.rows)

    def render(self, cap: int = DIGEST_CHARS) -> str:
        if not self.rows:
            return "(no evidence)"
        terms = _terms(self.question)
        ranked: list[tuple[int, int, Row]] = []
        for idx, row in enumerate(self.rows, 1):
            h = _host(row.url)
            primary = 8 if any(x in h for x in (".gov", "sec.gov", "edu", "int", "who.int", "worldbank", "un.org")) else 0
            fetch = 5 if row.kind == "fetch" else 0
            ranked.append((_score_text(row.title + " " + row.url + " " + row.text[:1800], terms) * 4 + primary + fetch, idx, row))
        ranked.sort(key=lambda x: (-x[0], x[1]))
        chunks: list[str] = []
        used = 0
        for _, idx, row in ranked:
            pieces = []
            spans = row.spans or [(0, min(len(row.text), SNIPPET_CHARS))]
            for a, b in spans[:3]:
                pieces.append(row.text[max(0, a): min(len(row.text), b)].strip())
            excerpt = "\n...\n".join(p for p in pieces if p)[:SNIPPET_CHARS]
            block = f"[{idx}] {row.title or '(untitled)'}\nURL: {row.url}\n{excerpt}"
            if used + len(block) > cap:
                continue
            chunks.append(block)
            used += len(block)
        return "\n\n".join(chunks) if chunks else "(evidence omitted by cap)"

    def ref(self, idx: int) -> tuple[CitationRef | None, int]:
        if idx < 1 or idx > len(self.rows):
            return None, 0
        row = self.rows[idx - 1]
        spans = row.spans or [(0, min(len(row.text), SNIPPET_CHARS))]
        grown: list[tuple[int, int]] = []
        for a, b in spans[:3]:
            a = max(0, int(a)); b = min(len(row.text), int(b))
            if b <= a:
                continue
            need = max(CITE_TARGET, b - a)
            pad = max(0, need - (b - a))
            left = min(a, pad // 2)
            right = min(len(row.text) - b, pad - left)
            grown.append((a - left, b + right))
        if not grown:
            return None, 0
        merged: list[tuple[int, int]] = []
        for a, b in sorted(grown):
            if merged and a <= merged[-1][1] + 64:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        cost = sum(b - a for a, b in merged)
        return CitationRef(receipt_id=row.receipt_id, result_id=row.result_id, slices=[CitationSlice(start=a, end=b) for a, b in merged]), cost


def _best_spans(text: str, focus: str, width: int = SNIPPET_CHARS, count: int = 2) -> list[tuple[int, int]]:
    if len(text) <= width:
        return [(0, len(text))] if text else []
    terms = _terms(focus, 40)
    step = max(700, width // 3)
    scored = []
    for start in range(0, len(text), step):
        end = min(len(text), start + width)
        seg = text[start:end]
        numeric = min(5, len(re.findall(r"\d", seg)) // 15)
        scored.append((_score_text(seg, terms) * 10 + numeric, start, end))
        if end == len(text):
            break
    scored.sort(key=lambda x: (-x[0], x[1]))
    chosen: list[tuple[int, int]] = [(0, min(1200, len(text)))]
    for score, start, end in scored:
        if score <= 0 and len(chosen) > 1:
            continue
        if any(start < b and a < end for a, b in chosen):
            continue
        chosen.append((start, end))
        if len(chosen) >= count + 1:
            break
    return sorted(chosen)


async def _load_models() -> None:
    try:
        info = await tooling_info(timeout=8.0)
        providers = getattr(info, "response", {}).get("allowed_llm_provider_models", {})
        if not isinstance(providers, dict):
            return
        found: dict[str, tuple[str, ...]] = {}
        for provider in LLM_PROVIDERS:
            raw = providers.get(provider) or []
            names = []
            for item in raw:
                name = item if isinstance(item, str) else item.get("model") or item.get("id") or item.get("name") if isinstance(item, dict) else ""
                if isinstance(name, str) and name.strip() and name.strip() not in names:
                    names.append(name.strip())
            if names:
                found[provider] = tuple(names)
        if found:
            STATE["models"] = found
    except Exception:
        return


def _rank_model(name: str) -> tuple[int, str]:
    low = name.lower()
    if "glm-5.2" in low: return (0, low)
    if "gpt-oss-120b" in low: return (1, low)
    if "deepseek" in low and "v3.2" in low: return (2, low)
    if "gemini-2.5" in low: return (3, low)
    if "qwen" in low: return (4, low)
    if "glm-5" in low: return (5, low)
    return (9, low)


def _attempts() -> list[tuple[str, str]]:
    found = STATE.get("models") if isinstance(STATE.get("models"), dict) else {}
    pairs: list[tuple[str, str]] = []
    for provider in LLM_PROVIDERS:
        live = list(found.get(provider, ())) if isinstance(found, dict) else []
        if live:
            chosen = [m for m in PREFERRED_MODELS if m in live]
            rest = sorted([m for m in live if m not in chosen], key=_rank_model)
            models = (chosen + rest)[:5]
        else:
            models = list(PREFERRED_MODELS[:4])
        for model in models:
            pair = (provider, model)
            if pair not in pairs:
                pairs.append(pair)
    return pairs[:8]


async def _chat(messages: list[dict[str, Any]], deadline: float, max_tokens: int, timeout: float, temp: float = 0.05) -> str:
    for i, (provider, model) in enumerate(_attempts()):
        left = _left(deadline)
        if left <= TAIL_S + 4:
            return ""
        cap = timeout if i == 0 else min(timeout, 22.0 if i == 1 else 16.0)
        try:
            payload = await llm_chat(provider=provider, model=model, messages=messages,
                                     temperature=temp, max_output_tokens=max_tokens,
                                     timeout=min(cap, left - TAIL_S))
            text = _payload_text(payload)
            if text.strip():
                return text.strip()
        except Exception:
            continue
    return ""


def _payload_text(payload: Any) -> str:
    if payload is None:
        return ""
    for root in (getattr(payload, "llm", None), payload):
        if root is None:
            continue
        raw = getattr(root, "raw_text", None)
        if isinstance(raw, str) and raw.strip():
            return raw
        choices = getattr(root, "choices", None) or []
        if choices:
            msg = getattr(choices[0], "message", None)
            content = getattr(msg, "content", None)
            if isinstance(content, str) and content.strip():
                return content
    resp = getattr(payload, "response", None)
    if isinstance(resp, dict):
        for key in ("text", "content", "raw_text"):
            if isinstance(resp.get(key), str) and resp[key].strip():
                return resp[key]
    return ""


def _json_from(text: str) -> Any:
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip(), flags=re.I)
    try:
        return json.loads(raw)
    except Exception:
        pass
    a, b = raw.find("{"), raw.rfind("}")
    if a >= 0 and b > a:
        try:
            return json.loads(raw[a:b + 1])
        except Exception:
            return None
    return None


def _seed_queries(question: str) -> list[str]:
    flags = _question_flags(question)
    base = _clean(question)
    terms = _terms(question, 14)
    seeds = [base[:240], " ".join(terms[:10])]
    if flags["source_limited"]:
        seeds.append('"' + '" "'.join(terms[:5]) + '"')
    if flags["set"] or flags["rank"]:
        seeds.append("official table list " + " ".join(terms[:8]))
        seeds.append("site:gov OR site:edu " + " ".join(terms[:8]))
    out = []
    for seed in seeds:
        q = _clean(seed).strip(' "')
        if q and q.lower() not in [x.lower() for x in out]:
            out.append(q)
    return out[:MAX_SEARCHES]


async def _make_queries(question: str, deadline: float) -> list[str]:
    prompt = (
        "Return JSON {\"queries\":[...]} with 4-6 concise web searches for answering this hard factual question. "
        "Include primary-source and candidate-roster searches when relevant. No explanation.\nQUESTION:\n" + question
    )
    text = await _chat([{"role": "user", "content": prompt}], deadline, 900, 18.0, 0.0)
    obj = _json_from(text)
    queries = []
    if isinstance(obj, dict) and isinstance(obj.get("queries"), list):
        queries = [str(x) for x in obj["queries"] if str(x).strip()]
    merged = []
    for q in queries + _seed_queries(question):
        q = _clean(q)[:260]
        if q and q.lower() not in [x.lower() for x in merged]:
            merged.append(q)
    return merged[:MAX_SEARCHES]


async def _search_one(query: str) -> list[Row]:
    rows: list[Row] = []
    try:
        payload = await search_web(query, provider=SEARCH_PROVIDER, num=SEARCH_NUM, timeout=SEARCH_S,
                                   provider_extra={"mode": "advanced", "max_chars_total": 20000,
                                                   "excerpt_settings": {"max_chars_per_result": 2200}})
        receipt = str(getattr(payload, "receipt_id", "") or "")
        for item in list(getattr(payload, "results", None) or []):
            rid = getattr(item, "result_id", None)
            note = str(getattr(item, "note", "") or "")
            if not receipt or not isinstance(rid, str) or not note.strip():
                continue
            rows.append(Row(receipt, rid, str(getattr(item, "title", "") or ""),
                            str(getattr(item, "url", "") or ""), note, "search",
                            [(0, min(len(note), 1800))]))
    except Exception:
        return []
    return rows


async def _fetch_one(row: Row, question: str) -> Row | None:
    if not row.url.startswith(("http://", "https://")):
        return None
    try:
        payload = await fetch_page(row.url, provider=SEARCH_PROVIDER, timeout=FETCH_S,
                                   provider_extra={"objective": _clip("Find exact evidence for: " + question, 1400),
                                                   "max_chars_total": 42000,
                                                   "excerpt_settings": {"max_chars_per_result": 14000},
                                                   "full_content": True})
        receipt = str(getattr(payload, "receipt_id", "") or "")
        results = list(getattr(payload, "results", None) or [])
        if not receipt or not results:
            return None
        item = results[0]
        rid = getattr(item, "result_id", None)
        note = str(getattr(item, "note", "") or "")
        if not isinstance(rid, str) or not note.strip():
            return None
        title = str(getattr(item, "title", "") or row.title)
        url = str(getattr(item, "url", "") or row.url)
        return Row(receipt, rid, title, url, note, "fetch", _best_spans(note, question + " " + title))
    except Exception:
        return None


async def _gather(question: str, nb: Notebook, deadline: float) -> None:
    queries = await _make_queries(question, deadline)
    tasks = [asyncio.create_task(_search_one(q)) for q in queries]
    done, pending = await asyncio.wait(tasks, timeout=min(24.0, max(5.0, _left(deadline) - 120)))
    candidates: list[Row] = []
    terms = _terms(question)
    for task in done:
        for row in task.result() if not task.exception() else []:
            nb.add(row)
            candidates.append(row)
    for task in pending:
        task.cancel()
    candidates.sort(key=lambda r: (_score_text(r.title + " " + r.url + " " + r.text[:1200], terms),
                                   any(x in _host(r.url) for x in ("gov", "edu", "int", "sec.gov"))), reverse=True)
    fetches = []
    for row in candidates:
        if len(fetches) >= MAX_FETCHES or _left(deadline) < 95:
            break
        if row.url.startswith(("http://", "https://")):
            fetches.append(asyncio.create_task(_fetch_one(row, question)))
    if fetches:
        done, pending = await asyncio.wait(fetches, timeout=min(34.0, max(5.0, _left(deadline) - 65)))
        for task in done:
            if not task.exception():
                got = task.result()
                if got:
                    nb.add(got)
        for task in pending:
            task.cancel()


def _answer_prompt(question: str, nb: Notebook, repair: str = "") -> list[dict[str, Any]]:
    flags = _question_flags(question)
    system = (
        "You answer difficult factual questions for a pairwise judge. Use only numbered evidence. "
        "Every factual claim needs an immediate [n] citation. Prefer exact source wording, dates, units, and names. "
        "For set/rank/count questions, show the checked pool or arithmetic enough to prove completeness. "
        "Do not mention research limitations; answer directly."
    )
    user = f"QUESTION:\n{question}\n\nFLAGS: {flags}\n\nEVIDENCE:\n{nb.render()}\n\n"
    if repair:
        user += f"REPAIR INSTRUCTIONS:\n{repair}\n\n"
    user += "Write the final answer now."
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _markers(text: str, top: int) -> list[int]:
    seen: set[int] = set(); out: list[int] = []
    for m in _CITE.finditer(text or ""):
        for part in m.group(1).split(','):
            part = part.strip()
            rm = re.fullmatch(r"(\d+)\s*-\s*(\d+)", part)
            if rm:
                lo, hi = int(rm.group(1)), min(int(rm.group(2)), int(rm.group(1)) + 20)
                nums = range(lo, hi + 1)
            elif part.isdigit():
                nums = [int(part)]
            else:
                nums = []
            for n in nums:
                if 1 <= n <= top and n not in seen:
                    seen.add(n); out.append(n)
    return out


def _bad_numbers(answer: str, nb: Notebook) -> list[str]:
    bad: list[str] = []
    for sent in re.split(r"(?<=[.!?])\s+|\n+", answer or ""):
        cited = _markers(sent, len(nb.rows))
        if not cited:
            continue
        src = " ".join(nb.rows[i - 1].text for i in cited)
        plain = src.replace(',', '')
        for m in _NUM.finditer(_CITE.sub(' ', sent)):
            tok = m.group(0)
            if len(re.sub(r"\D", "", tok)) < 2:
                continue
            if tok not in src and tok.replace(',', '') not in plain and tok not in bad:
                bad.append(tok)
    return bad[:8]


def _usable(text: str) -> bool:
    value = (text or "").strip()
    if len(value) < 8:
        return False
    if re.search(r"^\s*(?:unable to|i cannot|sorry|no evidence)|\{\s*\"queries\"", value, re.I):
        return False
    return True


async def _repair(question: str, answer: str, nb: Notebook, deadline: float) -> str:
    if _left(deadline) < 36 or not _usable(answer):
        return answer
    issues = []
    if not _markers(answer, len(nb.rows)):
        issues.append("The answer has no usable citations; add [n] citations from evidence.")
    nums = _bad_numbers(answer, nb)
    if nums:
        issues.append("These cited numbers are not present in cited source text: " + ", ".join(nums) + ".")
    flags = _question_flags(question)
    if flags["set"] or flags["rank"]:
        issues.append("Verify completeness for any set/rank/count and cite the pool or table used.")
    if not issues:
        return answer
    fixed = await _chat(_answer_prompt(question, nb, "\n".join(issues) + " Return a complete corrected answer."),
                        deadline, 3600, REPAIR_S, 0.0)
    return fixed if _usable(fixed) and len(fixed) > max(12, len(answer) // 3) else answer


def _citations(answer: str, nb: Notebook) -> list[CitationRef]:
    refs: list[CitationRef] = []
    spent = 0
    for idx in _markers(answer, len(nb.rows)):
        if len(refs) >= MAX_CITES:
            break
        ref, cost = nb.ref(idx)
        if ref is None or spent + cost > MAX_CITE_TOTAL:
            continue
        refs.append(ref); spent += cost
    return refs


def _bare(answer: str, question: str) -> str:
    if not _question_flags(question)["bare"]:
        return answer
    for line in (answer or "").splitlines():
        line = _CITE.sub("", line).strip(" -*\t")
        if line and not re.match(r"^(proof|evidence|citation)s?:", line, re.I):
            return line
    return _CITE.sub("", answer or "").strip()


def _schema_kind(schema: Any) -> str:
    if not isinstance(schema, dict): return ""
    t = schema.get("type")
    if isinstance(t, str): return t
    if isinstance(t, list):
        return next((x for x in t if isinstance(x, str) and x != "null"), "")
    if isinstance(schema.get("properties"), dict): return "object"
    if isinstance(schema.get("items"), dict): return "array"
    return ""


async def _schema(question: str, answer: str, schema: Any, deadline: float) -> Any:
    prompt = "Convert ANSWER to JSON matching SCHEMA. Output only JSON.\nSCHEMA:\n" + json.dumps(schema) + "\nQUESTION:\n" + question + "\nANSWER:\n" + answer[:14000]
    text = await _chat([{"role": "user", "content": prompt}], deadline, 2600, SCHEMA_S, 0.0)
    try:
        return json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I))
    except Exception:
        kind = _schema_kind(schema)
        stripped = _CITE.sub("", answer).strip()
        if kind == "array": return [x.strip(" -*") for x in stripped.splitlines() if x.strip()][:20]
        if kind == "integer":
            m = re.search(r"-?\d[\d,]*", stripped); return int(m.group(0).replace(',', '')) if m else 0
        if kind == "number":
            m = re.search(r"-?\d[\d,]*(?:\.\d+)?", stripped); return float(m.group(0).replace(',', '')) if m else 0.0
        if kind == "boolean": return bool(re.search(r"\b(?:yes|true)\b", stripped, re.I))
        if kind == "object": return {}
        return stripped[:4000]


async def _solve(q: Query) -> Response:
    question = (q.text or "").strip()
    deadline = monotonic() + WALL_S
    await _load_models()
    nb = Notebook(question)
    await _gather(question, nb, deadline)
    answer = await _chat(_answer_prompt(question, nb), deadline, 4300, CHAT_S, 0.03)
    if not _usable(answer):
        answer = nb.render(5000)
    answer = await _repair(question, answer, nb, deadline)
    answer = _clip(answer.strip(), ANSWER_CHARS)
    refs = _citations(answer, nb)
    if q.output_schema is not None:
        value = await _schema(question, answer, q.output_schema, deadline)
        try:
            return Response(output=value, citations=refs or None)
        except Exception:
            return Response(output=value)
    text = _bare(answer, question) or "Unable to produce a supported answer."
    try:
        return Response(text=text, citations=refs or None)
    except Exception:
        return Response(text=text)


@entrypoint("query")
async def query(query: Query) -> Response:
    if not (query.text or "").strip():
        return Response(text="No question provided.")
    try:
        return await _solve(query)
    except Exception:
        return Response(text="Unable to produce a supported answer.")
