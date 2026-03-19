#!/usr/bin/env python3
"""
PaperScan Weekly Digest
========================
Fetches new AI/ophthalmology papers from arXiv, PubMed, and Semantic Scholar,
deduplicates by DOI, skips already-sent papers, and emails a digest.

Usage:
    python tracker.py           # run normally
    python tracker.py --dry-run # print digest to terminal, don't send email
"""

import os
import re
import json
import time
import logging
import smtplib
import argparse
import hashlib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import yaml
import requests
import feedparser
from dotenv import load_dotenv
from tqdm import tqdm

# ── Setup ─────────────────────────────────────────────────────────────────────

load_dotenv()
BASE_DIR = Path(__file__).parent
CACHE_FILE = BASE_DIR / "sent_cache.json"
LOG_FILE = BASE_DIR / "logs" / "tracker.log"
LOG_FILE.parent.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

def load_config():
    with open(BASE_DIR / "config.yaml") as f:
        return yaml.safe_load(f)

# ── Cache (deduplication across runs) ────────────────────────────────────────

def load_cache():
    if CACHE_FILE.exists():
        with open(CACHE_FILE) as f:
            return json.load(f)
    return {"sent_ids": []}

def save_cache(cache):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)

def paper_id(paper):
    """Stable ID: prefer DOI, fall back to title hash."""
    if paper.get("doi"):
        return f"doi:{paper['doi'].strip().lower()}"
    title_hash = hashlib.md5(paper["title"].strip().lower().encode()).hexdigest()
    return f"title:{title_hash}"

# ── Keyword matching ──────────────────────────────────────────────────────────

def matches_query(text, medical_topics, ai_methods):
    """Return True if text contains ≥1 medical topic AND ≥1 AI method."""
    text_lower = text.lower()
    has_medical = any(kw.lower() in text_lower for kw in medical_topics)
    has_ai = any(kw.lower() in text_lower for kw in ai_methods)
    return has_medical and has_ai

# ── Source: arXiv ─────────────────────────────────────────────────────────────

def fetch_arxiv(config, since_date):
    log.info("Fetching from arXiv...")
    medical = config["medical_topics"]
    ai = config["ai_methods"]
    categories = config["arxiv_categories"]
    max_results = config["email"]["max_results_per_source"]

    # Build search query
    medical_query = " OR ".join(f'"{t}"' for t in medical[:10])  # top terms
    ai_query = " OR ".join(f'"{t}"' for t in ai[:10])
    cat_query = " OR ".join(f"cat:{c}" for c in categories)
    query = f"({medical_query}) AND ({ai_query}) AND ({cat_query})"

    url = "http://export.arxiv.org/api/query"
    params = {
        "search_query": query,
        "start": 0,
        "max_results": max_results,
        "sortBy": "submittedDate",
        "sortOrder": "descending"
    }

    try:
        resp = requests.get(url, params=params, timeout=30)
        feed = feedparser.parse(resp.text)
    except Exception as e:
        log.error(f"arXiv fetch failed: {e}")
        return []

    papers = []
    for entry in feed.entries:
        published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        if published < since_date:
            continue

        doi = None
        for link in entry.get("links", []):
            if "doi" in link.get("href", ""):
                doi = link["href"].replace("https://doi.org/", "").replace("http://dx.doi.org/", "")

        text_to_check = f"{entry.title} {entry.summary}"
        if not matches_query(text_to_check, medical, ai):
            continue

        papers.append({
            "title": entry.title.replace("\n", " ").strip(),
            "authors": ", ".join(a.name for a in entry.get("authors", [])[:5]),
            "abstract": entry.summary.replace("\n", " ").strip(),
            "doi": doi,
            "url": entry.link,
            "source": "arXiv",
            "published": published.strftime("%Y-%m-%d"),
        })

    log.info(f"  arXiv: {len(papers)} matching papers")
    return papers

# ── Source: PubMed ────────────────────────────────────────────────────────────

