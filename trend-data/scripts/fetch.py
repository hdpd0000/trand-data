#!/usr/bin/env python3
"""Fetch papers (arXiv + OpenAlex) and news (RSS) into data/<field>.json.

Standard library only. Every network step is isolated: a failure is recorded in
data/meta.json and never aborts the whole run. Nothing here is generated text:
every excerpt is the first sentence of the source's own abstract/description, and
the sub-field tag comes from the keyword rules in config.json (untagged items stay
untagged so a later review can label them).
"""
import html
import json
import math
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UA = "personal-trend-dashboard/1.0 (mailto:none@example.invalid)"
ATOM = "{http://www.w3.org/2005/Atom}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"
NOW = datetime.now(timezone.utc)
STATUS = {}  # source name -> "ok..." | error text


def get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def safe(name, fn, default):
    try:
        out = fn()
        STATUS[name] = "ok"
        return out
    except Exception as e:  # noqa: BLE001 - record and continue
        STATUS[name] = f"{type(e).__name__}: {e}"[:200]
        return default


# ---------- text helpers ----------
def clean(s):
    """Strip HTML, entities and feed boilerplate; collapse whitespace."""
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\s*The post\b.*?\bappeared first on\b.*$", "", s)
    s = re.sub(r"\s*\[?(…|\.\.\.)\]?\s*$", "", s)
    return s.strip()


def trim(text, n):
    if len(text) <= n:
        return text
    return text[:n].rsplit(" ", 1)[0] + "…"


def first_sentence(text, maxlen=220):
    text = (text or "").strip()
    if not text:
        return ""
    m = re.search(r"(?<=[.!?])\s+(?=[A-Z0-9\"“(\[])", text)
    s = text[:m.start()] if m else text
    return trim(s, maxlen)


# ---------- keyword rules (sub-field tagging) ----------
def compile_kw(kw):
    k = kw.lower()
    prefix = k.endswith("*")
    k = k.rstrip("*")
    return re.compile(r"(?<![a-z0-9])" + re.escape(k) + ("" if prefix else r"(?![a-z0-9])"))


def prepare_subfields(subfields):
    for sf in subfields:
        sf["_rx"] = [compile_kw(k) for k in sf["keywords"]]
    return subfields


def classify(title, body, subfields):
    """Each keyword scores 2 if in the title, else 1 if in the body.
    The highest sub-field wins (ties go to the earlier one); below 2 = untagged."""
    t, b = (title or "").lower(), (body or "").lower()
    best, best_s = None, 0
    for sf in subfields:
        s = 0
        for rx in sf["_rx"]:
            if rx.search(t):
                s += 2
            elif rx.search(b):
                s += 1
        if s > best_s:
            best, best_s = sf["id"], s
    return best if best_s >= 2 else None


# ---------- arXiv ----------
def arxiv_query(cfg):
    cats = " OR ".join(f"cat:{c}" for c in cfg["categories"])
    kws = " OR ".join(f'all:"{k}"' for k in cfg["keywords"])
    q = f"({cats}) AND ({kws})"
    params = urllib.parse.urlencode({
        "search_query": q, "sortBy": "submittedDate", "sortOrder": "descending",
        "start": 0, "max_results": cfg.get("max_results", 100)})
    return "https://export.arxiv.org/api/query?" + params


def parse_arxiv(xml_bytes, keywords):
    root = ET.fromstring(xml_bytes)
    papers = []
    for e in root.findall(f"{ATOM}entry"):
        url = e.findtext(f"{ATOM}id", "").strip()
        aid = re.sub(r"^https?://arxiv.org/abs/", "", url)
        aid = re.sub(r"v\d+$", "", aid)
        title = " ".join(e.findtext(f"{ATOM}title", "").split())
        summary = " ".join(e.findtext(f"{ATOM}summary", "").split())
        authors = [a.findtext(f"{ATOM}name", "") for a in e.findall(f"{ATOM}author")]
        cats = [c.get("term") for c in e.findall(f"{ATOM}category")]
        text = (title + " " + summary).lower()
        hits = sum(1 for k in keywords if k.lower() in text)
        papers.append({
            "id": aid, "title": title, "url": f"https://arxiv.org/abs/{aid}",
            "authors": authors[:3], "n_authors": len(authors),
            "published": e.findtext(f"{ATOM}published", "")[:10],
            "categories": cats, "kw_hits": hits,
            "abstract": trim(summary, 900), "excerpt": first_sentence(summary),
            "cited_by": None, "venue": None})
    return papers


