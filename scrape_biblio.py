"""
Scrape and reformat ÚFAL biblio entries.

Loads https://ufal.mff.cuni.cz/biblio/ and extracts publications that
match a given author substring and year, then prints them in a style
similar to https://ufal.mff.cuni.cz/ondrej-dusek/bibliography.

Usage (examples):
    python scrape_biblio.py --author "Ondřej Dušek" --year 2024
    python scrape_biblio.py --author "Dusek" --year 2019

Outputs lines like:
    • Author1, Author2, ... Title, in: Venue. [Link1](url) [Link2](url)

Notes:
- Matching is case-insensitive and accent-insensitive.
- The script parses the main title link and venue text from list items.
    The biblio site has heterogeneous formatting, so some entries may be best-effort.
"""

import argparse
import re
import sys
import unicodedata
from typing import List, Optional
from html import escape
import fitz
import difflib

import requests
from bs4 import BeautifulSoup, Tag


UFAL_BIBLIO_URL = "https://ufal.mff.cuni.cz/biblio/"


def strip_accents(text: str) -> str:
    """Return a version of text with diacritics removed (NFKD -> ASCII)."""
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    )


def fetch_biblio_html(session: Optional[requests.Session] = None) -> str:
    sess = session or requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    }
    resp = sess.get(UFAL_BIBLIO_URL, timeout=30, headers=headers)
    resp.raise_for_status()
    # Force proper UTF-8 decoding to avoid mojibake (names with diacritics)
    content = resp.content
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        # Fallback to requests' detected encoding
        return resp.text


def extract_main_title_link(li: Tag) -> Optional[Tag]:
    """Prefer <span class='pubtitle'> inner <a> as the main title link."""
    pt = li.find("span", class_="pubtitle")
    if pt:
        a = pt.find("a", href=True)
        if a:
            return a
    # Fallback: first anchor in the item
    return li.find("a", href=True)


def map_link(url: str) -> Optional[str]:
    MAPPING = {
        'arxiv.org': 'ArXiv',
        'aclanthology.org': 'Anthology',
    }
    for key, out in MAPPING.items():
        if key in url:
            return out
    return None