def fetch_pubmed(config, since_date):
    log.info("Fetching from PubMed...")
    medical = config["medical_topics"]
    ai = config["ai_methods"]
    max_results = config["email"]["max_results_per_source"]

    # Build PubMed query
    medical_terms = " OR ".join(f'"{t}"[tiab]' for t in medical)
    ai_terms = " OR ".join(f'"{t}"[tiab]' for t in ai)
    date_str = since_date.strftime("%Y/%m/%d")
    query = f"({medical_terms}) AND ({ai_terms}) AND (\"{date_str}\"[pdat] : \"3000\"[pdat])"

    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    # Step 1: search for IDs
    try:
        search_resp = requests.get(f"{base}/esearch.fcgi", params={
            "db": "pubmed", "term": query,
            "retmax": max_results, "retmode": "json",
            "sort": "pub_date"
        }, timeout=30)
        ids = search_resp.json()["esearchresult"]["idlist"]
    except Exception as e:
        log.error(f"PubMed search failed: {e}")
        return []

    if not ids:
        log.info("  PubMed: 0 results")
        return []

    # Step 2: fetch details
    try:
        fetch_resp = requests.get(f"{base}/efetch.fcgi", params={
            "db": "pubmed", "id": ",".join(ids),
            "retmode": "xml", "rettype": "abstract"
        }, timeout=30)
    except Exception as e:
        log.error(f"PubMed fetch failed: {e}")
        return []

    # Parse XML manually (avoid heavy deps)
    papers = []
    xml = fetch_resp.text

    articles = re.split(r"<PubmedArticle>", xml)[1:]
    for article in articles:
        try:
            title = re.search(r"<ArticleTitle>(.*?)</ArticleTitle>", article, re.DOTALL)
            title = re.sub(r"<[^>]+>", "", title.group(1)).strip() if title else "No title"

            abstract_match = re.search(r"<AbstractText[^>]*>(.*?)</AbstractText>", article, re.DOTALL)
            abstract = re.sub(r"<[^>]+>", "", abstract_match.group(1)).strip() if abstract_match else ""

            doi_match = re.search(r'<ArticleId IdType="doi">(.*?)</ArticleId>', article)
            doi = doi_match.group(1).strip() if doi_match else None

            pmid_match = re.search(r'<ArticleId IdType="pubmed">(.*?)</ArticleId>', article)
            pmid = pmid_match.group(1).strip() if pmid_match else None

            # Authors
            author_matches = re.findall(r"<LastName>(.*?)</LastName>.*?<ForeName>(.*?)</ForeName>", article, re.DOTALL)
            authors = ", ".join(f"{fn} {ln}" for ln, fn in author_matches[:5])

            # Date
            year = re.search(r"<PubDate>.*?<Year>(\d{4})</Year>", article, re.DOTALL)
            month = re.search(r"<PubDate>.*?<Month>(\w+)</Month>", article, re.DOTALL)
            pub_date = f"{year.group(1)}-{month.group(1)}" if year and month else (year.group(1) if year else "unknown")

            text_to_check = f"{title} {abstract}"
            if not matches_query(text_to_check, medical, ai):
                continue

            url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else ""

            papers.append({
                "title": title,
                "authors": authors,
                "abstract": abstract,
                "doi": doi,
                "url": url,
                "source": "PubMed",
                "published": pub_date,
            })
        except Exception as e:
            log.debug(f"PubMed parse error: {e}")
            continue

    log.info(f"  PubMed: {len(papers)} matching papers")
    return papers

# ── Source: Semantic Scholar ──────────────────────────────────────────────────

def fetch_semantic_scholar(config, since_date):
    log.info("Fetching from Semantic Scholar...")
    medical = config["medical_topics"]
    ai = config["ai_methods"]
    max_results = min(config["email"]["max_results_per_source"], 100)

    # Build a focused query (S2 uses simpler keyword search)
    query_terms = [
        "ophthalmology deep learning",
        "retinal fundus segmentation",
        "OCT machine learning",
        "fundus image classification",
        "neuro-ophthalmology AI",
        "visual field deep learning",
        "retinal disease detection neural network",
        "OCTA segmentation",
    ]

    papers = []
    seen_titles = set()
    since_str = since_date.strftime("%Y-%m-%d")

    for query in query_terms[:4]:  # limit API calls
        try:
            resp = requests.get(
                "https://api.semanticscholar.org/graph/v1/paper/search",
                params={
                    "query": query,
                    "fields": "title,authors,abstract,externalIds,publicationDate,url",
                    "limit": 25,
                    "publicationDateOrYear": f"{since_str}:",
                },
                timeout=30
            )
            time.sleep(1)  # rate limit: 1 req/sec for unauthenticated
            data = resp.json()
        except Exception as e:
            log.error(f"Semantic Scholar fetch failed for '{query}': {e}")
            continue

        for item in data.get("data", []):
            title = item.get("title", "").strip()
            if not title or title.lower() in seen_titles:
                continue
            seen_titles.add(title.lower())

            abstract = item.get("abstract") or ""
            text_to_check = f"{title} {abstract}"
            if not matches_query(text_to_check, medical, ai):
                continue

            doi = item.get("externalIds", {}).get("DOI")
            authors = ", ".join(a["name"] for a in item.get("authors", [])[:5])
            pub_date = item.get("publicationDate") or ""
            url = item.get("url") or (f"https://doi.org/{doi}" if doi else "")

            papers.append({
                "title": title,
                "authors": authors,
                "abstract": abstract,
                "doi": doi,
                "url": url,
                "source": "Semantic Scholar",
                "published": pub_date,
            })

    log.info(f"  Semantic Scholar: {len(papers)} matching papers")
    return papers