def enrich_openalex(papers):
    """Attach citation count / venue by arXiv DOI. Missing = stays None."""
    for i in range(0, len(papers), 40):
        batch = papers[i:i + 40]
        dois = "|".join(f"10.48550/arxiv.{p['id']}".lower() for p in batch)
        url = ("https://api.openalex.org/works?per_page=50&select=doi,cited_by_count,primary_location"
               "&filter=doi:" + urllib.parse.quote(dois, safe="|:/."))
        data = json.loads(get(url))
        by_doi = {(w.get("doi") or "").lower().replace("https://doi.org/", ""): w
                  for w in data.get("results", [])}
        for p in batch:
            w = by_doi.get(f"10.48550/arxiv.{p['id']}".lower())
            if w:
                p["cited_by"] = w.get("cited_by_count")
                src = ((w.get("primary_location") or {}).get("source") or {})
                p["venue"] = src.get("display_name")
        time.sleep(0.2)


def score(p):
    # Transparent rule: keyword hits dominate, citations add log-scaled weight.
    return p["kw_hits"] * 2 + math.log1p(p["cited_by"] or 0)


# ---------- OpenAlex classics ----------
def _words(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower().replace("-", " ")).split()


def title_matches(query, title):
    """True only if every (3+ letter) word of the query starts some title word."""
    toks = [t for t in _words(query) if len(t) >= 3] or _words(query)
    tw = _words(title)
    return all(any(w.startswith(tok) for w in tw) for tok in toks)


def abstract_from_index(idx):
    if not idx:
        return ""
    pos = {}
    for word, places in idx.items():
        for p in places:
            pos[p] = word
    return " ".join(pos[i] for i in sorted(pos))


def classics(queries, keep):
    # Relevance-ranked search (API default), keep only papers whose TITLE contains
    # the query terms, then order by citations.
    seen = {}
    for q in queries:
        params = urllib.parse.urlencode({
            "search": q, "filter": "from_publication_date:2012-01-01,type:article",
            "per_page": 100,
            "select": "id,title,publication_year,cited_by_count,doi,primary_location,authorships,abstract_inverted_index"})
        data = json.loads(get("https://api.openalex.org/works?" + params))
        for w in data.get("results", []):
            if w["id"] in seen or not title_matches(q, w.get("title")):
                continue
            src = ((w.get("primary_location") or {}).get("source") or {})
            abstract = clean(abstract_from_index(w.get("abstract_inverted_index")))
            seen[w["id"]] = {
                "title": w.get("title"), "year": w.get("publication_year"),
                "cited_by": w.get("cited_by_count"), "venue": src.get("display_name"),
                "url": w.get("doi") or w["id"], "query": q,
                "authors": [a["author"]["display_name"] for a in w.get("authorships", [])[:3]],
                "abstract": trim(abstract, 900), "excerpt": first_sentence(abstract)}
        time.sleep(0.2)
    return sorted(seen.values(), key=lambda x: x["cited_by"] or 0, reverse=True)[:keep]


