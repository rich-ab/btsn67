"""Harnyx SN67 miner — 151 (Richjg).

This candidate is built around a bounded evidence-compiler rather than a long
conversational tool loop.  It is designed from a real local-eval failure where
all core facts were found correctly, but the pairwise judge preferred the other
answer because it preserved source labels more faithfully, explained a subtle
count discrepancy, and finished more cleanly.

Controller topology:
    question -> deterministic contract + source policy -> Parallel-primary discovery
             -> hard source-host lock when the prompt requires a named source
             -> official-page fetch + sibling-stage derivation -> proof grid
             -> mandatory per-subquestion coverage compiler -> quote verification
             -> grounded answer selection -> deterministic hallucination guard
             -> source-verbatim restoration -> schema adapter

Design goals:
- reserve final-answer time instead of researching until the wall clock expires;
- preserve exact names, marks, capitalization, units, dates and labels from source;
- answer every numbered/multipart sub-question in order;
- enforce hard source locking for prompts that say using only/solely/from the official named source;
- refuse to finalize a multipart answer until every required part has grounded evidence;
- resolve same-slot factual contradictions before any answer can leave the controller;
- bind evidence to the requested event/discipline/sex and reject neighboring-event pages;
- pin canonical event-specific result routes before generic competition/calendar pages;
- prefer section-scoped table counts over whole-page row aggregates;
- explain discrepancy/counterfactual questions when the evidence supports why;
- keep exact receipt-backed citation slices around decisive quotes;
- never return the user's question as a fallback answer;
- use Parallel as the fast primary retrieval path, with one DeSearch fallback only when necessary;
- remain useful when one LLM/search/fetch stage fails.
"""

from __future__ import annotations

import asyncio
import json
import re
from time import monotonic
from typing import Any
from urllib.parse import urlparse

from harnyx_miner_sdk.api import fetch_page, llm_chat, search_web, tooling_info
from harnyx_miner_sdk.decorators import entrypoint
from harnyx_miner_sdk.query import CitationRef, CitationSlice, Query, Response


VERSION = "canonical-event-v12.0"

# ---------------------------------------------------------------------------
# Runtime policy
# ---------------------------------------------------------------------------

LLM_PROVIDER = "chutes"
SEARCH_PROVIDER = "parallel"
FALLBACK_SEARCH_PROVIDER = "desearch"

PLAN_MODELS = (
    "google/gemma-4-31B-turbo-TEE",
    "deepseek-ai/DeepSeek-V3.2-TEE",
    "Qwen/Qwen3.6-27B-TEE",
)
GRID_MODELS = (
    "deepseek-ai/DeepSeek-V3.2-TEE",
    "google/gemma-4-31B-turbo-TEE",
    "Qwen/Qwen3.6-27B-TEE",
    "Qwen/Qwen3.5-397B-A17B-TEE",
    "moonshotai/Kimi-K2.6-TEE",
)
WRITE_MODELS = (
    "deepseek-ai/DeepSeek-V3.2-TEE",
    "google/gemma-4-31B-turbo-TEE",
    "Qwen/Qwen3.6-27B-TEE",
    "Qwen/Qwen3.5-397B-A17B-TEE",
)
REVIEW_MODELS = (
    "google/gemma-4-31B-turbo-TEE",
    "deepseek-ai/DeepSeek-V3.2-TEE",
    "Qwen/Qwen3.5-397B-A17B-TEE",
)
SCHEMA_MODELS = (
    "google/gemma-4-31B-turbo-TEE",
    "deepseek-ai/DeepSeek-V3.2-TEE",
    "Qwen/Qwen3.6-27B-TEE",
)

# A Harnyx evaluation can have a larger outer timeout, but this agent commits
# well before it.  The v3 local run used ~251s and left too little finalization
# margin; v4 targets materially less.
WALL_SECONDS = 180.0
REPAIR_LATEST_ELAPSED = 105.0
REVIEW_LATEST_ELAPSED = 135.0
FORCE_COMMIT_ELAPSED = 158.0

PLAN_TIMEOUT = 10.0
# Parallel is the primary retrieval provider. DeSearch is retained only as a
# last-resort fallback because local runs showed 50-70+ second DeSearch latency.
SEARCH_TIMEOUT = 24.0
SEARCH_RETRY_TIMEOUT = 22.0
DESEARCH_FALLBACK_TIMEOUT = 82.0
FETCH_TIMEOUT = 32.0
GRID_TIMEOUT = 42.0
WRITE_TIMEOUT = 38.0
REVIEW_TIMEOUT = 26.0
SCHEMA_TIMEOUT = 20.0
EMERGENCY_TIMEOUT = 36.0

MAX_PLAN_QUERIES = 3
MAX_REPAIR_QUERIES = 1
SEARCH_RESULTS = 8
FETCH_CAP = 4
SEARCH_CONCURRENCY = 5
FETCH_CONCURRENCY = 5
MAX_SOURCES = 42
MAX_CITATIONS = 18
MAX_EVIDENCE_CHARS = 104_000
MAX_SOURCE_TEXT = 300_000
MAX_PACK_CHARS = 60_000
MAX_ANSWER_CHARS = 56_000
SEARCH_PREVIEW = 1800
FETCH_HEAD = 2600
FETCH_WINDOW = 5200
FETCH_WINDOW_COUNT = 3
QUOTE_CONTEXT = 620
MAX_SLICE_PER_SOURCE = 15_000
MIN_SLICE = 900

MIN_PLAN_USD = 0.010
MIN_GRID_USD = 0.020
MIN_WRITE_USD = 0.016
MIN_REVIEW_USD = 0.020
MIN_SCHEMA_USD = 0.010

_STATE: dict[str, Any] = {
    "remaining_usd": None,
    "allowed_models": (),
}

# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


def _remember_budget(payload: Any) -> None:
    budget = getattr(payload, "budget", None)
    remaining = getattr(budget, "session_remaining_budget_usd", None)
    if isinstance(remaining, (int, float)):
        _STATE["remaining_usd"] = float(remaining)


def _money_left() -> float:
    value = _STATE.get("remaining_usd")
    if isinstance(value, (int, float)):
        return float(value)
    return 1.0


def _left(deadline: float) -> float:
    return max(0.0, deadline - monotonic())


def _elapsed(started: float) -> float:
    return max(0.0, monotonic() - started)


def _clean_space(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _cap(text: str, limit: int) -> str:
    value = text or ""
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 2)].rstrip() + " …"


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _uniq_text(values: Any, limit: int, max_len: int = 500) -> list[str]:
    out: list[str] = []
    if not isinstance(values, list):
        return out
    lowered: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        item = _clean_space(value)
        if not item or len(item) > max_len:
            continue
        key = item.lower()
        if key in lowered:
            continue
        lowered.add(key)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def _json_object(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    start = raw.find("{")
    if start < 0:
        return None
    depth = 0
    quoted = False
    escaped = False
    for pos in range(start, len(raw)):
        ch = raw[pos]
        if quoted:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                quoted = False
            continue
        if ch == '"':
            quoted = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(raw[start : pos + 1])
                    return obj if isinstance(obj, dict) else None
                except Exception:
                    return None
    return None


def _host(url: str) -> str:
    try:
        return (urlparse(url or "").hostname or "").lower()
    except Exception:
        return ""


def _token_terms(text: str) -> list[str]:
    raw = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9][A-Za-zÀ-ÖØ-öø-ÿ0-9_.:/+%-]*", text or "")
    stop = {
        "the", "a", "an", "and", "or", "of", "to", "for", "in", "on", "at",
        "by", "with", "from", "as", "is", "are", "was", "were", "be", "been",
        "that", "this", "these", "those", "which", "what", "who", "when", "where",
        "how", "using", "only", "official", "result", "results", "page", "pages",
        "commentary", "claims", "claim", "answer", "plain", "prose", "check",
        "against", "usual", "exactly", "given", "shown", "printed", "requested",
    }
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        value = item.strip("._:/+%-")
        if len(value) < 3:
            continue
        key = value.lower()
        if key in stop or key in seen:
            continue
        seen.add(key)
        out.append(value)
        if len(out) >= 28:
            break
    return out


def _authority(url: str, title: str, named_hints: list[str]) -> int:
    host = _host(url)
    low_title = (title or "").lower()
    score = 0
    if host.endswith(".gov") or ".gov." in host:
        score += 12
    if host.endswith(".edu") or ".edu." in host:
        score += 8
    if any(piece in host for piece in ("sec.gov", "who.int", "un.org", "worldbank.org", "oecd.org")):
        score += 11
    if any(piece in host for piece in ("docs.", "developer.", "support.", "help.", "data.")):
        score += 5
    if any(word in low_title for word in ("official", "results", "filing", "report", "statistics", "database")):
        score += 4
    for hint in named_hints:
        h = re.sub(r"[^a-z0-9]", "", hint.lower())
        if not h:
            continue
        host_flat = re.sub(r"[^a-z0-9]", "", host)
        title_flat = re.sub(r"[^a-z0-9]", "", low_title)
        if h in host_flat or h in title_flat:
            score += 14
    return score


def _event_identity_score(question: str, url: str, title: str, body: str = "") -> int:
    """Reward exact event/entity identity and strongly penalize same-domain wrong events.

    Authority alone is unsafe: an official domain can host thousands of unrelated
    competitions. This score keeps the requested event/year/discipline attached
    to the evidence path.
    """
    q = _clean_space(question).lower()
    hay = _clean_space(f"{title} {url} {(body or '')[:2600]}").lower()
    score = 0

    # Exact multi-word event anchors matter more than generic domain authority.
    anchors = (
        "world athletics championships",
        "olympic games",
        "world championships",
        "uefa champions league",
        "fifa world cup",
        "super bowl",
        "academy awards",
    )
    for anchor in anchors:
        if anchor in q:
            slug = anchor.replace(" ", "-")
            if anchor in hay or slug in hay:
                score += 28
            else:
                score -= 12

    # Explicit year should survive source selection.
    years = re.findall(r"\b(?:19|20)\d{2}\b", q)
    for year in years[:2]:
        if year in hay:
            score += 8

    # Numeric discipline / product / version identifiers are often decisive.
    for token in re.findall(r"\b\d{3,5}\b", q):
        if token in years:
            continue
        if token in hay:
            score += 7

    # Location anchors help separate similarly named annual events.
    for place in ("tokyo", "paris", "london", "new york", "los angeles", "rome"):
        if place in q and place in hay:
            score += 5

    # Known same-domain distractor patterns. These are deliberately phrased as
    # generic mismatch penalties rather than a hard-coded answer.
    mismatch_terms = (
        "national sports festival",
        "qualification for the national",
        "junior championships",
        "u20 championships",
    )
    if any(term in hay for term in mismatch_terms) and not any(term in q for term in mismatch_terms):
        score -= 70

    # For result-stage questions, reward pages that visibly name the requested stage.
    if "semi" in q and ("semi-final" in hay or "semifinal" in hay):
        score += 7
    if "final" in q and "final" in hay:
        score += 7

    # V11 failure mode: a generic official competition page contained the
    # requested discipline in its navigation menu while the active table was a
    # completely different event. Canonical routes and active headings now
    # dominate generic authority/text overlap.
    score += _canonical_result_route_score(question, url)
    state = _event_match_state(question, url, title, body)
    if state == 2:
        score += 90
    elif state == 1:
        score += 45
    elif state < 0:
        score -= 180
    return score



def _event_signature(value: str) -> dict[str, str]:
    """Extract a compact event identity from a question, URL-ish text, or heading.

    The goal is not to understand every sport.  It is to recognize enough
    explicit identity to veto a clearly different neighboring event on the same
    official competition page.  Unknown is safer than a false mismatch.
    """
    raw = _clean_space(value)
    low = raw.lower().replace("×", "x")
    sex = ""
    if re.search(r"\bmen(?:'s)?\b", low) and not re.search(r"\bwomen(?:'s)?\b", low):
        sex = "men"
    elif re.search(r"\bwomen(?:'s)?\b", low):
        sex = "women"

    discipline = ""
    # Relays must be detected before the single-distance expression.
    relay = re.search(r"\b(4)\s*[x×]\s*(\d{2,4})\s*(?:m|metres|meters)?\b", low)
    if relay:
        discipline = f"{relay.group(1)}x{relay.group(2)}-metres-relay"
    else:
        distance = re.search(r"\b(\d{2,5})\s*(?:m|metres|meters)\b", low)
        if distance:
            discipline = f"{distance.group(1)}-metres"

    named = (
        ("3000-metres-steeplechase", ("3000 metres steeplechase", "3000m steeplechase")),
        ("110-metres-hurdles", ("110 metres hurdles", "110m hurdles")),
        ("100-metres-hurdles", ("100 metres hurdles", "100m hurdles")),
        ("400-metres-hurdles", ("400 metres hurdles", "400m hurdles")),
        ("high-jump", ("high jump",)),
        ("long-jump", ("long jump",)),
        ("triple-jump", ("triple jump",)),
        ("pole-vault", ("pole vault",)),
        ("shot-put", ("shot put",)),
        ("discus-throw", ("discus throw",)),
        ("hammer-throw", ("hammer throw",)),
        ("javelin-throw", ("javelin throw",)),
        ("decathlon", ("decathlon",)),
        ("heptathlon", ("heptathlon",)),
        ("marathon", ("marathon",)),
    )
    # Named disciplines are more specific than a stray number elsewhere.
    for slug, aliases in named:
        if any(alias in low for alias in aliases):
            discipline = slug
            break

    return {"sex": sex, "discipline": discipline}


def _expected_event_signature(question: str) -> dict[str, str]:
    return _event_signature(question)


def _heading_candidate(line: str) -> str:
    raw = _clean_space(line)
    if not raw:
        return ""
    # Strip markdown/list formatting but keep apostrophes and event punctuation.
    raw = re.sub(r"^[#>*_\-\s]+", "", raw).strip()
    raw = re.sub(r"\s+", " ", raw)
    if not raw or len(raw) > 125:
        return ""
    return raw