# ── Deduplication ─────────────────────────────────────────────────────────────

def deduplicate(papers):
    seen = {}
    for p in papers:
        pid = paper_id(p)
        if pid not in seen:
            seen[pid] = p
        else:
            # Merge: prefer entry with more info
            existing = seen[pid]
            if len(p.get("abstract", "")) > len(existing.get("abstract", "")):
                seen[pid] = p
    return list(seen.values())

# ── Email HTML digest ─────────────────────────────────────────────────────────

SOURCE_COLORS = {
    "arXiv":            ("#e8f0fe", "#1a73e8"),
    "PubMed":           "#fff8e1",
    "Semantic Scholar": "#f3e5f5",
}

def source_badge(source):
    colors = {
        "arXiv":            ("#1a73e8", "#e8f0fe"),
        "PubMed":           ("#e65100", "#fff8e1"),
        "Semantic Scholar": ("#6a1b9a", "#f3e5f5"),
    }
    fg, bg = colors.get(source, ("#333", "#eee"))
    return (
        f'<span style="background:{bg};color:{fg};padding:2px 8px;'
        f'border-radius:12px;font-size:11px;font-weight:600;">{source}</span>'
    )

def truncate(text, max_chars=400):
    if not text:
        return "<em>No abstract available.</em>"
    text = text.strip()
    return text[:max_chars] + "…" if len(text) > max_chars else text