# ---------- News (RSS / Atom) ----------
def parse_date(s):
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse_feed(xml_bytes, name):
    root = ET.fromstring(xml_bytes)
    items = []
    for it in root.iter("item"):  # RSS 2.0
        items.append((it.findtext("title"), it.findtext("link"),
                      it.findtext("pubDate") or it.findtext("{http://purl.org/dc/elements/1.1/}date"),
                      it.findtext("description") or it.findtext(CONTENT)))
    for en in root.iter(f"{ATOM}entry"):  # Atom
        link = en.find(f"{ATOM}link")
        items.append((en.findtext(f"{ATOM}title"), link.get("href") if link is not None else None,
                      en.findtext(f"{ATOM}published") or en.findtext(f"{ATOM}updated"),
                      en.findtext(f"{ATOM}summary") or en.findtext(f"{ATOM}content")))
    out = []
    for title, link, date, desc in items:
        d = parse_date(date)
        if title and link and d:
            title = " ".join(title.split())
            text = clean(desc)
            excerpt = first_sentence(text)
            if excerpt.lower().startswith(title.lower()[:40]):
                excerpt = ""  # description only repeats the headline
            out.append({"title": title, "url": link.strip(), "source": name,
                        "published": d.isoformat(), "desc": trim(text, 500), "excerpt": excerpt})
    return out


def collect_news(feeds, days, per_feed):
    cutoff = NOW - timedelta(days=days)
    seen, news = set(), []
    for f in feeds:
        key = "feed:" + f["name"]
        items = safe(key, lambda f=f: parse_feed(get(f["url"]), f["name"]), [])
        parsed = len(items)
        newest = max((i["published"] for i in items), default="none")[:10]
        kws = [k.lower() for k in f.get("filter_keywords", [])]
        if kws:  # general-interest feeds: keep only on-topic titles
            items = [i for i in items if any(k in i["title"].lower() for k in kws)]
        items = [i for i in items if datetime.fromisoformat(i["published"]) >= cutoff]
        if STATUS.get(key) == "ok":  # diagnostics for silent zero-item feeds
            STATUS[key] = f"ok (parsed={parsed}, newest={newest}, kept_in_window={len(items)})"
        items.sort(key=lambda i: i["published"], reverse=True)
        for i in items[:per_feed]:
            if i["url"] not in seen:
                seen.add(i["url"])
                news.append(i)
    news.sort(key=lambda i: i["published"], reverse=True)
    return news


# ---------- main ----------
def main():
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    (ROOT / "data").mkdir(exist_ok=True)
    for key, fld in cfg["fields"].items():
        a = fld["arxiv"]
        subs = prepare_subfields(fld["subfields"])
        papers = safe(f"{key}:arxiv", lambda: parse_arxiv(get(arxiv_query(a)), a["keywords"]), [])
        if papers:
            safe(f"{key}:openalex-enrich", lambda: enrich_openalex(papers), None)
        for p in papers:
            p["score"] = round(score(p), 2)
        papers.sort(key=lambda p: (p["score"], p["published"]), reverse=True)
        papers = papers[:cfg["new_papers_keep"]]
        classic = safe(f"{key}:openalex-classics",
                       lambda: classics(fld["classic_queries"], cfg["classic_keep"]), [])
        news = collect_news(fld["feeds"], cfg["news_days"], cfg["news_per_feed"])
        for p in papers + classic:
            p["tag"] = classify(p["title"], p.get("abstract"), subs)
        for n in news:
            n["tag"] = classify(n["title"], n.get("desc"), subs)
        out = {
            "field": key, "label": fld["label"], "updated": NOW.isoformat(timespec="seconds"),
            "subfields": [{"id": s["id"], "label": s["label"]} for s in subs],
            "papers_new": papers, "papers_classic": classic, "news": news,
            "score_rule": "score = 2*keyword_hits(title+abstract) + ln(1+citations); source: arXiv API, OpenAlex"}
        (ROOT / "data" / f"{key}.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    (ROOT / "data" / "meta.json").write_text(
        json.dumps({"updated": NOW.isoformat(timespec="seconds"), "status": STATUS},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    bad = {k: v for k, v in STATUS.items() if not v.startswith("ok")}
    print(json.dumps({"ok": len(STATUS) - len(bad), "failed": bad}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