def _active_event_heading(body: str) -> str:
    """Return the first visible heading that looks like an actual event.

    Navigation menus often contain every discipline on one very long line.  We
    intentionally ignore long/menu-like lines and favor short headings such as
    `Men's 1500 Metres` or `Men's 4x400 Metres Relay`.
    """
    if not body:
        return ""
    for raw in body.splitlines()[:260]:
        candidate = _heading_candidate(raw)
        if not candidate:
            continue
        sig = _event_signature(candidate)
        if not sig.get("discipline"):
            continue
        low = candidate.lower()
        # Exclude generic menu/index strings that happen to enumerate events.
        if low.count("metres") + low.count("meters") > 2:
            continue
        if len(candidate.split()) > 12:
            continue
        return candidate
    return ""


def _event_match_state(question: str, url: str, title: str, body: str = "") -> int:
    """Return 2 strong match, 1 visible match, 0 unknown, -1 explicit mismatch."""
    expected = _expected_event_signature(question)
    if not expected.get("discipline") and not expected.get("sex"):
        return 0

    path_text = urlparse(url or "").path.lower().replace("_", "-")
    path_text = path_text.replace("%20", "-")
    expected_disc = expected.get("discipline", "")
    expected_sex = expected.get("sex", "")

    # Canonical event routes are strongest evidence of identity.
    route_disc_match = bool(expected_disc and expected_disc in path_text)
    route_sex_match = bool(expected_sex and f"/{expected_sex}/" in path_text)
    if route_disc_match and (not expected_sex or route_sex_match):
        return 2

    heading = _active_event_heading(body)
    if heading:
        actual = _event_signature(heading)
        actual_disc = actual.get("discipline", "")
        actual_sex = actual.get("sex", "")
        if expected_disc and actual_disc and expected_disc != actual_disc:
            return -1
        if expected_sex and actual_sex and expected_sex != actual_sex:
            return -1
        if expected_disc and actual_disc == expected_disc:
            return 1
        if expected_sex and actual_sex == expected_sex and not expected_disc:
            return 1

    # Titles can confirm identity but should not veto a page because many
    # official result titles are generic.
    title_sig = _event_signature(title or "")
    if expected_disc and title_sig.get("discipline") == expected_disc:
        if not expected_sex or not title_sig.get("sex") or title_sig.get("sex") == expected_sex:
            return 1
    return 0


def _canonical_result_route_score(question: str, url: str) -> int:
    """Reward event-specific result routes and penalize clearly different routes."""
    expected = _expected_event_signature(question)
    discipline = expected.get("discipline", "")
    sex = expected.get("sex", "")
    path = urlparse(url or "").path.lower().replace("_", "-")
    score = 0
    if "/results/" in path:
        score += 10
    if discipline:
        if discipline in path:
            score += 90
        else:
            # If the path explicitly names another recognized event under a
            # result route, it is a strong mismatch. Generic calendar pages stay
            # neutral and can still be validated by their active heading.
            path_sig = _event_signature(path.replace("-", " "))
            other = path_sig.get("discipline", "")
            if other and other != discipline:
                score -= 100
    if sex:
        if f"/{sex}/" in path:
            score += 35
        elif re.search(r"/(?:men|women)/", path):
            score -= 70
    if "/semi-final/result" in path or "/semifinal/result" in path:
        score += 35
    if "/final/result" in path:
        score += 35
    return score


def _canonical_event_source(question: str, row: dict[str, Any]) -> bool:
    state = _event_match_state(
        question,
        str(row.get("url") or ""),
        str(row.get("title") or ""),
        str(row.get("text") or "")[:5000],
    )
    return state >= 1 or _canonical_result_route_score(question, str(row.get("url") or "")) >= 100


def _event_source_rejected(question: str, row: dict[str, Any]) -> bool:
    return _event_match_state(
        question,
        str(row.get("url") or ""),
        str(row.get("title") or ""),
        str(row.get("text") or "")[:8000],
    ) < 0


def _canonical_stage_urls(url: str, question: str) -> list[str]:
    """Return the event-specific URL and obvious sibling stage URLs."""
    target = (url or "").strip()
    if not target:
        return []
    if _canonical_result_route_score(question, target) < 100:
        return []
    out = [target]
    for sibling in _stage_sibling_urls(target, question):
        if sibling not in out:
            out.append(sibling)
    return out