def clean_whitespace(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def extract_venue_text(li: Tag) -> str:
    """Extract the venue as the italic text following 'In:'."""
    # Iterate through descendants to find the first <i> after the 'In:' marker
    seen_in_marker = False
    for node in li.descendants:
        if isinstance(node, str):
            if not seen_in_marker and "In:" in node:
                seen_in_marker = True
            continue
        if isinstance(node, Tag) and node.name == "i" and seen_in_marker:
            return clean_whitespace(node.get_text(" ", strip=True))
    # Fallback: regex on raw text
    raw = li.get_text(" ", strip=True)
    m = re.search(r"In:\s*([^()]+)", raw)
    return clean_whitespace(m.group(1)) if m else ""


def normalize_text(text: str) -> str:
    # use unicode normalization -- strip accents, normalize apostrophes and quotation marks
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[‘’´`]", "'", text)  # normalize apostrophes
    text = re.sub(r'[“”„‟″]', '"', text)  # normalize quotation marks
    text = re.sub(r"[●○•‣◦▪▫◆◇]", " ", text)  # remove bullet points
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s*([:,;])\s+", r" ", text).strip()
    text = text.lower()
    return text


def longest_common_word_substring(a: str, b: str) -> List[str]:
    """Return the longest common substring (sequence of words) between a and b."""
    a_words = a.split()
    b_words = b.split()
    sm = difflib.SequenceMatcher(None, a_words, b_words)
    match = max(sm.get_matching_blocks(), key=lambda x: x.size)
    if match.size == 0:
        return ""
    return a_words[match.a : match.a + match.size]


def reformat_item(li: Tag, poster_texts: Optional[List]) -> Optional[str]:
    # Authors and year
    authors_span = li.find("span", class_="authors")
    if not authors_span:
        return None
    authors_raw = authors_span.get_text(" ", strip=True)
    m = re.search(r"^(.*)\((\d{4})\):\s*$", authors_raw)  # match & strip year
    authors_txt = clean_whitespace(authors_raw[: m.start()] if m else authors_raw)
    # Build HTML
    authors_html = escape(authors_txt)

    # Title
    title_a = extract_main_title_link(li)
    title_text = clean_whitespace(title_a.get_text(strip=True)) if title_a else ""
    # Title should be bold text only (no link)
    title_html = f"<strong>{escape(title_text)}</strong>"

    # Venue
    venue_text = extract_venue_text(li)

    # Links from biblio -- main paper link
    links_html = ""
    # get first url/pdf/local PDF/local ZIP link
    supplemental_a: Optional[Tag] = None
    for a in li.find_all("a", href=True):
        label = (a.get_text(strip=True) or "").lower()
        if any(key in label for key in ["url", "pdf", "local PDF", "local ZIP"]):
            supplemental_a = a
            break

    if supplemental_a is not None:
        # remap link title according to where it leads
        lab = map_link(str(supplemental_a.get("href"))) or supplemental_a.get_text(strip=True) or "link"
        href2 = escape(str(supplemental_a.get("href")), quote=True)
        links_html = f"[<a href=\"{href2}\">{escape(lab)}</a>]"

    # Additional links from poster texts
    if poster_texts:
        found = False
        norm_title = normalize_text(title_text)
        for poster_filename, poster_text in poster_texts.items():
            if norm_title in poster_text:
                hrefp = escape(poster_filename, quote=True)
                links_html += f" [<a href=\"{hrefp}\">Poster</a>]"
                found = True
                break

        if not found:
            # Try LCS on word level with poster texts
            best_match = None
            best_lcs = []
            for poster_filename, poster_text in poster_texts.items():
                lcs = longest_common_word_substring(norm_title, poster_text)
                if len(lcs) > len(best_lcs):
                    best_lcs = lcs
                    best_match = poster_filename

            if len(best_lcs) >= 4 and best_match:
                hrefp = escape(best_match, quote=True)
                links_html += f" [<a href=\"{hrefp}\">Poster</a>]"


    core = f"{authors_html}. {title_html}"
    if venue_text:
        core += f", in: {escape(venue_text)}."
    if links_html:
        core += f" {links_html}"
    return f"<li>{core}</li>"


def find_matching_items(html: str, author_query: str, year: int) -> List[str]:
    soup = BeautifulSoup(html, "lxml")
    items = soup.find_all("li")
    matches: List[str] = []
    author_norm = strip_accents(author_query.lower())

    # process all biblio items
    for li in items:
        authors_span = li.find("span", class_="authors")
        if not authors_span:
            continue
        authors_raw = authors_span.get_text(" ", strip=True)
        m = re.search(r"^(.*)\((\d{4})\):\s*$", authors_raw)
        if not m:
            continue
        authors_txt = clean_whitespace(m.group(1))
        y = int(m.group(2))
        if y != year:
            continue
        if author_norm not in strip_accents(authors_txt.lower()):
            continue
        matches.append(li)
    return matches


def extract_poster_text(pdf_filename: str) -> str:
    """Extract all text from the poster PDF."""
    try:
        doc = fitz.open(pdf_filename)
        texts = []
        for page in doc:
            texts.append(page.get_text())
        return normalize_text("\n".join(texts))
    except Exception as e:
        print(f"Error reading PDF {pdf_filename}: {e}", file=sys.stderr)
        return ""


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Scrape UFAL biblio and reformat entries.")
    ap.add_argument("--author", required=True, help="Author name (substring match, case-insensitive)")
    ap.add_argument("--year", required=True, type=int, help="Publication year to filter")
    ap.add_argument("--posters", nargs="*", default=[], help="List of poster PDF filenames to consider as additional links")

    args = ap.parse_args(argv)

    try:
        html = fetch_biblio_html()
    except Exception as e:
        print(f"Error fetching {UFAL_BIBLIO_URL}: {e}", file=sys.stderr)
        return 2

    poster_texts = None
    if args.posters:
        poster_texts = {poster: extract_poster_text(poster) for poster in args.posters}

    items = find_matching_items(html, author_query=args.author, year=args.year)

    if not items:
        print("No matching items found.")

    items = [reformat_item(li, poster_texts=poster_texts) for li in items]

    # Output as an HTML list
    for line in items:
        print(line)

    return 0


if __name__ == "__main__":
    main()