def build_html(papers, since_date, run_date):
    n = len(papers)
    date_range = f"{since_date.strftime('%b %d')} – {run_date.strftime('%b %d, %Y')}"

    source_counts = {}
    for p in papers:
        source_counts[p["source"]] = source_counts.get(p["source"], 0) + 1
    source_summary = " &nbsp;|&nbsp; ".join(
        f"{s}: <strong>{c}</strong>" for s, c in sorted(source_counts.items())
    )

    cards = ""
    for i, p in enumerate(papers, 1):
        badge = source_badge(p["source"])
        abstract = truncate(p.get("abstract", ""))
        url = p.get("url", "")
        link = f'<a href="{url}" style="color:#1a73e8;text-decoration:none;">Read paper →</a>' if url else ""
        doi_line = f'<span style="color:#888;font-size:11px;">DOI: {p["doi"]}</span>' if p.get("doi") else ""

        cards += f"""
        <div style="background:#fff;border:1px solid #e0e0e0;border-radius:8px;
                    padding:18px 20px;margin-bottom:16px;">
            <div style="display:flex;justify-content:space-between;
                        align-items:flex-start;margin-bottom:6px;">
                <span style="font-size:13px;color:#888;">{p.get('published','')}</span>
                {badge}
            </div>
            <h3 style="margin:6px 0 8px;font-size:15px;color:#1a1a2e;line-height:1.4;">
                {i}. {p['title']}
            </h3>
            <p style="margin:0 0 6px;font-size:12px;color:#555;">
                <strong>Authors:</strong> {p.get('authors') or 'N/A'}
            </p>
            <p style="margin:0 0 10px;font-size:13px;color:#444;line-height:1.6;">
                {abstract}
            </p>
            <div style="display:flex;gap:16px;align-items:center;">
                {link}
                {doi_line}
            </div>
        </div>
        """

    html = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
                background:#f5f5f5;margin:0;padding:20px;">
        <div style="max-width:700px;margin:0 auto;">

            <!-- Header -->
            <div style="background:linear-gradient(135deg,#1a1a2e 0%,#16213e 100%);
                        border-radius:12px;padding:28px 32px;margin-bottom:20px;color:#fff;">
                <h1 style="margin:0 0 6px;font-size:22px;">
                    🔬 PaperScan Weekly Digest
                </h1>
                <p style="margin:0;opacity:0.8;font-size:14px;">{date_range}</p>
            </div>

            <!-- Stats bar -->
            <div style="background:#fff;border:1px solid #e0e0e0;border-radius:8px;
                        padding:14px 20px;margin-bottom:20px;font-size:13px;color:#555;">
                📄 <strong>{n} new papers</strong> found &nbsp;|&nbsp; {source_summary}
            </div>

            <!-- Papers -->
            {cards if cards else '<p style="color:#888;text-align:center;">No new papers found this period.</p>'}

            <!-- Footer -->
            <div style="text-align:center;padding:20px;font-size:11px;color:#aaa;">
                Generated by PaperScan Digest &nbsp;·&nbsp; {run_date.strftime('%Y-%m-%d %H:%M UTC')}
                <br>Sources: arXiv · PubMed · Semantic Scholar
            </div>
        </div>
    </body>
    </html>
    """
    return html

# ── Send email ────────────────────────────────────────────────────────────────

def send_email(html, n_papers, run_date):
    sender = os.getenv("GMAIL_SENDER")
    password = os.getenv("GMAIL_APP_PASSWORD")
    recipient = os.getenv("GMAIL_RECIPIENT")

    if not all([sender, password, recipient]):
        raise ValueError("Missing GMAIL_SENDER, GMAIL_APP_PASSWORD or GMAIL_RECIPIENT in .env")

    subject = f"🔬 PaperScan Weekly Digest — {n_papers} new papers ({run_date.strftime('%b %d, %Y')})"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"PaperScan Digest <{sender}>"
    msg["To"] = recipient
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, password)
        server.sendmail(sender, recipient, msg.as_string())

    log.info(f"Email sent to {recipient}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="PaperScan Digest")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print digest to terminal instead of sending email")
    args = parser.parse_args()

    config = load_config()
    cache = load_cache()

    lookback = config["email"]["lookback_days"]
    run_date = datetime.now(timezone.utc)
    since_date = run_date - timedelta(days=lookback)

    log.info(f"=== PaperScan Digest — {run_date.strftime('%Y-%m-%d %H:%M UTC')} ===")
    log.info(f"Searching papers from {since_date.strftime('%Y-%m-%d')} to {run_date.strftime('%Y-%m-%d')}")

    # Fetch from all sources
    all_papers = []
    all_papers += fetch_arxiv(config, since_date)
    all_papers += fetch_pubmed(config, since_date)
    all_papers += fetch_semantic_scholar(config, since_date)

    log.info(f"Total before dedup: {len(all_papers)}")

    # Deduplicate by DOI / title hash
    papers = deduplicate(all_papers)
    log.info(f"After deduplication: {len(papers)}")

    # Filter already-sent papers
    sent_ids = set(cache.get("sent_ids", []))
    new_papers = [p for p in papers if paper_id(p) not in sent_ids]
    log.info(f"New (not previously sent): {len(new_papers)}")

    if not new_papers:
        log.info("No new papers to send. Exiting.")
        return

    # Sort by date descending
    new_papers.sort(key=lambda p: p.get("published", ""), reverse=True)

    # Build digest
    html = build_html(new_papers, since_date, run_date)

    if args.dry_run:
        print("\n" + "="*60)
        print(f"DRY RUN — {len(new_papers)} papers would be emailed")
        print("="*60)
        for i, p in enumerate(new_papers, 1):
            print(f"\n{i}. [{p['source']}] {p['title']}")
            print(f"   Authors: {p.get('authors','N/A')}")
            print(f"   Date: {p.get('published','')}")
            print(f"   DOI: {p.get('doi','N/A')}")
            print(f"   URL: {p.get('url','')}")
        print("\n(No email sent — dry run mode)")
    else:
        send_email(html, len(new_papers), run_date)

        # Update cache
        new_ids = [paper_id(p) for p in new_papers]
        cache["sent_ids"] = list(sent_ids | set(new_ids))
        cache["last_run"] = run_date.isoformat()
        save_cache(cache)
        log.info(f"Cache updated with {len(new_ids)} new paper IDs")

    log.info("=== Done ===")

if __name__ == "__main__":
    main()