def _best_windows(text: str, focus: str, width: int = FETCH_WINDOW, count: int = FETCH_WINDOW_COUNT) -> list[tuple[int, int]]:
    if not text:
        return []
    if len(text) <= width:
        return [(0, len(text))]
    terms = [x.lower() for x in _token_terms(focus)[:18]]
    if not terms:
        return [(0, min(len(text), width))]
    lower = text.lower()
    candidates: list[tuple[int, int, int]] = []
    step = max(600, width // 3)
    for start in range(0, len(text), step):
        end = min(len(text), start + width)
        block = lower[start:end]
        score = 0
        for term in terms:
            hits = block.count(term)
            if hits:
                score += min(hits, 5) * (2 if len(term) >= 7 else 1)
        candidates.append((-score, start, end))
        if end == len(text):
            break
    candidates.sort()
    selected: list[tuple[int, int]] = []
    for neg, start, end in candidates:
        if neg == 0 and selected:
            break
        overlap = False
        for old_start, old_end in selected:
            if min(end, old_end) - max(start, old_start) > width // 2:
                overlap = True
                break
        if not overlap:
            selected.append((start, end))
        if len(selected) >= count:
            break
    return sorted(selected)


def _merge_spans(spans: list[tuple[int, int]], text_len: int) -> list[tuple[int, int]]:
    cleaned: list[tuple[int, int]] = []
    for start, end in spans:
        a = max(0, min(text_len, _int(start)))
        b = max(0, min(text_len, _int(end)))
        if b > a:
            cleaned.append((a, b))
    cleaned.sort()
    merged: list[tuple[int, int]] = []
    for start, end in cleaned:
        if not merged or start > merged[-1][1] + 80:
            merged.append((start, end))
        else:
            old_start, old_end = merged[-1]
            merged[-1] = (old_start, max(old_end, end))
    return merged


def _llm_text(payload: Any) -> str:
    if payload is None:
        return ""
    direct = getattr(payload, "llm", None)
    if direct is not None:
        value = getattr(direct, "raw_text", None)
        if isinstance(value, str):
            return value
    value = getattr(payload, "raw_text", None)
    if isinstance(value, str):
        return value
    response = getattr(payload, "response", None)
    if isinstance(response, dict):
        for key in ("text", "content", "raw_text"):
            item = response.get(key)
            if isinstance(item, str):
                return item
    return ""


# ---------------------------------------------------------------------------
# Tooling/model selection
# ---------------------------------------------------------------------------


async def _load_tooling() -> None:
    """Load current allowed model ids from tooling_info().response."""
    try:
        info = await tooling_info(timeout=8.0)
        _remember_budget(info)
        response = getattr(info, "response", None)
        if not isinstance(response, dict):
            return
        provider_models = response.get("allowed_llm_provider_models")
        if not isinstance(provider_models, dict):
            return
        raw = provider_models.get(LLM_PROVIDER)
        found: list[str] = []
        if isinstance(raw, (list, tuple)):
            for item in raw:
                if isinstance(item, str) and item.strip() and item.strip() not in found:
                    found.append(item.strip())
                elif isinstance(item, dict):
                    name = item.get("model") or item.get("id") or item.get("name")
                    if isinstance(name, str) and name.strip() and name.strip() not in found:
                        found.append(name.strip())
        if found:
            _STATE["allowed_models"] = tuple(found)
    except Exception:
        return


def _model_rank(model: str) -> tuple[int, int]:
    low = model.lower()
    hints = (
        "deepseek-v3.2",
        "gemma-4-31b",
        "qwen3.6",
        "qwen3.5-397",
        "kimi-k2",
        "glm-5.2",
    )
    for idx, hint in enumerate(hints):
        if hint in low:
            return (idx, -len(model))
    return (50, -len(model))


def _models(preferred: tuple[str, ...]) -> list[str]:
    live = _STATE.get("allowed_models")
    if isinstance(live, tuple) and live:
        allowed = [x for x in live if isinstance(x, str) and x]
        exact = [x for x in preferred if x in allowed]
        rest = [x for x in allowed if x not in exact]
        rest.sort(key=_model_rank)
        return (exact + rest)[:5]
    out = ["google/gemma-4-31B-turbo-TEE"]
    for item in preferred:
        if item not in out:
            out.append(item)
    return out[:5]


async def _chat(
    preferred: tuple[str, ...],
    messages: list[dict[str, Any]],
    deadline: float,
    max_tokens: int,
    timeout_cap: float,
    temperature: float = 0.0,
) -> Any:
    models = _models(preferred)
    for idx, model in enumerate(models):
        # The first route gets the full allowance. Later fallbacks are shorter so
        # a 503/slow chute cannot consume the entire task wall clock.
        attempt_cap = timeout_cap
        if idx == 1:
            attempt_cap = min(timeout_cap, 34.0)
        elif idx >= 2:
            attempt_cap = min(timeout_cap, 28.0)
        timeout = min(attempt_cap, _left(deadline) - 2.0)
        if timeout <= 5.0:
            return None
        try:
            payload = await llm_chat(
                provider=LLM_PROVIDER,
                model=model,
                messages=messages,
                temperature=temperature,
                max_output_tokens=max_tokens,
                timeout=timeout,
            )
            _remember_budget(payload)
            if _llm_text(payload).strip():
                return payload
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Question contract and research plan
# ---------------------------------------------------------------------------


class QuestionMap:
    def __init__(self, question: str, output_schema: Any) -> None:
        self.question = question
        self.output_schema = output_schema
        self.parts: list[str] = []
        self.named_sources: list[str] = []
        self.entities: list[str] = []
        self.hard_source_lock = False
        self.must_preserve_exact = False
        self.explain_difference = False
        self.complete_set = False
        self.superlative = False
        self.computed = False
        self.strict_output = False
        self.ordering = ""
        self.answer_kind = "fact"
        self._derive()

    def _derive(self) -> None:
        q = self.question
        low = q.lower()
        if re.search(
            r"\b(?:using|use|based on|from)\s+(?:only|solely|exclusively)\s+(?:the\s+)?(?:official\s+)?|"
            r"\busing\s+only\s+the\s+official\b|\bonly\s+the\s+official\b",
            low,
        ):
            self.hard_source_lock = True
        if re.search(r"exactly as (?:given|shown|printed|written)|verbatim|exact labels?|exact names?|exact marks?", low):
            self.must_preserve_exact = True
        if re.search(r"how .*compare|compare(?:s|d)? with|difference|why .*more|why .*less|discrep", low):
            self.explain_difference = True
        if re.search(r"\b(all|every|each|both|complete list|which of these|among the .* which)\b", low):
            self.complete_set = True
        if re.search(r"\b(highest|lowest|largest|smallest|best|worst|most|least|top\s+\d+|first|last)\b", low):
            self.superlative = True
        if re.search(r"\b(total|sum|average|mean|ratio|percent|percentage|how many|number of|difference)\b", low):
            self.computed = True
        if "only the answer" in low or "respond with only" in low or "nothing else" in low:
            self.strict_output = True
        if "alphabetical" in low:
            self.ordering = "alphabetical"
        elif "chronological" in low:
            self.ordering = "chronological"
        elif "highest to lowest" in low or "descending" in low:
            self.ordering = "descending"
        elif "lowest to highest" in low or "ascending" in low:
            self.ordering = "ascending"
        if re.search(r"\bhow many\b|\bnumber of\b", low):
            self.answer_kind = "number/comparison"
        elif self.complete_set:
            self.answer_kind = "list/set"
        elif self.superlative:
            self.answer_kind = "ranking/fact"
        # Slice between numbered markers so embedded labels such as "(Q)" do
        # not truncate or erase later sub-questions.
        markers = list(re.finditer(r"(?:^|\s)\((\d+)\)\s*", q))
        for idx, marker in enumerate(markers):
            body_start = marker.end()
            body_end = markers[idx + 1].start() if idx + 1 < len(markers) else len(q)
            item = _clean_space(q[body_start:body_end])
            if item:
                self.parts.append(item[:900])
        if not self.parts and q.count("?") > 1:
            for item in q.split("?"):
                item = _clean_space(item)
                if item:
                    self.parts.append(item[:700])
        # Capture source brands explicitly named in common constructions.
        patterns = (
            r"official\s+([A-Z][A-Za-z0-9&. -]{2,60}?)(?:\s+(?:results?|page|website|site|data|database|report|competition))",
            r"according to\s+([A-Z][A-Za-z0-9&. -]{2,60}?)(?:[,.;]|\s+(?:data|results?|report))",
        )
        for pattern in patterns:
            for match in re.finditer(pattern, q):
                item = _clean_space(match.group(1))
                if item and item.lower() not in [x.lower() for x in self.named_sources]:
                    self.named_sources.append(item)
        # Proper-noun phrases help retrieval but are not treated as facts themselves.
        for match in re.finditer(r"\b(?:[A-Z][A-Za-zÀ-ÖØ-öø-ÿ.'-]+(?:\s+|$)){2,6}", q):
            item = _clean_space(match.group(0))
            if 3 <= len(item) <= 100 and item.lower() not in [x.lower() for x in self.entities]:
                self.entities.append(item)
            if len(self.entities) >= 16:
                break

    def block(self) -> str:
        return json.dumps(
            {
                "answer_kind": self.answer_kind,
                "parts": self.parts,
                "named_sources": self.named_sources,
                "hard_source_lock": self.hard_source_lock,
                "entities": self.entities,
                "must_preserve_exact": self.must_preserve_exact,
                "explain_difference": self.explain_difference,
                "complete_set": self.complete_set,
                "superlative": self.superlative,
                "computed": self.computed,
                "strict_output": self.strict_output,
                "ordering": self.ordering,
            },
            ensure_ascii=False,
        )


class ResearchPlan:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.focus_terms: list[str] = []
        self.must_answer: list[str] = []
        self.must_explain: list[str] = []
        self.exact_values_needed: list[str] = []
        self.preferred_domains: list[str] = []

    def apply(self, data: dict[str, Any], qmap: QuestionMap) -> None:
        self.queries = _uniq_text(data.get("queries"), MAX_PLAN_QUERIES, 430)
        self.focus_terms = _uniq_text(data.get("focus_terms"), 18, 120)
        self.must_answer = _uniq_text(data.get("must_answer"), 10, 500)
        self.must_explain = _uniq_text(data.get("must_explain"), 8, 500)
        self.exact_values_needed = _uniq_text(data.get("exact_values_needed"), 14, 220)
        self.preferred_domains = _uniq_text(data.get("preferred_domains"), 6, 120)
        if not self.must_answer:
            self.must_answer = list(qmap.parts) if qmap.parts else [qmap.question[:700]]

    def block(self) -> str:
        return json.dumps(
            {
                "queries": self.queries,
                "focus_terms": self.focus_terms,
                "must_answer": self.must_answer,
                "must_explain": self.must_explain,
                "exact_values_needed": self.exact_values_needed,
                "preferred_domains": self.preferred_domains,
            },
            ensure_ascii=False,
        )


def _query_prefix(question: str) -> str:
    """Return the source/event clause, not the potentially false commentary claim."""
    q = _clean_space(question)
    if not q:
        return ""
    prefix = q.split(":", 1)[0] if ":" in q else q
    prefix = re.sub(r"^(?:using|based on|according to)\s+(?:only\s+)?(?:the\s+)?", "", prefix, flags=re.I)
    prefix = re.sub(r"\s+", " ", prefix).strip(" ,.;:-")
    if len(prefix) > 240:
        prefix = " ".join(prefix.split()[:28])
    return prefix


def _compact_queries(qmap: QuestionMap) -> list[str]:
    """Build a few compact high-signal Parallel queries.

    False claims in benchmark prompts are deliberately excluded. When a prompt
    contrasts stages such as semifinal/final, issue stage-specific searches so
    both official tables can surface independently instead of relying on one
    broad query.
    """
    q = qmap.question
    low = q.lower()
    source = qmap.named_sources[0] if qmap.named_sources else ""
    prefix = _query_prefix(q)
    years = list(dict.fromkeys(re.findall(r"\b(?:19|20)\d{2}\b", q)))[:2]

    source_words = {x.lower() for x in _token_terms(source)}
    blocked = {
        "commentary", "claims", "claim", "won", "winner", "usual", "exactly",
        "competition", "results", "result", "official", "championships", "championship",
    }
    salient: list[str] = []
    for term in _token_terms(prefix):
        key = term.lower()
        if key in source_words or key in blocked or re.fullmatch(r"(?:19|20)\d{2}", term):
            continue
        if term not in salient:
            salient.append(term)
        if len(salient) >= 6:
            break

    base: list[str] = []
    if source:
        base.extend(source.split())
    base.extend(years)
    base.extend(salient[:5])
    if not base:
        base.extend(_token_terms(prefix)[:8])

    variants: list[list[str]] = []
    if "semifinal" in low or "semi-final" in low:
        variants.append(base + ["semifinal", "results"])
    if re.search(r"\bfinal\b", low):
        variants.append(base + ["final", "results"])
    if any(word in low for word in ("qualif", "advancement", "advance")):
        variants.append(base + ["qualification", "rule", "results"])
    if not variants:
        variants.append(base + ["results"])

    out: list[str] = []
    for parts in variants:
        words: list[str] = []
        seen: set[str] = set()
        for piece in parts:
            for word in str(piece).split():
                key = word.lower().strip(".,;:()[]{}\"'")
                if not key or key in seen:
                    continue
                seen.add(key)
                words.append(word.strip(".,;:()[]{}\"'"))
        candidate = _clean_space(" ".join(words[:13]))
        if candidate and candidate.lower() not in [x.lower() for x in out]:
            out.append(candidate)
        if len(out) >= MAX_PLAN_QUERIES:
            break
    if not out:
        out.append(_cap(_clean_space(prefix or q), 180))
    return out

def _fallback_plan(qmap: QuestionMap) -> ResearchPlan:
    plan = ResearchPlan()
    q = qmap.question
    plan.must_answer = list(qmap.parts) if qmap.parts else [q[:700]]
    raw_focus = _token_terms(_query_prefix(q) + " " + q)
    plan.focus_terms = [
        term for term in raw_focus
        if term.lower() not in {"won", "winner", "claims", "claim", "commentary", "usual"}
    ][:16]
    plan.queries = _compact_queries(qmap)
    if qmap.explain_difference:
        plan.must_explain.append(
            "Explain the observed discrepancy from actual source rows/statuses; do not infer the answer from the rule alone."
        )
    if qmap.must_preserve_exact:
        plan.exact_values_needed.append(
            "All requested names, marks, labels, dates and units exactly as the official source prints them."
        )
    return plan


async def _make_plan(qmap: QuestionMap, deadline: float) -> ResearchPlan:
    # Deterministic by design: avoid an extra LLM hop and avoid search-query drift.
    return _fallback_plan(qmap)


# ---------------------------------------------------------------------------
# Evidence store
# ---------------------------------------------------------------------------


class SourceBook:
    def __init__(self, qmap: QuestionMap, plan: ResearchPlan) -> None:
        self.qmap = qmap
        self.plan = plan
        self.rows: list[dict[str, Any]] = []
        self.searched: list[str] = []
        self.fetched: list[str] = []
        self.locked_hosts: list[str] = []
        self.pinned_event_urls: list[str] = []

    def _host_allowed(self, host: str) -> bool:
        if not self.qmap.hard_source_lock:
            return True
        clean = (host or "").lower().split(":", 1)[0].strip(".")
        if not self.locked_hosts:
            host_flat = re.sub(r"[^a-z0-9]", "", clean)
            for hint in self.qmap.named_sources:
                h = re.sub(r"[^a-z0-9]", "", hint.lower())
                if h and h in host_flat:
                    return True
            return not self.qmap.named_sources
        for allowed in self.locked_hosts:
            base = allowed.lower().split(":", 1)[0].strip(".")
            if clean == base or clean.endswith("." + base) or base.endswith("." + clean):
                return True
        return False

    def source_allowed(self, number: int) -> bool:
        row = self.row(number)
        if row is None or not self._host_allowed(str(row.get("host") or "")):
            return False
        if _event_source_rejected(self.qmap.question, row):
            return False
        # Once an exact event-specific route has been discovered, generic
        # competition pages with unknown active-event identity are no longer
        # admissible evidence. This prevents navigation/menu text from leaking
        # a neighboring discipline back into synthesis or citations.
        if self.has_canonical_event_source():
            route = _canonical_result_route_score(self.qmap.question, str(row.get("url") or ""))
            state = _event_match_state(
                self.qmap.question,
                str(row.get("url") or ""),
                str(row.get("title") or ""),
                str(row.get("text") or "")[:8000],
            )
            if route < 100 and state <= 0:
                return False
        return True

    def infer_source_lock(self) -> None:
        """Infer the named source's own host from discovery results."""
        if not self.qmap.hard_source_lock or not self.qmap.named_sources:
            return
        candidates: list[tuple[int, str]] = []
        for row in self.rows:
            host = str(row.get("host") or "").lower()
            if not host:
                continue
            host_flat = re.sub(r"[^a-z0-9]", "", host)
            owner = 0
            for hint in self.qmap.named_sources:
                h = re.sub(r"[^a-z0-9]", "", hint.lower())
                if h and h in host_flat:
                    owner += 100
            if owner <= 0:
                continue
            identity = _event_identity_score(
                self.qmap.question,
                str(row.get("url") or ""),
                str(row.get("title") or ""),
                str(row.get("text") or "")[:2600],
            )
            if identity <= -20:
                continue
            candidates.append((owner + identity + _int(row.get("authority"), 0), host))
        candidates.sort(reverse=True)
        if not candidates:
            return
        best = candidates[0][0]
        hosts: list[str] = []
        for score, host in candidates:
            if score < best - 18:
                continue
            if host not in hosts:
                hosts.append(host)
        self.locked_hosts = hosts[:3]
        self.refresh_event_pins()

    def refresh_event_pins(self) -> None:
        """Pin canonical event-specific result routes discovered in search/fetch."""
        candidates: list[tuple[int, str]] = []
        for row in self.rows:
            if _event_source_rejected(self.qmap.question, row):
                continue
            url = str(row.get("url") or "").strip()
            route = _canonical_result_route_score(self.qmap.question, url)
            if route < 100:
                continue
            candidates.append((route + _int(row.get("authority"), 0), url))
            for sibling in _stage_sibling_urls(url, self.qmap.question):
                candidates.append((route + 5, sibling))
        candidates.sort(reverse=True)
        pins: list[str] = []
        for _, url in candidates:
            clean = url.split("#", 1)[0]
            if clean and clean not in pins:
                pins.append(clean)
        self.pinned_event_urls = pins[:6]

    def has_canonical_event_source(self) -> bool:
        if self.pinned_event_urls:
            return True
        return any(
            _canonical_event_source(self.qmap.question, row)
            for row in self.rows
            if not _event_source_rejected(self.qmap.question, row)
        )

    def row(self, number: int) -> dict[str, Any] | None:
        if 1 <= number <= len(self.rows):
            return self.rows[number - 1]
        return None

    def add(
        self,
        receipt_id: str,
        result_id: str,
        text: str,
        title: str,
        url: str,
        shown: list[tuple[int, int]],
        origin: str,
    ) -> int:
        if len(self.rows) >= MAX_SOURCES:
            return 0
        if not receipt_id or not result_id or not text.strip():
            return 0
        for idx, old in enumerate(self.rows, 1):
            if old.get("receipt_id") == receipt_id and old.get("result_id") == result_id:
                spans = old.get("shown")
                if not isinstance(spans, list):
                    spans = []
                spans.extend(shown)
                old["shown"] = _merge_spans(spans, len(str(old.get("text") or "")))
                return idx
        body = text[:MAX_SOURCE_TEXT]
        row = {
            "receipt_id": receipt_id,
            "result_id": result_id,
            "text": body,
            "title": _cap(title, 220),
            "url": _cap(url, 600),
            "host": _host(url),
            "authority": _authority(url, title, self.qmap.named_sources + self.plan.preferred_domains),
            "shown": _merge_spans(shown, len(body)),
            "retained": [],
            "origin": origin,
        }
        self.rows.append(row)
        # Search snippets can reveal an exact event route before any page fetch.
        if _canonical_result_route_score(self.qmap.question, url) >= 100:
            self.refresh_event_pins()
        return len(self.rows)

    def retain(self, number: int, quote: str) -> bool:
        row = self.row(number)
        if row is None:
            return False
        body = str(row.get("text") or "")
        needle = (quote or "").strip()
        if len(needle) < 6:
            return False
        pos = body.find(needle)
        if pos < 0:
            pos = body.lower().find(needle.lower())
        if pos < 0:
            return False
        start = max(0, pos - QUOTE_CONTEXT)
        end = min(len(body), pos + len(needle) + QUOTE_CONTEXT)
        spans = row.get("retained")
        if not isinstance(spans, list):
            spans = []
        spans.append((start, end))
        row["retained"] = _merge_spans(spans, len(body))[:8]
        return True

    def relevance(self, number: int) -> int:
        row = self.row(number)
        if row is None:
            return -999
        hay = f"{row.get('title','')} {row.get('url','')} {str(row.get('text',''))[:5000]}".lower()
        score = _int(row.get("authority"), 0) * 5
        score += _event_identity_score(
            self.qmap.question,
            str(row.get("url") or ""),
            str(row.get("title") or ""),
            str(row.get("text") or "")[:2600],
        )
        for term in _token_terms(self.qmap.question)[:18]:
            if term.lower() in hay:
                score += 2
        for term in self.plan.focus_terms[:14]:
            if term.lower() in hay:
                score += 3
        if row.get("origin") == "fetch":
            score += 8
        return score

    def ranked(self) -> list[int]:
        numbers = [n for n in range(1, len(self.rows) + 1) if self.source_allowed(n)]
        numbers.sort(key=lambda n: (-self.relevance(n), n))
        return numbers

    def _snippet(self, number: int, focus: str, max_chars: int) -> str:
        row = self.row(number)
        if row is None:
            return ""
        body = str(row.get("text") or "")
        retained = row.get("retained")
        pieces: list[str] = []
        if isinstance(retained, list) and retained:
            for start, end in retained[:3]:
                pieces.append(body[_int(start) : _int(end)])
        if not pieces:
            shown = row.get("shown")
            if isinstance(shown, list):
                for start, end in shown[:3]:
                    pieces.append(body[_int(start) : _int(end)])
        if not pieces:
            for start, end in _best_windows(body, focus, min(max_chars, 4200), 2):
                pieces.append(body[start:end])
        if not pieces:
            pieces = [body[:max_chars]]
        return _cap("\n...\n".join(pieces), max_chars)

    def pack(self, max_chars: int = MAX_PACK_CHARS) -> str:
        focus = self.qmap.question + "\n" + " ".join(self.plan.focus_terms)
        blocks: list[str] = []
        spent = 0
        for number in self.ranked():
            row = self.row(number)
            if row is None:
                continue
            snippet = self._snippet(number, focus, 6500 if row.get("origin") == "fetch" else 2600)
            block = (
                f"[{number}] SOURCE\n"
                f"TITLE: {row.get('title','')}\n"
                f"URL: {row.get('url','')}\n"
                f"AUTHORITY: {row.get('authority',0)} ORIGIN: {row.get('origin','')}\n"
                f"EVENT_MATCH: {_event_match_state(self.qmap.question, str(row.get('url') or ''), str(row.get('title') or ''), str(row.get('text') or '')[:5000])} "
                f"CANONICAL_ROUTE_SCORE: {_canonical_result_route_score(self.qmap.question, str(row.get('url') or ''))}\n"
                f"TEXT:\n{snippet}\n"
            )
            if spent + len(block) > max_chars:
                continue
            blocks.append(block)
            spent += len(block)
            if spent >= max_chars - 1200:
                break
        return "\n\n".join(blocks)

    def citation(self, number: int) -> tuple[CitationRef | None, int]:
        row = self.row(number)
        if row is None or not self.source_allowed(number):
            return None, 0
        receipt = str(row.get("receipt_id") or "")
        result = str(row.get("result_id") or "")
        body = str(row.get("text") or "")
        if not receipt or not result or not body:
            return None, 0
        spans = row.get("retained") or row.get("shown") or []
        if not isinstance(spans, list):
            spans = []
        widened: list[tuple[int, int]] = []
        for raw in spans[:6]:
            try:
                start = max(0, int(raw[0]))
                end = min(len(body), int(raw[1]))
            except Exception:
                continue
            if end <= start:
                continue
            if end - start < MIN_SLICE:
                middle = (start + end) // 2
                start = max(0, middle - MIN_SLICE // 2)
                end = min(len(body), start + MIN_SLICE)
                start = max(0, end - MIN_SLICE)
            widened.append((start, end))
        if not widened:
            widened = [(0, min(len(body), 1800))]
        widened = _merge_spans(widened, len(body))
        total = sum(end - start for start, end in widened)
        if total > MAX_SLICE_PER_SOURCE:
            kept: list[tuple[int, int]] = []
            left = MAX_SLICE_PER_SOURCE
            for start, end in widened:
                if left <= 0:
                    break
                width = min(end - start, left)
                kept.append((start, start + width))
                left -= width
            widened = kept
            total = sum(end - start for start, end in widened)
        slices = [CitationSlice(start=start, end=end) for start, end in widened if end > start]
        if not slices:
            return None, 0
        return CitationRef(receipt_id=receipt, result_id=result, slices=slices), total


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


def _loosen(query: str) -> str:
    value = re.sub(r"\bsite:\S+", " ", query or "", flags=re.I)
    value = value.replace('"', " ").replace("'", " ")
    return _clean_space(value)


def _ingest_search(payload: Any, book: SourceBook) -> list[int]:
    if payload is None:
        return []
    _remember_budget(payload)
    receipt = str(getattr(payload, "receipt_id", "") or "")
    items = list(getattr(payload, "results", None) or [])
    if not receipt or not items:
        return []
    numbers: list[int] = []
    for item in items:
        result_id = getattr(item, "result_id", None)
        note = str(getattr(item, "note", None) or "")
        if not isinstance(result_id, str) or not result_id or not note.strip():
            continue
        title = str(getattr(item, "title", None) or "")
        url = str(getattr(item, "url", None) or "")
        number = book.add(
            receipt, result_id, note, title, url,
            [(0, min(len(note), SEARCH_PREVIEW))], "search"
        )
        if number and number not in numbers:
            numbers.append(number)
    return numbers


async def _parallel_search_call(
    queries: list[str],
    book: SourceBook,
    deadline: float,
    advanced: bool = False,
) -> list[int]:
    clean: list[str] = []
    for raw in queries:
        q = _clean_space(raw)
        if q and q.lower() not in [x.lower() for x in clean]:
            clean.append(_cap(q, 220))
        if len(clean) >= MAX_PLAN_QUERIES:
            break
    if not clean or _left(deadline) < 10.0:
        return []
    timeout = min(SEARCH_TIMEOUT if not advanced else SEARCH_RETRY_TIMEOUT, max(8.0, _left(deadline) - 5.0))
    try:
        payload = await search_web(
            clean,
            provider="parallel",
            num=SEARCH_RESULTS,
            timeout=timeout,
            provider_extra={
                "mode": "advanced" if advanced else "basic",
                "max_chars_total": 28000 if advanced else 22000,
                "excerpt_settings": {"max_chars_per_result": 4200 if advanced else 3200},
            },
        )
    except Exception:
        return []
    for q in clean:
        if q not in book.searched:
            book.searched.append(q)
    added = _ingest_search(payload, book)
    book.infer_source_lock()
    return added


async def _parallel_locked_repair(
    queries: list[str],
    book: SourceBook,
    deadline: float,
) -> list[int]:
    """Run one domain-filtered Parallel search after a hard source host is known."""
    if not book.qmap.hard_source_lock or not book.locked_hosts or _left(deadline) < 10.0:
        return []
    clean = [_clean_space(x) for x in queries if _clean_space(x)][:2]
    if not clean:
        return []
    timeout = min(SEARCH_TIMEOUT, max(8.0, _left(deadline) - 5.0))
    try:
        payload = await search_web(
            clean,
            provider="parallel",
            num=SEARCH_RESULTS,
            timeout=timeout,
            provider_extra={
                "mode": "basic",
                "max_chars_total": 24000,
                "source_policy": {"include_domains": list(book.locked_hosts)},
                "excerpt_settings": {"max_chars_per_result": 4200},
            },
        )
    except Exception:
        return []
    added = _ingest_search(payload, book)
    book.infer_source_lock()
    return added


async def _desearch_last_resort(query: str, book: SourceBook, deadline: float) -> list[int]:
    """One slow-provider fallback only after Parallel has failed completely."""
    q = _clean_space(query)
    if not q or _left(deadline) < 90.0:
        return []
    timeout = min(DESEARCH_FALLBACK_TIMEOUT, max(20.0, _left(deadline) - 7.0))
    try:
        payload = await search_web(
            q,
            provider="desearch",
            num=SEARCH_RESULTS,
            timeout=timeout,
        )
    except Exception:
        return []
    if q not in book.searched:
        book.searched.append(q)
    return _ingest_search(payload, book)


async def _search_many(queries: list[str], book: SourceBook, deadline: float) -> list[int]:
    """Parallel-first retrieval with one bounded quality escalation.

    Parallel's Harnyx adapter applies `num` as `max_results` at request time and
    supports excerpt limits, so this path avoids the long full-response latency
    observed with DeSearch. A single DeSearch call remains only as a last resort.
    """
    unique: list[str] = []
    for raw in queries:
        q = _clean_space(raw)
        if q and q.lower() not in [x.lower() for x in unique]:
            unique.append(q)
        if len(unique) >= MAX_PLAN_QUERIES:
            break
    if not unique:
        return []

    collected = await _parallel_search_call(unique, book, deadline, False)
    if collected:
        if book.qmap.hard_source_lock and book.locked_hosts and _left(deadline) > 24.0:
            locked = await _parallel_locked_repair(unique, book, deadline)
            for number in locked:
                if number not in collected:
                    collected.append(number)
        strongest = max(
            (_int((book.row(number) or {}).get("authority"), 0) for number in collected),
            default=0,
        )
        # Escalate quality only when source-bound questions did not surface an
        # authoritative source. Do not duplicate a good search just for volume.
        if strongest < 9 and _left(deadline) > 35.0:
            extra = await _parallel_search_call(unique[:2], book, deadline, True)
            for number in extra:
                if number not in collected:
                    collected.append(number)
        return collected

    # Parallel failed completely. One paid DeSearch fallback is preferable to
    # silently answering a source-bound question from model memory.
    fallback = await _desearch_last_resort(unique[0], book, deadline)
    return fallback

def _stage_sibling_urls(url: str, question: str) -> list[str]:
    """Derive obvious sibling result-stage URLs when a provider finds one stage.

    This is useful for sites whose result paths encode /semi-final/result and
    /final/result. It avoids another web search when the user explicitly asks
    to compare the two stages.
    """
    target = (url or "").strip()
    low_q = (question or "").lower()
    if not target or not ("final" in low_q and ("semi" in low_q or "semifinal" in low_q)):
        return []
    out: list[str] = []
    replacements = (
        ("/semi-final/result", "/final/result"),
        ("/semifinal/result", "/final/result"),
        ("/final/result", "/semi-final/result"),
    )
    for old_part, new_part in replacements:
        if old_part in target:
            candidate = target.replace(old_part, new_part, 1)
            if candidate != target and candidate not in out:
                out.append(candidate)
    return out


def _named_source_host_score(book: SourceBook, row: dict[str, Any]) -> int:
    """Prefer the named source's own host over pages that merely mention its name."""
    host_flat = re.sub(r"[^a-z0-9]", "", _host(str(row.get("url") or "")))
    score = 0
    for hint in book.qmap.named_sources:
        h = re.sub(r"[^a-z0-9]", "", hint.lower())
        if h and h in host_flat:
            score += 45
    return score


def _fetch_candidates(book: SourceBook, cap: int) -> list[str]:
    """Choose event-correct pages, pinning canonical stage routes first."""
    book.refresh_event_pins()
    out: list[str] = []
    seen: set[str] = set()

    # Hard priority: canonical event-specific routes discovered by Parallel.
    # This prevents generic competition/calendar pages from consuming the fetch
    # budget when an exact /men/1500-metres/... route is already available.
    for url in book.pinned_event_urls:
        for candidate in _canonical_stage_urls(url, book.qmap.question) or [url]:
            key = candidate.split("#", 1)[0]
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(candidate)
            if len(out) >= cap:
                return out

    ranked: list[tuple[int, int, int, int, int, str]] = []
    for number in book.ranked():
        row = book.row(number)
        if row is None or _event_source_rejected(book.qmap.question, row):
            continue
        url = str(row.get("url") or "").strip()
        if not url:
            continue
        identity = _event_identity_score(
            book.qmap.question,
            url,
            str(row.get("title") or ""),
            str(row.get("text") or "")[:4000],
        )
        route = _canonical_result_route_score(book.qmap.question, url)
        owner = _named_source_host_score(book, row)
        score = book.relevance(number)
        if identity <= -40 or route <= -80:
            continue
        # canonical route > active event match > source ownership > relevance.
        state = _event_match_state(
            book.qmap.question,
            url,
            str(row.get("title") or ""),
            str(row.get("text") or "")[:5000],
        )
        ranked.append((-route, -state, -owner, -identity, -score, url))
    ranked.sort()

    for _, _, _, _, _, url in ranked:
        candidates = _canonical_stage_urls(url, book.qmap.question)
        if not candidates:
            candidates = [url] + _stage_sibling_urls(url, book.qmap.question)
        for candidate in candidates:
            key = candidate.split("#", 1)[0]
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(candidate)
            if len(out) >= cap:
                return out
    return out


def _fetch_objective(book: SourceBook) -> str:
    parts = book.qmap.parts[:6] if book.qmap.parts else book.plan.must_answer[:6]
    focus = "; ".join(parts)
    if not focus:
        focus = " ".join(book.plan.focus_terms[:12])
    instruction = (
        "Extract exact source text needed to answer these requested facts. "
        "Preserve result-table rows, names, marks, dates, units, qualification "
        "labels/status codes, totals, and any rule explaining a discrepancy: " + focus
    )
    return _cap(_clean_space(instruction), 1300)


async def _fetch_one(url: str, book: SourceBook) -> int:
    target = (url or "").strip()
    if not target:
        return 0
    if target not in book.fetched:
        book.fetched.append(target)
    try:
        payload = await fetch_page(
            target,
            provider="parallel",
            timeout=FETCH_TIMEOUT,
            provider_extra={
                "objective": _fetch_objective(book),
                "max_chars_total": 30000,
                "excerpt_settings": {"max_chars_per_result": 9000},
                "full_content": True,
            },
        )
    except Exception:
        return 0
    if not getattr(payload, "results", None):
        return 0
    _remember_budget(payload)
    receipt = str(getattr(payload, "receipt_id", "") or "")
    items = list(getattr(payload, "results", None) or [])
    if not receipt or not items:
        return 0
    item = items[0]
    result_id = getattr(item, "result_id", None)
    note = str(getattr(item, "note", None) or "")
    if not isinstance(result_id, str) or not result_id or not note.strip():
        return 0
    title = str(getattr(item, "title", None) or target)
    final_url = str(getattr(item, "url", None) or target)

    probe = {
        "url": final_url,
        "title": title,
        "text": note,
        "host": _host(final_url),
    }
    # A same-domain page with an explicit different active event is never
    # admissible evidence. Example: a Tokyo-2025 competition page whose active
    # table is Men's 4x400 Relay cannot answer a Men's 1500m question merely
    # because "Men's 1500 Metres" appears in the navigation menu.
    if _event_source_rejected(book.qmap.question, probe):
        return 0

    shown: list[tuple[int, int]] = []
    if len(note) <= 8500:
        shown = [(0, len(note))]
    else:
        shown.append((0, min(len(note), FETCH_HEAD)))
        focus = book.qmap.question + "\n" + " ".join(book.plan.focus_terms)
        shown.extend(_best_windows(note, focus))
        shown = _merge_spans(shown, len(note))
    return book.add(receipt, result_id, note, title, final_url, shown, "fetch")


async def _fetch_many(urls: list[str], book: SourceBook, deadline: float) -> None:
    unique: list[str] = []
    for url in urls:
        if url and url not in unique:
            unique.append(url)
        if len(unique) >= FETCH_CAP:
            break
    if not unique or _left(deadline) < 12.0:
        return
    tasks = [asyncio.create_task(_fetch_one(url, book)) for url in unique]
    try:
        timeout = min(FETCH_TIMEOUT + 6.0, max(12.0, _left(deadline) - 5.0))
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            task.cancel()
        for task in done:
            try:
                task.result()
            except Exception:
                pass
    except Exception:
        for task in tasks:
            if not task.done():
                task.cancel()
        return


# ---------------------------------------------------------------------------
# Deterministic table signals
# ---------------------------------------------------------------------------


def _row_number_from_line(line: str) -> int:
    """Parse a ranking row from debug `ROW n` or markdown `|14\\.|...` syntax."""
    raw = line or ""
    m = re.match(r"^\s*ROW\s+(\d{1,3})\b", raw, flags=re.I)
    if m:
        return _int(m.group(1), 0)
    # Parallel markdown often escapes ordinal periods: `|14\\. |Josh KERR ...`.
    m = re.match(r"^\s*\|?\s*(\d{1,3})\s*(?:(?:\\?\.)|\))?\s*\|", raw)
    if m:
        return _int(m.group(1), 0)
    return 0


def _source_stage(row: dict[str, Any]) -> str:
    """Infer the result stage from the source URL/title without using page body claims."""
    hay = f"{row.get('title','')} {row.get('url','')}".lower()
    if re.search(r"(?:semi[- ]?final|semi-final/result|semifinal/result)", hay):
        return "semifinal"
    if re.search(r"(?:/final/result\b|\bfinal result\b)", hay) and "semi" not in hay:
        return "final"
    return "unknown"


def _section_role(line: str, current: str, source_stage: str = "unknown") -> str:
    """Map a visible heading to a semantic table scope.

    World Athletics result pages can contain start lists, official results, race
    analysis and other tables on one page.  Counting rows across those sections
    created the v10 61-row contradiction.  Non-result sections therefore get
    explicit roles that are never eligible for result-field counts.
    """
    low = _clean_space(line).lower().strip("*# _-")
    if not low:
        return current

    if "official startlist" in low or "official start list" in low or low == "startlist":
        return "startlist"
    if "race analysis" in low or "photo finish" in low or "start list" in low:
        return "analysis"
    if "official result" in low:
        return source_stage if source_stage in {"final", "semifinal"} else current

    if re.search(r"\bsemi[- ]?final\s*1\b", low):
        return "semifinal1"
    if re.search(r"\bsemi[- ]?final\s*2\b", low):
        return "semifinal2"

    # Some World Athletics semifinal pages label their two races as Heat 1/2.
    if source_stage == "semifinal" and re.search(r"\bheat\s*1\b", low):
        return "semifinal1"
    if source_stage == "semifinal" and re.search(r"\bheat\s*2\b", low):
        return "semifinal2"

    if re.search(r"\bsemi[- ]?final\b", low):
        return "semifinal"
    if re.search(r"\bfinal\b", low) and "semi" not in low:
        return "final"
    if re.search(r"\bheat(?:s|\s+\d+)?\b", low):
        return "heat"
    return current


def _is_final_result_source(row: dict[str, Any]) -> bool:
    return _source_stage(row) == "final"


def _record_from_entries(
    number: int,
    body: str,
    role: str,
    entries: list[tuple[int, int, int]],
) -> dict[str, Any] | None:
    if role not in {"final", "semifinal", "semifinal1", "semifinal2", "heat"}:
        return None
    if len(entries) < 4:
        return None
    nums = [value for value, _, _ in entries]
    # A true result table is one run.  If a page restarts numbering, the caller
    # flushes the previous run before this helper is invoked.
    unique: list[int] = []
    for value in nums:
        if value not in unique:
            unique.append(value)
    if len(unique) < 4:
        return None
    minimum = min(unique)
    maximum = max(unique)
    contiguous = minimum == 1 and unique == list(range(1, maximum + 1))
    first = entries[0][1]
    last = entries[-1][2]
    return {
        "source": number,
        "row_count": len(unique),
        "min_row": minimum,
        "max_row": maximum,
        "role": role,
        "is_final": role == "final",
        "contiguous": contiguous,
        "quote": body[first:last][:14000],
    }


def _table_records_for_row(number: int, row: dict[str, Any]) -> list[dict[str, Any]]:
    """Return section-scoped table runs from one fetched/search source.

    Important: never deduplicate all ROW labels across an entire page.  Result
    pages may contain several numbered tables.  A restart (e.g. 14 -> 1) or a
    section-role change closes the current run.
    """
    body = str(row.get("text") or "")
    if not body:
        return []

    source_stage = _source_stage(row)
    current = source_stage
    entries: list[tuple[int, int, int]] = []
    records: list[dict[str, Any]] = []
    offset = 0
    last_value = 0

    def flush() -> None:
        nonlocal entries, last_value
        record = _record_from_entries(number, body, current, entries)
        if record is not None:
            records.append(record)
        entries = []
        last_value = 0

    for raw_line in body.splitlines(keepends=True):
        new_role = _section_role(raw_line, current, source_stage)
        if new_role != current:
            flush()
            current = new_role

        value = _row_number_from_line(raw_line)
        if value > 0:
            # Numbering restart means a new table even if the surrounding page
            # did not expose a clean heading in extracted text.
            if entries and value <= last_value:
                flush()
            if current in {"final", "semifinal", "semifinal1", "semifinal2", "heat"}:
                entries.append((value, offset, offset + len(raw_line)))
                last_value = value
        offset += len(raw_line)
    flush()
    return records


def _record_specificity(item: dict[str, Any], qmap: QuestionMap, book: SourceBook) -> int:
    source = _int(item.get("source"), 0)
    row = book.row(source) or {}
    score = 0
    role = str(item.get("role") or "")
    url = str(row.get("url") or "").lower()
    if role == "final":
        score += 100
    if item.get("contiguous"):
        score += 60
    route = _canonical_result_route_score(qmap.question, url)
    state = _event_match_state(
        qmap.question,
        url,
        str(row.get("title") or ""),
        str(row.get("text") or "")[:8000],
    )
    score += max(-120, min(150, route))
    if state == 2:
        score += 100
    elif state == 1:
        score += 55
    elif state < 0:
        score -= 250
    if "/final/result" in url and role == "final":
        score += 120
    if "/semi-final/result" in url and role.startswith("semifinal"):
        score += 100
    if book.source_allowed(source):
        score += 50
    score += min(40, max(0, _int(row.get("authority"), 0)))
    # Do not reward a larger row count.  v10's failure came from treating a
    # broad page aggregate as "more complete" merely because it was larger.
    return score


def _best_table_record(qmap: QuestionMap, book: SourceBook, role: str) -> dict[str, Any] | None:
    records = _table_signal_records(qmap, book)
    candidates = [item for item in records if str(item.get("role") or "") == role]
    if role == "final":
        candidates = [item for item in candidates if item.get("contiguous") and _int(item.get("min_row"), 0) == 1]
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            _record_specificity(item, qmap, book),
            1 if item.get("contiguous") else 0,
            -_int(item.get("source"), 0),
        ),
        reverse=True,
    )
    return candidates[0]

