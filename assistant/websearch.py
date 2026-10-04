"""Web search run by us, against POST /api/web, rather than by the chat endpoint.

The chat endpoint's `:online` suffix searches on the app's behalf, which sounds simpler
and cost three rounds of debugging: the query is derived server-side from the latest user
message, so every instruction left in that message became part of what was searched for,
and the sources are never handed back - measured, a request that demonstrably searched
(17,763 prompt tokens against 2,755 without) returned none of the documented
x_nanogpt_search_* fields.

Calling the search endpoint ourselves fixes both. We choose the query, and the results
come back as titles, URLs and page text, which is what a Sources list needs. It also
works with any model, rather than only the OpenAI ones whose hosted tools the Responses
endpoint accepts.

https://docs.nano-gpt.com/api-reference/endpoint/web-search
"""

import html
import json
import re

import requests

# How much page text reaches the model per search, across all results together.
#
# This used to be a flat 1,200 per result, which punished a long good page for the sake
# of seven short ones and spent the same allowance on a result that had nothing to say.
# The budget is the old cap times the largest result count, so the total is unchanged -
# what changes is that it is shared out rather than handed round. A search set to three
# results now gets 3,200 characters each instead of 1,200.
TOTAL_CONTEXT_CHARS = 9600
# A search query, not a prompt: past a few hundred characters providers match nothing.
MAX_QUERY_CHARS = 400
# What the context-size setting means now that it no longer maps to a provider field.
RESULT_COUNTS = {"low": 3, "medium": 5, "high": 8}
DEFAULT_RESULTS = 5

# The providers worth offering, best first - the order also decides what the settings
# dropdown falls back to. Measured over three queries (a Windows error code, a Python
# traceback, a passage of Japanese lyrics), asking each for eight results:
#
#   perplexity  10/16 primary sources, ~1,300 clean chars per result, 1.7s   <- default
#   valyu        5/16, clean prose, tightest set on the error query, 1.6s
#   tavily       4/16, clean but thin: a 142-char median snippet
#   kagi         9/16 good links, ~150 chars of text to reason from, 2.0s
#   linkup       2/16, and the only provider that drifts language - it answered an
#                English Windows question with Vietnamese, Russian and Danish pages
#   brave        5/16, fastest at 0.9s, ships <strong> tags and HTML entities
#   sofya        6/16, the most bytes and the worst of them: whole raw HTML documents,
#                so the snippet cap can land entirely inside a <head>
#
# Exa was removed rather than ranked. It returns no page text at all - title, URL, date
# and author only - at every one of its six depths, `deep` included, so the model got a
# list of links it could not read.
PROVIDERS = ["perplexity", "valyu", "tavily", "kagi", "linkup", "brave", "sofya"]
DEFAULT_PROVIDER = "perplexity"


class SearchError(Exception):
    pass


def endpoint(api_url):
    """The /api/web URL that belongs with a configured chat-completions URL."""
    marker = "/api/"
    index = (api_url or "").find(marker)
    if index == -1:
        return "https://nano-gpt.com/api/web"
    return api_url[:index + len(marker)] + "web"


def query_from(messages):
    """The search query for a turn.

    A turn may carry an explicit `search_query`, built from the question and the material
    it is about. That is much better than reading it back out of the prompt: the prompt
    contains scaffolding the model needs and a search engine does not, and measured
    against the live API that scaffolding is ruinous. Searching
    `Can you identify this song? --- Text visible in the image: <lyrics>` returned two
    on-target results out of five, the rest being generic "identify any song" tools;
    the same lyrics without the label returned five out of five.

    Otherwise - follow-ups, clipboard operations - the user's own words are already the
    query, and only the separators the prompts use need taking out.
    """
    for message in reversed(messages or []):
        if message.get("role") != "user":
            continue
        explicit = (message.get("search_query") or "").strip()
        if explicit:
            return _trim(explicit)
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(part.get("text") or "" for part in content
                               if isinstance(part, dict))
        text = " ".join((content or "").replace("---", " ").split())
        if not text:
            continue
        return _trim(text)
    return ""


def _trim(text):
    text = " ".join(text.split())
    if len(text) <= MAX_QUERY_CHARS:
        return text
    # Cut on a word boundary; a query ending mid-word searches for a word nobody wrote.
    return text[:MAX_QUERY_CHARS].rsplit(" ", 1)[0]


# What the crafting model gets to read. More than a query needs, less than a prompt.
MATERIAL_CHARS = 2000


def material_from(messages):
    """The latest user turn in full, for writing a query from rather than searching."""
    for message in reversed(messages or []):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = "\n".join(part.get("text") or "" for part in content
                                 if isinstance(part, dict))
        text = (content or "").strip()
        if text:
            return text[:MATERIAL_CHARS]
    return ""


def result_count(config):
    size = (getattr(config, "web_search_context_size", "") or "").lower()
    return RESULT_COUNTS.get(size, DEFAULT_RESULTS)