def _table_signal_records(qmap: QuestionMap, book: SourceBook) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    canonical_exists = book.has_canonical_event_source()
    for number in book.ranked()[:16]:
        row = book.row(number)
        if row is None or _event_source_rejected(qmap.question, row):
            continue
        if canonical_exists:
            route = _canonical_result_route_score(qmap.question, str(row.get("url") or ""))
            state = _event_match_state(
                qmap.question,
                str(row.get("url") or ""),
                str(row.get("title") or ""),
                str(row.get("text") or "")[:8000],
            )
            # Once event-specific routes exist, generic unknown event pages must
            # not contribute deterministic row counts.
            if route < 100 and state <= 0:
                continue
        signals.extend(_table_records_for_row(number, row))
    return signals


def _table_signal_text(qmap: QuestionMap, book: SourceBook) -> str:
    lines: list[str] = []
    for item in _table_signal_records(qmap, book)[:10]:
        role = str(item.get("role") or "result-table")
        lines.append(
            f"[{item['source']}] {role}: detected {item['row_count']} listed result rows "
            f"(positions {item['min_row']} through {item['max_row']})."
        )
    return "\n".join(lines)


def _final_count_from_record(record: dict[str, Any] | None) -> int:
    if not isinstance(record, dict):
        return 0
    count = _int(record.get("row_count"), 0)
    minimum = _int(record.get("min_row"), 0)
    maximum = _int(record.get("max_row"), 0)
    if record.get("contiguous") and minimum == 1 and maximum == count:
        return maximum
    return 0


def _final_count_claim_value(text: str) -> int:
    """Extract a claimed final-field/list count, not a finishing position."""
    value = _clean_space(text)
    low = value.lower()
    if "final" not in low:
        return 0
    patterns = (
        r"(?:official\s+)?final(?:\s+result)?(?:\s+list|\s+field)?[^.;]{0,70}?(?:contains|has|lists|shows|includes|comprises|with)\s+(?:exactly\s+)?(\d{1,3})\s+(?:(?:explicitly|officially|listed|qualified|total)\s+){0,4}(?:athletes|rows|entries|finalists)",
        r"(?:contains|has|lists|shows|includes|comprises)\s+(?:exactly\s+)?(\d{1,3})\s+(?:(?:explicitly|officially|listed|qualified|total)\s+){0,4}(?:athletes|rows|entries|finalists)[^.;]{0,70}?\bfinal\b",
        r"(\d{1,3})\s+(?:(?:explicitly|officially|listed|qualified|total)\s+){0,4}(?:athletes|rows|entries|finalists)[^.;]{0,70}?(?:official\s+)?final(?:\s+result)?(?:\s+list|\s+field)?",
    )
    for pattern in patterns:
        m = re.search(pattern, value, flags=re.I)
        if m:
            return _int(m.group(1), 0)
    return 0


def _is_final_count_fact(item: dict[str, Any]) -> bool:
    claim = str(item.get("claim") or "")
    requirement = str(item.get("requirement") or "").lower()
    slot = str(item.get("slot") or "").lower()
    if slot == "final_result_count":
        return True
    if "deterministic count" in requirement:
        return True
    return _final_count_claim_value(claim) > 0


def _sanitize_grid_conflicts(grid: dict[str, Any], qmap: QuestionMap, book: SourceBook) -> None:
    """Resolve same-slot fact conflicts before synthesis.

    For an explicit final-list count, a contiguous section-scoped final table is
    authoritative over broad page aggregates or LLM-derived competing counts.
    """
    low = qmap.question.lower()
    if "final" not in low or not ("how many" in low or "number of" in low or qmap.computed):
        return
    record = _best_table_record(qmap, book, "final")
    authoritative = _final_count_from_record(record)
    if authoritative <= 0:
        return

    facts = grid.get("facts")
    if not isinstance(facts, list):
        facts = []
        grid["facts"] = facts

    cleaned: list[Any] = []
    for item in facts:
        if not isinstance(item, dict):
            cleaned.append(item)
            continue
        if _is_final_count_fact(item):
            value = _final_count_claim_value(str(item.get("claim") or ""))
            if value and value != authoritative:
                continue
            # Replace all same-slot count claims with one deterministic claim.
            continue
        cleaned.append(item)
    facts[:] = cleaned

    source = _int(record.get("source"), 0) if isinstance(record, dict) else 0
    quote = str(record.get("quote") or "") if isinstance(record, dict) else ""
    if source > 0 and len(quote) >= 20:
        facts.append({
            "claim": f"The official final result list contains {authoritative} explicitly listed rows.",
            "source": source,
            "quote": quote,
            "requirement": "deterministic count from the official final result section",
            "slot": "final_result_count",
            "confidence": 100,
        })
        book.retain(source, quote)


def _augment_grid_table_count(grid: dict[str, Any], qmap: QuestionMap, book: SourceBook) -> None:
    low = qmap.question.lower()
    if not (qmap.computed or "how many" in low or "number of" in low):
        return

    if "final" in low:
        chosen = _best_table_record(qmap, book, "final")
    else:
        records = [item for item in _table_signal_records(qmap, book) if item.get("contiguous")]
        chosen = max(records, key=lambda item: _record_specificity(item, qmap, book), default=None)
    if chosen is None:
        return

    source = _int(chosen.get("source"), 0)
    count = _final_count_from_record(chosen) if chosen.get("is_final") else _int(chosen.get("row_count"), 0)
    quote = str(chosen.get("quote") or "")
    if source <= 0 or count <= 0 or len(quote) < 20:
        return

    facts = grid.get("facts")
    if not isinstance(facts, list):
        facts = []
        grid["facts"] = facts
    facts.append({
        "claim": (
            f"The official final result list contains {count} explicitly listed rows."
            if chosen.get("is_final") and "final" in low
            else f"The result table contains {count} explicitly listed rows."
        ),
        "source": source,
        "quote": quote,
        "requirement": (
            "deterministic count from the official final result section"
            if chosen.get("is_final") and "final" in low
            else "deterministic count from the actual listed result rows"
        ),
        "slot": "final_result_count" if chosen.get("is_final") and "final" in low else "table_count",
        "confidence": 100,
    })
    book.retain(source, quote)
    _sanitize_grid_conflicts(grid, qmap, book)


def _sanitize_answer_conflicts(answer: str, qmap: QuestionMap, book: SourceBook) -> str:
    """Drop final-count statements that contradict the scoped result table."""
    value = (answer or "").strip()
    if not value:
        return value
    record = _best_table_record(qmap, book, "final") if "final" in qmap.question.lower() else None
    authoritative = _final_count_from_record(record)
    if authoritative <= 0:
        return value

    multiline = "\n" in value
    segments = value.splitlines() if multiline else re.split(r"(?<=[.!?])\s+", value)
    kept: list[str] = []
    for segment in segments:
        claimed = _final_count_claim_value(segment)
        if claimed > 0 and claimed != authoritative:
            continue
        kept.append(segment)
    result = ("\n" if multiline else " ").join(x for x in kept if x.strip()).strip()
    return result or value


def _answer_has_final_count_conflict(answer: str, qmap: QuestionMap, book: SourceBook) -> bool:
    record = _best_table_record(qmap, book, "final") if "final" in qmap.question.lower() else None
    authoritative = _final_count_from_record(record)
    if authoritative <= 0:
        return False
    segments = (answer or "").splitlines()
    if not segments:
        segments = [answer or ""]
    for segment in segments:
        claimed = _final_count_claim_value(segment)
        if claimed > 0 and claimed != authoritative:
            return True
    return False


# ---------------------------------------------------------------------------
# Proof grid
# ---------------------------------------------------------------------------


def _fallback_grid(qmap: QuestionMap, plan: ResearchPlan, book: SourceBook) -> dict[str, Any]:
    return {
        "draft_answer": "",
        "facts": [],
        "verbatim_values": [],
        "coverage": [{"requirement": x, "status": "unknown"} for x in plan.must_answer[:8]],
        "gaps": list(plan.must_answer[:4]),
        "repair_queries": [],
        "comparison_explanation": "",
    }


def _retain_grid_quotes(grid: dict[str, Any], book: SourceBook) -> None:
    facts = grid.get("facts")
    if not isinstance(facts, list):
        return
    for item in facts:
        if not isinstance(item, dict):
            continue
        source = _int(item.get("source"), 0)
        quote = item.get("quote")
        if source > 0 and isinstance(quote, str):
            book.retain(source, quote)