def params(query, config):
    """Request body for /api/web, from the same settings the chat path used."""
    body = {"query": query}

    provider = (getattr(config, "web_search_provider", "") or "").lower()
    # A provider that has since been withdrawn can still be sitting in a saved config,
    # and sending it gets an error or silence rather than a search.
    if provider not in PROVIDERS:
        provider = DEFAULT_PROVIDER
    body["provider"] = provider

    depth = (getattr(config, "web_search_mode", "") or "standard").lower()
    if depth not in ("standard", "deep"):
        depth = "standard"
    body["depth"] = depth

    if provider == "kagi":
        # Kagi's other two tiers, "web" and "news", are enrichment rather than search.
        # They now fail outright: HTTP 504 "Search returned no usable results" on every
        # query tried, 8 attempts out of 8. Only "search" does the job, so it is not a
        # setting any more.
        body["kagiSource"] = "search"
    elif provider == "valyu":
        scope = (getattr(config, "web_search_valyu_search_type", "") or "").lower()
        if scope:
            body["searchType"] = scope

    body["maxResults"] = result_count(config)
    return body


_DROPPED_ELEMENTS = re.compile(
    r"<(script|style|head|nav|footer|noscript)\b.*?</\1\s*>", re.I | re.S)
_TAG = re.compile(r"<[^>]{0,400}?>", re.S)
_MD_LINK = re.compile(r"\[([^\]]{0,200}?)\]\((?:https?|/)[^)]{0,400}?\)")


def clean(text):
    """Readable text out of whatever the provider called `content`.

    Providers disagree wildly about what belongs in that field. Some send prose, some
    send prose with `<strong>` scattered through it and `&quot;` left unescaped, and one
    sends entire raw HTML documents - measured, 11,865 characters of one, whose first
    1,200 were `<!DOCTYPE html>` and the contents of `<head>`. Truncating that fed the
    model meta tags and threw the article away, so the markup comes out first.
    """
    text = str(text or "")
    if "<" in text:
        text = _DROPPED_ELEMENTS.sub(" ", text)
        text = _TAG.sub(" ", text)
    # After the tags, never before: `&lt;div&gt;` in prose is text, not a tag.
    text = html.unescape(text)
    # A markdown link spends its characters on a URL nobody will follow from here.
    text = _MD_LINK.sub(r"\1", text)
    return " ".join(text.split())


def _share(lengths, budget):
    """Split `budget` across `lengths`, giving away what the short ones do not use.

    Equal shares, then anything a result does not need goes back into the pot for the
    ones that do. A page of 150 characters keeps all 150; the long ones divide the rest.
    """
    out = [0] * len(lengths)
    pending = set(range(len(lengths)))
    while pending:
        share = budget // len(pending)
        if share <= 0:
            break
        fitted = [i for i in pending if lengths[i] <= share]
        if not fitted:
            for i in pending:
                out[i] = share
            break
        for i in fitted:
            out[i] = lengths[i]
            budget -= lengths[i]
            pending.discard(i)
    return out


def _cut(text, limit):
    """`text` trimmed to `limit`, on a word boundary so it does not end mid-word."""
    if len(text) <= limit:
        return text
    kept = text[:limit]
    spaced = kept.rsplit(" ", 1)[0]
    # CJK text can run for a long way without a space; a hard cut beats returning almost
    # nothing.
    return (spaced if len(spaced) >= limit * 0.6 else kept).rstrip()


def _entries(payload):
    """The result list, wherever this provider happens to put it."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("results", "data", "sources", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def parse(body, limit=DEFAULT_RESULTS, budget=TOTAL_CONTEXT_CHARS):
    """Normalise a provider's payload to [{title, url, content}].

    Cleaning happens before the budget is shared out, so the allowance is spent on text
    rather than on markup that was going to be discarded anyway.
    """
    found = []
    seen = set()
    for entry in _entries(body.get("data") if isinstance(body, dict) else body):
        if not isinstance(entry, dict):
            continue
        url = entry.get("url") or entry.get("link")
        if not url or url in seen:
            continue
        seen.add(url)
        text = entry.get("content") or entry.get("snippet") or entry.get("text") or ""
        found.append({"title": clean(entry.get("title") or url) or url.strip(),
                      "url": url.strip(),
                      "content": clean(text)})
        if len(found) >= limit:
            break

    for result, allowance in zip(found, _share([len(r["content"]) for r in found],
                                               budget)):
        result["content"] = _cut(result["content"], allowance)
    return found


def run(query, config, session=None, timeout=None):
    """Search, returning [{title, url, content}]. Raises SearchError on failure."""
    if not query:
        return []

    post = (session or requests).post
    try:
        response = post(
            endpoint(config.api_url),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {config.api_key}"},
            json=params(query, config),
            timeout=timeout or getattr(config, "request_timeout", 60))
    except requests.RequestException as e:
        raise SearchError(str(e))

    if response.status_code != 200:
        detail = ""
        try:
            body = response.json()
            detail = (body.get("error") if isinstance(body, dict) else None) or ""
            if isinstance(detail, dict):
                detail = detail.get("message") or json.dumps(detail)
        except ValueError:
            detail = (response.text or "")[:200]
        raise SearchError(f"HTTP {response.status_code}: {detail}".strip())

    try:
        body = response.json()
    except ValueError:
        raise SearchError("search endpoint returned a non-JSON response")
    return parse(body, result_count(config))


def as_context(query, results):
    """The results as a block for the model, in the shape it already sees them in.

    Deliberately the same `## title / URL: …` layout the chat endpoint injects
    server-side, so the model is reading something familiar.
    """
    if not results:
        return ""
    blocks = [f'Web search results for "{query}":', ""]
    for result in results:
        blocks.append(f"## {result['title']}")
        blocks.append(f"URL: {result['url']}")
        if result["content"]:
            blocks.append(result["content"])
        blocks.append("")
    return "\n".join(blocks).strip()