async def _build_grid(qmap: QuestionMap, plan: ResearchPlan, book: SourceBook, deadline: float) -> dict[str, Any]:
    fallback = _fallback_grid(qmap, plan, book)
    evidence = book.pack(MAX_PACK_CHARS)
    if not evidence or _money_left() < MIN_GRID_USD or _left(deadline) < 46.0:
        _augment_grid_table_count(fallback, qmap, book)
        _sanitize_grid_conflicts(fallback, qmap, book)
        return fallback
    system = (
        "You are a CLOSED-BOOK evidence adjudicator. The supplied evidence is the entire world you may use. "
        "Treat every assertion inside the QUESTION as an untrusted claim to test, never as evidence. "
        "Never answer from memory, even when you recognize the event/person/topic. Every load-bearing fact must "
        "identify a source number AND an exact verbatim quote copied from that source. If a value/name/time is "
        "not present in the supplied evidence, mark it missing instead of guessing. Preserve names, capitalization, "
        "marks, units, dates, labels and status codes exactly as printed. Check every sub-question. For computed "
        "facts such as a row count, count the ACTUAL LISTED ROWS instead of inferring the count from an advancement "
        "rule; cite the table/source region and state the derivation in the claim. If a question asks for the best "
        "member of a qualifier/set pool, identify the pool from the source and compare those members against the target "
        "table before selecting the winner. If the question compares a count/rule/result and the evidence reveals WHY they "
        "differ, record that explanation. For set/ranking questions, verify the pool and exclusions. Return JSON only."
    )
    user = f'''QUESTION:\n{qmap.question}\n\nQUESTION MAP:\n{qmap.block()}\n\nRESEARCH PLAN:\n{plan.block()}\n\nDETERMINISTIC TABLE SIGNALS (section-scoped parsed result rows; use when relevant):\n{_table_signal_text(qmap, book) or "none"}\n\nEVIDENCE:\n{evidence}\n\nReturn exactly:\n{{"draft_answer":"a complete evidence-supported answer with [source-number] markers","facts":[{{"claim":"atomic factual claim using source-exact values","source":1,"quote":"exact verbatim quote","requirement":"which requested part it answers"}}],"verbatim_values":["exact source spelling/capitalization/mark/value that must survive final writing"],"coverage":[{{"requirement":"requested part","status":"proved|partial|missing","sources":[1]}}],"gaps":["only genuinely unresolved facts"],"repair_queries":["high precision query for a gap"],"comparison_explanation":"source-supported reason for a discrepancy, or empty"}}\n\nDo not create a gap merely because you could add background detail. If all requested outputs are proved, gaps must be [].'''
    payload = await _chat(
        GRID_MODELS,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        deadline,
        4200,
        GRID_TIMEOUT,
        0.0,
    )
    data = _json_object(_llm_text(payload))
    if data is None:
        data = fallback
    _augment_grid_table_count(data, qmap, book)
    _sanitize_grid_conflicts(data, qmap, book)
    _retain_grid_quotes(data, book)
    return data


def _repair_queries(grid: dict[str, Any]) -> list[str]:
    raw = grid.get("repair_queries")
    return _uniq_text(raw, MAX_REPAIR_QUERIES, 430)


def _grid_draft(grid: dict[str, Any]) -> str:
    value = grid.get("draft_answer")
    return value.strip() if isinstance(value, str) else ""


def _grid_verbatim(grid: dict[str, Any]) -> list[str]:
    return _uniq_text(grid.get("verbatim_values"), 40, 180)


def _grid_has_real_gaps(grid: dict[str, Any]) -> bool:
    gaps = grid.get("gaps")
    if not isinstance(gaps, list):
        return False
    return any(isinstance(x, str) and _clean_space(x) for x in gaps)


# ---------------------------------------------------------------------------
# Grounding firewall
# ---------------------------------------------------------------------------

_NUM_TOKEN_RE = re.compile(r"(?<![A-Za-z])\d+(?::\d{1,2}(?:\.\d+)?|\.\d+|,\d{3})*(?:%|st|nd|rd|th)?", re.I)
_NAME_TOKEN_RE = re.compile(r"\b(?:[A-Z][A-Za-zÀ-ÖØ-öø-ÿ.'-]{2,}|[A-Z]{2,})(?:\s+(?:[A-Z][A-Za-zÀ-ÖØ-öø-ÿ.'-]{2,}|[A-Z]{2,})){1,3}\b")
_COMMON_NAME_PHRASES = {
    "World Athletics", "Official Result", "Official Results", "Race Analysis",
    "Photo Finish", "Final Result", "Semifinal Result", "Reference Answer",
}


def _norm_quote(text: str) -> str:
    value = (text or "").replace("\u00a0", " ")
    value = value.replace("–", "-").replace("—", "-")
    return _clean_space(value).lower()


def _quote_supported(quote: str, body: str) -> bool:
    if not quote or not body:
        return False
    if quote in body or quote.lower() in body.lower():
        return True
    nq = _norm_quote(quote)
    nb = _norm_quote(body)
    if len(nq) >= 6 and nq in nb:
        return True
    words = nq.split()
    for width in (12, 10, 8):
        if len(words) < width:
            continue
        for i in range(0, len(words) - width + 1):
            if " ".join(words[i:i + width]) in nb:
                return True
    return False


def _verified_facts(grid: dict[str, Any], book: SourceBook) -> list[dict[str, Any]]:
    """Keep only facts whose quoted evidence literally exists in the cited source."""
    raw = grid.get("facts")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw[:30]:
        if not isinstance(item, dict):
            continue
        source = _int(item.get("source"), 0)
        claim = _clean_space(str(item.get("claim") or ""))
        quote = str(item.get("quote") or "").strip()
        row = book.row(source)
        if row is None or not claim or len(quote) < 6:
            continue
        body = str(row.get("text") or "")
        if not _quote_supported(quote, body):
            continue
        clean = dict(item)
        clean["source"] = source
        clean["claim"] = claim
        clean["quote"] = quote
        out.append(clean)
        book.retain(source, quote)
    return out


def _fact_basis(question: str, facts: list[dict[str, Any]], book: SourceBook) -> str:
    pieces = [question]
    for item in facts:
        pieces.append(str(item.get("claim") or ""))
        pieces.append(str(item.get("quote") or ""))
        row = book.row(_int(item.get("source"), 0))
        if row is not None:
            pieces.append(str(row.get("title") or ""))
    return "\n".join(pieces)


def _critical_numbers(text: str) -> list[str]:
    clean = _CITE_RE.sub("", text or "")
    out: list[str] = []
    for match in _NUM_TOKEN_RE.finditer(clean):
        token = match.group(0)
        if token not in out:
            out.append(token)
    return out


def _critical_names(text: str) -> list[str]:
    out: list[str] = []
    for match in _NAME_TOKEN_RE.finditer(text or ""):
        token = _clean_space(match.group(0))
        if token in _COMMON_NAME_PHRASES:
            continue
        if token not in out:
            out.append(token)
    return out


def _token_present(token: str, basis: str) -> bool:
    if not token:
        return True
    low_basis = (basis or "").lower()
    if token.lower() in low_basis:
        return True
    # Preserve the existing policy that values explicitly stated by the user may
    # appear in comparisons, while recognizing digit/word equivalents. This is
    # important for prompts that contrast an asserted "twelve" with an actual 14.
    number_words = {
        0: "zero", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
        6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten",
        11: "eleven", 12: "twelve", 13: "thirteen", 14: "fourteen",
        15: "fifteen", 16: "sixteen", 17: "seventeen", 18: "eighteen",
        19: "nineteen", 20: "twenty",
    }
    if re.fullmatch(r"\d{1,2}", token):
        value = _int(token, -1)
        word = number_words.get(value)
        if word and re.search(rf"\b{re.escape(word)}\b", low_basis):
            return True
    return False


def _coverage_signature(facts: list[dict[str, Any]]) -> list[str]:
    """Critical values that a final answer should preserve from verified facts."""
    out: list[str] = []
    for item in facts:
        claim = str(item.get("claim") or "")
        for token in _critical_numbers(claim):
            if token not in out:
                out.append(token)
        # Exact multi-word names are especially important in pairwise judging.
        for token in _critical_names(claim):
            if token not in out:
                out.append(token)
    return out[:36]


def _grounded_answer(answer: str, question: str, facts: list[dict[str, Any]], book: SourceBook) -> bool:
    """Reject memory substitutions and unsupported precise values/names."""
    if not _usable_answer(answer, question):
        return False
    basis = _fact_basis(question, facts, book)

    # Any precise numeric token in the answer must exist in the question or
    # verified evidence/facts. This catches remembered times, prices, years, etc.
    for token in _critical_numbers(answer):
        if not _token_present(token, basis):
            return False

    # Any multi-word proper name introduced by the writer must be visible in
    # the question or verified evidence. This catches entity substitution such
    # as answering about a famous athlete from a different championship.
    for token in _critical_names(answer):
        if not _token_present(token, basis):
            return False

    # If verified facts contain distinctive values, the answer must preserve a
    # meaningful share of them. A fluent answer that ignores the proof grid is
    # worse than a mechanical evidence-only fallback.
    required = _coverage_signature(facts)
    if len(required) >= 4:
        hits = sum(1 for token in required if _token_present(token, answer))
        if hits < max(2, int(len(required) * 0.45)):
            return False
    return True



def _grounded_in_book(answer: str, question: str, book: SourceBook) -> bool:
    """Allow a complete evidence-only answer even when proof-grid JSON is sparse.

    Every precise number/name still has to exist in retrieved evidence. This keeps
    the proof grid from becoming a single point of failure.
    """
    if not _usable_answer(answer, question):
        return False
    pieces = [question]
    for number in book.ranked()[:12]:
        row = book.row(number)
        if row is None:
            continue
        pieces.append(str(row.get("title") or ""))
        pieces.append(str(row.get("text") or ""))
    basis = "\n".join(pieces)
    for token in _critical_numbers(answer):
        if not _token_present(token, basis):
            return False
    for token in _critical_names(answer):
        if not _token_present(token, basis):
            return False
    return True


def _answer_part_coverage(answer: str, qmap: QuestionMap) -> int:
    """Heuristic coverage guard for multi-part answers.

    A numbered multi-part question should not collapse to one verified sentence
    merely because one deterministic fact survived a proof-grid parse.
    """
    if not qmap.parts:
        return 1 if _usable_answer(answer, qmap.question) else 0
    low = (answer or "").lower()
    numbered = sum(1 for i in range(1, min(len(qmap.parts), 9) + 1) if f"({i})" in low or f"{i}." in low)
    if numbered:
        return numbered
    # Non-numbered prose: approximate coverage by distinct lines/sentences.
    chunks = [x.strip() for x in re.split(r"[\n]+|(?<=[.!?])\s+", answer or "") if len(x.strip()) >= 12]
    return min(len(qmap.parts), len(chunks))


def _verified_fact_answer(question: str, facts: list[dict[str, Any]], book: SourceBook) -> str:
    """Zero-memory fallback built only from quote-verified atomic claims."""
    lines: list[str] = []
    seen: set[str] = set()
    for item in facts[:14]:
        claim = _clean_space(str(item.get("claim") or ""))
        source = _int(item.get("source"), 0)
        if not claim:
            continue
        key = claim.lower()
        if key in seen:
            continue
        seen.add(key)
        marker = f" [{source}]" if 1 <= source <= len(book.rows) else ""
        lines.append(claim + marker)
    text = "\n".join(lines)
    return text if _usable_answer(text, question) else ""


def _grid_for_writer(grid: dict[str, Any], facts: list[dict[str, Any]]) -> dict[str, Any]:
    """Strip unverified fact claims before final synthesis."""
    clean = dict(grid)
    clean["facts"] = facts
    # Draft answer is untrusted free-form text; the writer receives verified
    # atomic facts instead of being anchored to a possibly hallucinated draft.
    clean["draft_answer"] = ""
    return clean


# ---------------------------------------------------------------------------
# Mandatory multipart coverage compiler
# ---------------------------------------------------------------------------


def _required_part_count(qmap: QuestionMap) -> int:
    return len(qmap.parts) if qmap.parts else 0


def _source_markers(text: str) -> list[int]:
    out: list[int] = []
    for raw in re.findall(r"\[(\d{1,3})\]", text or ""):
        value = _int(raw, 0)
        if value > 0 and value not in out:
            out.append(value)
    return out


def _part_object_valid(item: dict[str, Any], index: int, book: SourceBook, question: str) -> bool:
    if _int(item.get("index"), 0) != index:
        return False
    answer = _clean_answer(str(item.get("answer") or ""))
    if not _usable_answer(answer, question):
        return False
    sources = item.get("sources")
    if not isinstance(sources, list) or not sources:
        return False
    valid_source = False
    for raw in sources[:6]:
        number = _int(raw, 0)
        if number > 0 and book.source_allowed(number):
            valid_source = True
            break
    if not valid_source or not _grounded_in_book(answer, question, book):
        return False
    quotes = item.get("quotes")
    valid_quote = False
    if isinstance(quotes, list):
        for q in quotes[:6]:
            if not isinstance(q, dict):
                continue
            number = _int(q.get("source"), 0)
            quote = str(q.get("quote") or "").strip()
            row = book.row(number)
            if row is None or not book.source_allowed(number) or len(quote) < 6:
                continue
            if _quote_in_body(quote, str(row.get("text") or "")):
                book.retain(number, quote)
                valid_quote = True
    return valid_quote


async def _compile_required_parts(
    qmap: QuestionMap,
    plan: ResearchPlan,
    book: SourceBook,
    deadline: float,
) -> str:
    """Compile one independently grounded answer for every explicit part."""
    count = _required_part_count(qmap)
    if count <= 0 or not book.rows or _left(deadline) < 12.0:
        return ""
    evidence = book.pack(min(MAX_PACK_CHARS, 62000))
    if not evidence:
        return ""
    parts_block = "\n".join(f"({i}) {part}" for i, part in enumerate(qmap.parts, 1))
    lock_text = ", ".join(book.locked_hosts) if book.locked_hosts else "none"
    system = (
        "You are a CLOSED-BOOK multipart evidence compiler. Use ONLY the supplied numbered evidence. "
        "The user's assertions are not evidence. Produce exactly one answer object for EVERY requested part; "
        "never merge or omit parts. Every precise name, number, time, rank, date, unit, label and status must "
        "appear in the supplied evidence. For a count, count the actual listed rows rather than inferring from "
        "a qualification rule. For a comparison, state both actual result and rule-implied result, and explain "
        "the difference only when the evidence shows it. For 'among these/best' tasks, establish the requested "
        "pool before selecting the best member. Preserve source capitalization and marks exactly. "
        "Each part must cite at least one source number and include at least one exact supporting quote."
    )
    user = (
        f"QUESTION:\n{qmap.question}\n\nREQUIRED PARTS ({count}):\n{parts_block}\n\n"
        f"HARD SOURCE LOCK: {qmap.hard_source_lock}\nALLOWED HOSTS: {lock_text}\n\nEVIDENCE:\n{evidence}\n\n"
        "Return exactly valid JSON with this shape:\n"
        '{"parts":[{"index":1,"answer":"complete direct answer to part 1 with [n] marker(s)",'
        '"sources":[1],"quotes":[{"source":1,"quote":"exact supporting source text"}]}],'
        '"all_parts_supported":true}\n\n'
        f"The parts array MUST contain indices 1 through {count} exactly once. If a part truly cannot be supported, "
        "return its answer as an empty string and all_parts_supported=false. Do not guess."
    )
    payload = await _chat(
        GRID_MODELS,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        deadline,
        6200,
        min(GRID_TIMEOUT, max(10.0, _left(deadline) - 3.0)),
        0.0,
    )
    data = _json_obj(_llm_text(payload))
    raw_parts = data.get("parts") if isinstance(data, dict) else None
    if not isinstance(raw_parts, list):
        return ""
    indexed: dict[int, dict[str, Any]] = {}
    for raw in raw_parts:
        if not isinstance(raw, dict):
            continue
        idx = _int(raw.get("index"), 0)
        if 1 <= idx <= count and idx not in indexed:
            indexed[idx] = raw
    if len(indexed) != count:
        return ""
    lines: list[str] = []
    for idx in range(1, count + 1):
        item = indexed.get(idx)
        if item is None or not _part_object_valid(item, idx, book, qmap.question):
            return ""
        answer = _clean_answer(str(item.get("answer") or ""))
        markers = [n for n in _source_markers(answer) if book.source_allowed(n)]
        if not markers:
            sources = [_int(x, 0) for x in item.get("sources", []) if _int(x, 0) > 0 and book.source_allowed(_int(x, 0))]
            if sources:
                answer = answer.rstrip(" .") + f" [{sources[0]}]"
        lines.append(f"({idx}) {answer}")
    compiled = "\n".join(lines)
    if _answer_part_coverage(compiled, qmap) < count:
        return ""
    if not _grounded_in_book(compiled, qmap.question, book):
        return ""
    return compiled


# ---------------------------------------------------------------------------
# Answer quality, citations, source-verbatim restoration
# ---------------------------------------------------------------------------


_CITE_RE = re.compile(r"\[(\d{1,3})\]")


def _usable_answer(text: str, question: str = "") -> bool:
    value = (text or "").strip()
    if len(value) < 3:
        return False
    low = value.lower()
    bad = (
        "unable to answer",
        "i could not complete",
        "best-effort answer unavailable",
        "insufficient information",
        "i cannot determine",
    )
    if any(item in low for item in bad):
        return False
    if question:
        q = _clean_space(question)
        v = _clean_space(value)
        # Reject accidental prompt echo as an answer.
        if len(q) > 80 and (v == q or v.startswith(q[: min(300, len(q))])):
            return False
    return True


def _marker_numbers(text: str, maximum: int) -> list[int]:
    out: list[int] = []
    for match in _CITE_RE.finditer(text or ""):
        number = _int(match.group(1), 0)
        if 1 <= number <= maximum and number not in out:
            out.append(number)
    return out


def _normalize_markers(text: str, maximum: int) -> str:
    def repl(match: re.Match[str]) -> str:
        number = _int(match.group(1), 0)
        return f"[{number}]" if 1 <= number <= maximum else ""
    return re.sub(r"\[(\d{1,4})\]", repl, text or "")


def _citations(text: str, book: SourceBook) -> list[CitationRef]:
    refs: list[CitationRef] = []
    spent = 0
    for number in _marker_numbers(text, len(book.rows)):
        if len(refs) >= MAX_CITATIONS:
            break
        ref, cost = book.citation(number)
        if ref is None:
            continue
        if spent + cost > MAX_EVIDENCE_CHARS:
            continue
        refs.append(ref)
        spent += cost
    return refs


def _retain_output_quotes(data: dict[str, Any], book: SourceBook) -> None:
    proof = data.get("proof_quotes")
    if not isinstance(proof, list):
        return
    for item in proof:
        if not isinstance(item, dict):
            continue
        source = _int(item.get("source"), 0)
        quote = item.get("quote")
        if source > 0 and isinstance(quote, str):
            book.retain(source, quote)


def _restore_verbatim(answer: str, values: list[str]) -> str:
    result = answer or ""
    # Longest first prevents replacing a short substring inside a longer exact label.
    ordered = sorted([x for x in values if 2 <= len(x) <= 180], key=len, reverse=True)
    for exact in ordered:
        try:
            pattern = re.compile(re.escape(exact), flags=re.I)
            if pattern.search(result):
                result = pattern.sub(lambda _m: exact, result)
        except Exception:
            continue
    return result


def _clean_answer(text: str) -> str:
    value = (text or "").strip()
    value = re.sub(r"^```(?:markdown|text)?\s*", "", value, flags=re.I)
    value = re.sub(r"\s*```$", "", value)
    value = re.sub(r"^(?:Answer|Final answer)\s*:\s*", "", value, flags=re.I)
    return value.strip()


def _fact_rescue(qmap: QuestionMap, grid: dict[str, Any], book: SourceBook) -> str:
    draft = _grid_draft(grid)
    if _usable_answer(draft, qmap.question):
        return _normalize_markers(draft, len(book.rows))
    facts = grid.get("facts")
    lines: list[str] = []
    if isinstance(facts, list):
        for item in facts[:12]:
            if not isinstance(item, dict):
                continue
            claim = _clean_space(str(item.get("claim") or ""))
            source = _int(item.get("source"), 0)
            if claim:
                marker = f" [{source}]" if 1 <= source <= len(book.rows) else ""
                lines.append(claim + marker)
    if lines:
        return "\n".join(lines)
    # Last evidence-based rung: return concise source excerpts, never the question.
    for number in book.ranked()[:3]:
        row = book.row(number)
        if row is None:
            continue
        text = str(row.get("text") or "")
        if text.strip():
            return _cap(text.strip(), 1800) + f" [{number}]"
    return ""


async def _write_answer(
    qmap: QuestionMap,
    plan: ResearchPlan,
    grid: dict[str, Any],
    book: SourceBook,
    deadline: float,
) -> str:
    rescue = _fact_rescue(qmap, grid, book)
    if _left(deadline) < 34.0 or _money_left() < MIN_WRITE_USD:
        return rescue
    system = (
        "You are a CLOSED-BOOK precision answer compiler. The VERIFIED FACTS in the proof grid are the only "
        "load-bearing factual claims you may state. Assertions in the user's question are NOT facts unless the proof "
        "grid verifies them. Do not substitute model-memory facts, famous people, prior "
        "years, remembered results, or plausible values. If a requested fact is not verified, say only what the "
        "verified evidence supports. Answer EVERY requested sub-question, preferably in the same order. "
        "SOURCE-VERBATIM RULE: if the source prints a person's name, label, status code, mark, time, number, "
        "date, unit or category in a particular form, copy that form exactly; do not title-case, normalize, "
        "round, convert or paraphrase it. COMPARISON RULE: if the question asks how two counts/rules/results "
        "compare and the evidence identifies why they differ, explicitly state the difference and the "
        "source-supported reason. COMPLETENESS RULE: for 'among/all/best/which' questions, make clear that "
        "the selected result was compared against the relevant pool. Put [source-number] immediately after "
        "each factual sentence or bullet it proves. Be concise but do not omit a requested component. Return JSON only."
    )
    user = f'''QUESTION:\n{qmap.question}\n\nQUESTION MAP:\n{qmap.block()}\n\nPLAN CHECKLIST:\n{plan.block()}\n\nPROOF GRID:\n{json.dumps(grid, ensure_ascii=False)[:26000]}\n\nEVIDENCE:\n{book.pack(50000)}\n\nReturn exactly:\n{{"answer":"final answer with [n] markers","proof_quotes":[{{"source":1,"quote":"exact decisive source quote used by the answer"}}]}}\n\nBefore returning, silently verify: all requested parts answered; exact source spellings/marks preserved; discrepancy explained when evidence supports it; no unsupported background added.'''
    payload = await _chat(
        WRITE_MODELS,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        deadline,
        4600,
        WRITE_TIMEOUT,
        0.0,
    )
    data = _json_object(_llm_text(payload))
    if data is None:
        return rescue
    _retain_output_quotes(data, book)
    answer = data.get("answer")
    if not isinstance(answer, str):
        return rescue
    answer = _normalize_markers(_clean_answer(answer), len(book.rows))
    answer = _restore_verbatim(answer, _grid_verbatim(grid))
    if not _usable_answer(answer, qmap.question):
        return rescue
    return answer


async def _review_answer(
    answer: str,
    qmap: QuestionMap,
    plan: ResearchPlan,
    grid: dict[str, Any],
    book: SourceBook,
    deadline: float,
) -> str:
    if not _usable_answer(answer, qmap.question):
        return answer
    if _left(deadline) < 27.0 or _money_left() < MIN_REVIEW_USD:
        return answer
    system = (
        "You are a strict CLOSED-BOOK final-answer reviewer. You may revise only with supplied verified evidence. "
        "Never replace a source-backed value with remembered knowledge from another year/event/entity. "
        "Judge the CURRENT ANSWER the way a strict pairwise benchmark judge would. Prefer the answer that is more "
        "complete, source-exact, directly responsive, and better supported. Check: (1) every explicit/numbered "
        "sub-question is answered; (2) no answer value came from an unverified claim in the QUESTION; (3) names, labels, marks, times, dates, "
        "units and status codes match source spelling/capitalization exactly; (4) a requested comparison says "
        "both the numerical/logical difference and, when evidenced, why it exists; (5) row counts come from actual "
        "listed rows rather than an expected rule count; (6) ranking/set answers demonstrate the relevant comparison "
        "pool; (7) every factual sentence has a valid [n] marker. "
        "Do not add uncited model-memory facts. Return JSON only."
    )
    user = f'''QUESTION:\n{qmap.question}\n\nCHECKLIST:\n{plan.block()}\n\nCURRENT ANSWER:\n{answer}\n\nPROOF GRID:\n{json.dumps(grid, ensure_ascii=False)[:22000]}\n\nDECISIVE EVIDENCE:\n{book.pack(42000)}\n\nReturn exactly:\n{{"answer":"corrected answer, or the original unchanged if already optimal","proof_quotes":[{{"source":1,"quote":"exact source quote"}}]}}'''
    payload = await _chat(
        ("deepseek-ai/DeepSeek-V3.2-TEE", "google/gemma-4-31B-turbo-TEE", "Qwen/Qwen3.6-27B-TEE"),
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        deadline,
        3600,
        REVIEW_TIMEOUT,
        0.0,
    )
    data = _json_object(_llm_text(payload))
    if data is None:
        return answer
    _retain_output_quotes(data, book)
    revised = data.get("answer")
    if not isinstance(revised, str):
        return answer
    revised = _normalize_markers(_clean_answer(revised), len(book.rows))
    revised = _restore_verbatim(revised, _grid_verbatim(grid))
    if _usable_answer(revised, qmap.question):
        return revised
    return answer


# ---------------------------------------------------------------------------
# Structured output
# ---------------------------------------------------------------------------


def _schema_kind(schema: Any) -> str:
    if not isinstance(schema, dict):
        return ""
    kind = schema.get("type")
    if isinstance(kind, str):
        return kind
    for key in ("anyOf", "oneOf", "allOf"):
        value = schema.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    found = _schema_kind(item)
                    if found and found != "null":
                        return found
    return ""


def _schema_branch(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {}
    if isinstance(schema.get("type"), str):
        return schema
    for key in ("anyOf", "oneOf", "allOf"):
        value = schema.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and _schema_kind(item) != "null":
                    return item
    return schema


def _shape_ok(value: Any, schema: Any, depth: int = 0) -> bool:
    if depth > 6 or not isinstance(schema, dict):
        return True
    branch = _schema_branch(schema)
    enum = branch.get("enum")
    if isinstance(enum, list) and enum and value not in enum:
        return False
    kind = _schema_kind(branch)
    if kind == "object":
        if not isinstance(value, dict):
            return False
        props = branch.get("properties") or {}
        required = branch.get("required") or []
        if isinstance(required, list):
            for key in required:
                if key not in value:
                    return False
        if isinstance(props, dict):
            for key, sub in props.items():
                if key in value and isinstance(sub, dict) and not _shape_ok(value[key], sub, depth + 1):
                    return False
        return True
    if kind == "array":
        if not isinstance(value, list):
            return False
        sub = branch.get("items") or {}
        return all(_shape_ok(item, sub, depth + 1) for item in value[:40])
    if kind == "string":
        return isinstance(value, str)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "null":
        return value is None
    return True


def _strip_citations(text: str) -> str:
    return _clean_space(_CITE_RE.sub("", text or ""))


def _value_lines(text: str) -> list[str]:
    clean = _strip_citations(text)
    parts = re.split(r"[\n;]+", clean)
    out: list[str] = []
    for raw in parts:
        item = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", raw).strip()
        if item:
            out.append(item[:500])
        if len(out) >= 24:
            break
    return out


def _coerce(text: str, schema: Any, depth: int = 0) -> Any:
    branch = _schema_branch(schema)
    if depth > 6:
        return _strip_citations(text)[:500]
    enum = branch.get("enum")
    if isinstance(enum, list) and enum:
        low = text.lower()
        for option in enum:
            if isinstance(option, str) and option.lower() in low:
                return option
        return enum[0]
    kind = _schema_kind(branch)
    if kind == "object":
        props = branch.get("properties") or {}
        required = branch.get("required") or list(props.keys()) if isinstance(props, dict) else []
        out: dict[str, Any] = {}
        if isinstance(required, list):
            for key in required:
                sub = props.get(key) if isinstance(props, dict) else {}
                out[str(key)] = _coerce(text, sub if isinstance(sub, dict) else {}, depth + 1)
        return out
    if kind == "array":
        sub = branch.get("items") or {}
        return [_coerce(item, sub, depth + 1) for item in _value_lines(text)[:12]]
    clean = _strip_citations(text)
    if kind in ("integer", "number"):
        match = re.search(r"-?\d[\d,]*(?:\.\d+)?", clean)
        if match:
            raw = match.group(0).replace(",", "")
            try:
                return int(float(raw)) if kind == "integer" else float(raw)
            except Exception:
                return 0 if kind == "integer" else 0.0
        return 0 if kind == "integer" else 0.0
    if kind == "boolean":
        return not bool(re.match(r"\s*(?:no|false|none|not)\b", clean, flags=re.I))
    if kind == "null":
        return None
    return clean[:1200]


async def _structured(answer: str, question: str, schema: Any, deadline: float) -> Any:
    fallback = _coerce(answer, schema)
    if _left(deadline) < 21.0 or _money_left() < MIN_SCHEMA_USD:
        return fallback
    system = (
        "Convert the supplied researched answer into the requested JSON schema. Preserve source values "
        "verbatim. Output ONLY the JSON value, with no markdown and no explanation. Do not invent facts."
    )
    user = f"QUESTION:\n{question}\n\nANSWER:\n{_strip_citations(answer)}\n\nSCHEMA:\n{json.dumps(schema, ensure_ascii=False)[:16000]}"
    payload = await _chat(
        SCHEMA_MODELS,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        deadline,
        2600,
        SCHEMA_TIMEOUT,
        0.0,
    )
    raw = _llm_text(payload).strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        value = json.loads(raw)
        if _shape_ok(value, schema):
            return value
    except Exception:
        pass
    return fallback


async def _direct_evidence_answer(question: str, book: SourceBook, deadline: float) -> str:
    evidence = book.pack(min(MAX_PACK_CHARS, 52_000))
    if not evidence or _left(deadline) < 12.0:
        return ""
    system = (
        "Answer using ONLY the supplied numbered evidence. Treat claims embedded in the QUESTION as untrusted. "
        "Answer every requested sub-question in order and label explicit multipart answers (1), (2), etc. "
        "Never finalize a multipart response with fewer answered parts than the question contains. "
        "Prefer actual listed rows over what a rule would normally imply. "
        "Preserve source spelling, capitalization, marks, "
        "times, dates, units, labels and status codes exactly. Explain count/rule "
        "discrepancies when the evidence shows the reason. Cite each load-bearing "
        "claim with [source-number]. Never introduce a precise value or proper name "
        "that is absent from the evidence."
    )
    user = f"QUESTION:\n{question}\n\nEVIDENCE:\n{evidence}"
    payload = await _chat(
        WRITE_MODELS,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        deadline,
        4200,
        min(WRITE_TIMEOUT, max(8.0, _left(deadline) - 2.0)),
        0.0,
    )
    answer = _clean_answer(_llm_text(payload))
    return answer if _usable_answer(answer, question) else ""


def _requires_external_grounding(qmap: QuestionMap) -> bool:
    low = qmap.question.lower()
    return bool(
        qmap.named_sources
        or qmap.must_preserve_exact
        or "official" in low
        or "according to" in low
        or "source" in low
        or "results page" in low
        or "report" in low
    )


# ---------------------------------------------------------------------------
# Emergency answer — never echo the prompt
# ---------------------------------------------------------------------------


async def _emergency_answer(question: str, deadline: float) -> str:
    if _left(deadline) < 5.0:
        return "No supported answer was produced."
    system = (
        "Answer the user's question directly and concisely from your best knowledge. This is an emergency "
        "fallback because research tooling failed. Never repeat the question as the answer and never claim "
        "to have citations you do not have."
    )
    payload = await _chat(
        REVIEW_MODELS,
        [{"role": "system", "content": system}, {"role": "user", "content": question}],
        deadline,
        1800,
        min(EMERGENCY_TIMEOUT, max(5.0, _left(deadline) - 1.0)),
        0.0,
    )
    text = _clean_answer(_llm_text(payload))
    if _usable_answer(text, question):
        return text
    return "No supported answer was produced."


# ---------------------------------------------------------------------------
# Main controller
# ---------------------------------------------------------------------------


async def _solve(query: Query, question: str) -> Response:
    started = monotonic()
    deadline = started + WALL_SECONDS
    try:
        await _load_tooling()
    except Exception:
        pass

    qmap = QuestionMap(question, query.output_schema)
    try:
        plan = await _make_plan(qmap, deadline)
    except Exception:
        plan = _fallback_plan(qmap)
    book = SourceBook(qmap, plan)

    # One high-value retrieval wave. Keep the exact event/year/source terms from
    # the question; do not let an LLM planner drift to a remembered benchmark.
    try:
        await _search_many(plan.queries, book, deadline)
        book.infer_source_lock()
    except Exception:
        pass
    try:
        targets = _fetch_candidates(book, FETCH_CAP)
        await _fetch_many(targets, book, deadline)
        book.infer_source_lock()
    except Exception:
        pass

    # Build one central grid and immediately discard any fact whose cited quote
    # cannot be found literally in the source. This is the v7 grounding wall.
    try:
        grid = await _build_grid(qmap, plan, book, deadline)
    except Exception:
        grid = _fallback_grid(qmap, plan, book)
    facts = _verified_facts(grid, book)
    clean_grid = _grid_for_writer(grid, facts)
    best_answer = _verified_fact_answer(question, facts, book)
    protected_compiled = False

    # The evidence compiler is now an independent answer path, not merely a
    # no-facts emergency. V8 proved that one bad deterministic fact could suppress
    # a much richer evidence answer. For multi-part/source-bound tasks, always try
    # one direct closed-book synthesis from the retrieved evidence.
    coverage_target = max(2, len(qmap.parts) if qmap.parts else 2)
    sparse_fact_path = len(facts) < coverage_target or _answer_part_coverage(best_answer, qmap) < min(coverage_target, 4)
    if book.rows and (sparse_fact_path or _grid_has_real_gaps(grid)):
        try:
            direct = await _direct_evidence_answer(question, book, deadline)
            if _grounded_in_book(direct, question, book):
                if _answer_part_coverage(direct, qmap) >= _answer_part_coverage(best_answer, qmap):
                    best_answer = direct
        except Exception:
            pass

    # Mandatory multipart compiler: if the prompt explicitly contains N
    # numbered parts, compile and validate all N independently.
    if qmap.parts and book.rows and _left(deadline) > 18.0:
        try:
            compiled = await _compile_required_parts(qmap, plan, book, deadline)
            if compiled and _answer_part_coverage(compiled, qmap) == len(qmap.parts):
                compiled = _sanitize_answer_conflicts(compiled, qmap, book)
                if not _answer_has_final_count_conflict(compiled, qmap, book):
                    best_answer = compiled
                    protected_compiled = bool(qmap.hard_source_lock)
        except Exception:
            pass

    # A single targeted repair is allowed only when too little quote-verified
    # evidence survived. Do not spend another full wave merely to polish style.
    needs_repair = (
        len(facts) < coverage_target
        or _grid_has_real_gaps(grid)
        or (bool(qmap.parts) and _answer_part_coverage(best_answer, qmap) < len(qmap.parts))
    )
    if needs_repair and not protected_compiled and _elapsed(started) < REPAIR_LATEST_ELAPSED:
        repair = _repair_queries(grid)[:1]
        if repair:
            try:
                await _search_many(repair, book, deadline)
                await _fetch_many(_fetch_candidates(book, min(2, FETCH_CAP)), book, deadline)
                newer = await _build_grid(qmap, plan, book, deadline)
                newer_facts = _verified_facts(newer, book)
                if len(newer_facts) > len(facts):
                    grid = newer
                    facts = newer_facts
                    clean_grid = _grid_for_writer(grid, facts)
                    mechanical = _verified_fact_answer(question, facts, book)
                    if _answer_part_coverage(mechanical, qmap) > _answer_part_coverage(best_answer, qmap):
                        best_answer = mechanical
                # Whether or not the grid improved, newly retrieved evidence can
                # improve a complete direct answer.
                if book.rows:
                    direct = await _direct_evidence_answer(question, book, deadline)
                    if _grounded_in_book(direct, question, book) and _answer_part_coverage(direct, qmap) >= _answer_part_coverage(best_answer, qmap):
                        best_answer = direct
            except Exception:
                pass

    # Re-run the strict coverage compiler after any repair wave.
    if qmap.parts and book.rows and _answer_part_coverage(best_answer, qmap) < len(qmap.parts) and _left(deadline) > 14.0:
        try:
            compiled = await _compile_required_parts(qmap, plan, book, deadline)
            if compiled and _answer_part_coverage(compiled, qmap) == len(qmap.parts):
                compiled = _sanitize_answer_conflicts(compiled, qmap, book)
                if not _answer_has_final_count_conflict(compiled, qmap, book):
                    best_answer = compiled
                    protected_compiled = protected_compiled or bool(qmap.hard_source_lock)
        except Exception:
            pass

    # Final synthesis is permitted only from quote-verified facts. Its output
    # must pass a deterministic grounding firewall or it is discarded.
    if facts and not protected_compiled and _elapsed(started) < FORCE_COMMIT_ELAPSED:
        try:
            written = await _write_answer(qmap, plan, clean_grid, book, deadline)
            writer_grounded = _grounded_answer(written, question, facts, book) or _grounded_in_book(written, question, book)
            if writer_grounded and _answer_part_coverage(written, qmap) >= _answer_part_coverage(best_answer, qmap):
                best_answer = written
        except Exception:
            pass

    # One short review only if there is ample time. The revised answer must pass
    # the same deterministic grounding firewall before it can replace the prior.
    current_grounded = _grounded_answer(best_answer, question, facts, book) if facts else False
    current_grounded = current_grounded or _grounded_in_book(best_answer, question, book)
    if not protected_compiled and _elapsed(started) < REVIEW_LATEST_ELAPSED and current_grounded:
        try:
            reviewed = await _review_answer(best_answer, qmap, plan, clean_grid, book, deadline)
            reviewed_grounded = (_grounded_answer(reviewed, question, facts, book) if facts else False) or _grounded_in_book(reviewed, question, book)
            if reviewed_grounded and _answer_part_coverage(reviewed, qmap) >= _answer_part_coverage(best_answer, qmap):
                best_answer = reviewed
        except Exception:
            pass

    # If free-form synthesis drifted outside both the verified-fact basis and
    # retrieved evidence, fall back mechanically. Do not discard a complete,
    # evidence-grounded answer merely because proof-grid JSON was sparse.
    fact_ok = _grounded_answer(best_answer, question, facts, book) if facts else False
    book_ok = _grounded_in_book(best_answer, question, book)
    if facts and not (fact_ok or book_ok):
        best_answer = _verified_fact_answer(question, facts, book)

    # Hard completeness gate for explicit multipart questions.
    if qmap.parts and _answer_part_coverage(best_answer, qmap) < len(qmap.parts) and book.rows and _left(deadline) > 10.0:
        try:
            compiled = await _compile_required_parts(qmap, plan, book, deadline)
            if compiled:
                compiled = _sanitize_answer_conflicts(compiled, qmap, book)
                if not _answer_has_final_count_conflict(compiled, qmap, book):
                    best_answer = compiled
        except Exception:
            pass

    best_answer = _sanitize_answer_conflicts(best_answer, qmap, book)
    best_answer = _restore_verbatim(best_answer, _grid_verbatim(grid))
    best_answer = _sanitize_answer_conflicts(best_answer, qmap, book)
    best_answer = _normalize_markers(_clean_answer(best_answer), len(book.rows))

    # Last deterministic invariant: a final-field count cannot contradict the
    # section-scoped official result table.  If it somehow does, prefer a fresh
    # coverage compilation rather than emitting mutually exclusive facts.
    if _answer_has_final_count_conflict(best_answer, qmap, book) and qmap.parts and _left(deadline) > 8.0:
        try:
            repaired = await _compile_required_parts(qmap, plan, book, deadline)
            repaired = _sanitize_answer_conflicts(repaired, qmap, book)
            if repaired and not _answer_has_final_count_conflict(repaired, qmap, book):
                best_answer = repaired
        except Exception:
            pass

    if not _usable_answer(best_answer, question):
        # Evidence excerpts are safer than model-memory invention.
        best_answer = _fact_rescue(qmap, clean_grid, book)
    if not _usable_answer(best_answer, question) and not _requires_external_grounding(qmap):
        try:
            emergency = await _emergency_answer(question, deadline)
            emergency_ok = _grounded_answer(emergency, question, facts, book) if facts else _usable_answer(emergency, question)
            if emergency_ok:
                best_answer = emergency
        except Exception:
            pass
    if not _usable_answer(best_answer, question):
        best_answer = "No supported answer was produced."

    if len(best_answer) > MAX_ANSWER_CHARS:
        best_answer = best_answer[: MAX_ANSWER_CHARS - 4].rstrip() + " …"

    try:
        refs = _citations(best_answer, book)
    except Exception:
        refs = []

    if query.output_schema is not None:
        try:
            output = await _structured(best_answer, question, query.output_schema, deadline)
        except Exception:
            output = _coerce(best_answer, query.output_schema)
        return Response(output=output, citations=refs or None)

    if qmap.strict_output:
        first = best_answer.splitlines()[0].strip() if best_answer else ""
        first = _CITE_RE.sub("", first)
        best_answer = _clean_space(first)
    return Response(text=best_answer, citations=refs or None)


@entrypoint("query")
async def query(query: Query) -> Response:
    question = (query.text or "").strip()
    if not question:
        if query.output_schema is not None:
            return Response(output=_coerce("", query.output_schema))
        return Response(text="No question provided.")
    try:
        return await _solve(query, question)
    except Exception:
        # A final containment boundary. Source-bound research questions are safer
        # unanswered than confidently hallucinated from model memory.
        low = question.lower()
        source_bound = bool(
            "official" in low
            or "according to" in low
            or "results page" in low
            or "report" in low
            or "exactly as" in low
        )
        if source_bound:
            answer = "No supported answer was produced."
        else:
            deadline = monotonic() + 30.0
            try:
                answer = await _emergency_answer(question, deadline)
            except Exception:
                answer = "No supported answer was produced."
        if query.output_schema is not None:
            return Response(output=_coerce(answer, query.output_schema))
        return Response(text=answer)
