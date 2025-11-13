"""
Scrape and reformat ÚFAL biblio entries.

Loads https://ufal.mff.cuni.cz/biblio/ and extracts publications that
match a given author substring and year, then prints them in a style
similar to https://ufal.mff.cuni.cz/ondrej-dusek/bibliography.

Usage (examples):
    python scrape_biblio.py --author "Ondřej Dušek" --year 2024
    python scrape_biblio.py --author "Dusek" --year 2019
    python scrape_biblio.py --author "Dusek" --year 2024 --bib custom_papers.bib

Outputs lines like:
    • Author1, Author2, ... Title, in: Venue. [Link1](url) [Link2](url)

Notes:
- Matching is case-insensitive and accent-insensitive.
- The script parses the main title link and venue text from list items.
    The biblio site has heterogeneous formatting, so some entries may be best-effort.
- You can add entries from a custom .bib file using the --bib argument.
"""

import argparse
import re
import sys
from typing import Dict, List, Optional
from html import escape
import difflib
from dataclasses import dataclass

import unicodedata
import fitz
import requests
import bibtexparser

from bs4 import BeautifulSoup, Tag
from bibtexparser.bparser import BibTexParser

UFAL_BIBLIO_URL = "https://ufal.mff.cuni.cz/biblio/"


@dataclass
class PublicationEntry:
    """Data class to hold publication information."""
    authors: str
    title: str
    venue: str
    links: List[tuple[str, str]]  # List of (label, url) tuples
    
    def __post_init__(self):
        """Ensure links is always a list."""
        if self.links is None:
            self.links = []

    def __str__(self) -> str:
        """Format a PublicationEntry object as an HTML list item.
        Outputs lines like:
            <li>Authors. <strong>Title</strong>, in: Venue. [<a href="url">Label</a>] ...</li>
        """
        # Escape and format authors
        authors_html = escape(self.authors)
        # Escape and format title (bold)
        title_html = f"<strong>{escape(self.title)}</strong>"
        
        # Format links
        links_html = ""
        if self.links:
            link_parts = []
            for label, url in self.links:
                href = escape(url, quote=True)
                link_parts.append(f"[<a href=\"{href}\">{escape(label)}</a>]")
            links_html = " ".join(link_parts)
        
        # Build the complete entry
        core = f"{authors_html}. {title_html}"
        if self.venue:
            core += f", in: {escape(self.venue)}."
        if links_html:
            core += f" {links_html}"
        
        return f"<li>{core}</li>"


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


def normalize_text(text: str, lowercase: bool = True) -> str:
    # use unicode normalization -- strip accents, normalize apostrophes and quotation marks
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[‘’´`]", "'", text)  # normalize apostrophes
    text = re.sub(r'[“”„‟″]', '"', text)  # normalize quotation marks
    text = re.sub(r"[●○•‣◦▪▫◆◇]", " ", text)  # remove bullet points
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s*([:,;])\s+", r" ", text).strip()
    if lowercase:
        text = text.lower()
    return text


def longest_common_word_substring(a: str, b: str) -> List[str]:
    """Return the longest common substring (sequence of words) between a and b."""
    a_words = a.split()
    b_words = b.split()
    sm = difflib.SequenceMatcher(None, a_words, b_words)
    match = sm.find_longest_match(0, len(a_words), 0, len(b_words))
    if match.size == 0:
        return []
    return a_words[match.a: match.a + match.size]


def find_additional_link(title_text: str, files_texts: Dict, link_name) -> Optional[tuple[str, str]]:
    """Find a poster/slides link matching the title text in the files_texts dict.
    
    Returns a tuple of (link_name, url) if found, None otherwise.
    """
    # Direct substring match of the normalized title in files_texts
    norm_title = normalize_text(title_text)
    for filename, file_text in files_texts.items():
        if norm_title in file_text:
            return (link_name, filename)

    # Backoff: try LCS on word level with files_texts
    best_match = None
    best_lcs = []
    for filename, file_text in files_texts.items():
        lcs = longest_common_word_substring(norm_title, file_text)
        if len(lcs) > len(best_lcs):
            best_lcs = lcs
            best_match = filename

    if len(best_lcs) >= 4 and best_match:
        return (link_name, best_match)
    return None  # no poster/slides link found


def reformat_biblio_entry(li: Tag, poster_texts: Optional[Dict], slides_texts: Optional[Dict]) -> PublicationEntry:
    """Parse a biblio entry and return a PublicationEntry object."""
    # Authors and year
    authors_span = li.find("span", class_="authors")
    if not authors_span:
        raise ValueError("No authors span found in biblio entry.")
    authors_raw = authors_span.get_text(" ", strip=True)
    m = re.search(r"^(.*)\((\d{4})\):\s*$", authors_raw)  # match & strip year
    authors_txt = clean_whitespace(authors_raw[: m.start()] if m else authors_raw)

    # Title
    title_a = extract_main_title_link(li)
    title_text = clean_whitespace(title_a.get_text(strip=True)) if title_a else ""

    # Venue
    venue_text = extract_venue_text(li)

    # Links from biblio -- main paper link
    links = []
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
        url = str(supplemental_a.get("href"))
        links.append((lab, url))

    # Additional links from poster texts
    if poster_texts:
        poster_link = find_additional_link(title_text, poster_texts, "Poster")
        if poster_link:
            links.append(poster_link)
    if slides_texts:
        slides_link = find_additional_link(title_text, slides_texts, "Slides")
        if slides_link:
            links.append(slides_link)

    return PublicationEntry(authors=authors_txt, title=title_text, venue=venue_text, links=links)


def find_matching_biblio_entries(html: str, author_query: str, year: int) -> List[Tag]:
    soup = BeautifulSoup(html, "lxml")
    items = soup.find_all("li")
    matches: List[Tag] = []
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


def extract_pdf_text(pdf_source: str, lowercase: bool = True) -> str:
    """Extract all text from a PDF file (local file or URL)."""
    try:
        # Check if the source is a URL
        if pdf_source.startswith(('http://', 'https://')):
            # if the URL doesn't include '.pdf' & is to ACL anthology or arXiv, adjust it
            if 'pdf' not in pdf_source:
                if 'aclanthology.org' in pdf_source:
                    pdf_source = pdf_source.rstrip('/')
                    pdf_source += '.pdf'
                elif 'arxiv.org' in pdf_source:
                    pdf_source = pdf_source.replace('/abs/', '/pdf/')
            # Download PDF from URL
            response = requests.get(pdf_source, timeout=30)
            response.raise_for_status()
            # Open PDF from bytes
            doc = fitz.open(stream=response.content, filetype="pdf")
        else:
            # Open local PDF file
            doc = fitz.open(pdf_source)
        
        texts = []
        for page in doc:
            texts.append(page.get_text())
        doc.close()
        return normalize_text("\n".join(texts), lowercase=lowercase)
    except Exception as e:
        print(f"Error reading PDF {pdf_source}: {e}", file=sys.stderr)
        return ""


def format_bib_authors(author_string: str) -> str:
    """Format BibTeX author string (e.g., 'Last1, First1 and Last2, First2') to readable format."""
    # Split by 'and'
    authors = [a.strip() for a in author_string.split(" and ")]
    formatted = []
    
    for author in authors:
        # Handle different author formats
        if "," in author:
            # Format: Last, First
            parts = [p.strip() for p in author.split(",", 1)]
            if len(parts) == 2:
                formatted.append(f"{parts[1]} {parts[0]}")
            else:
                formatted.append(author)
        else:
            # Format: First Last (already in correct order)
            formatted.append(author)
    
    return ", ".join(formatted)


def reformat_bibtex_entry(entry: Dict, poster_texts: Optional[Dict], slides_texts: Optional[Dict]) -> PublicationEntry:
    """Parse a BibTeX entry and return a PublicationEntry object."""
    
    title = re.sub(r"^\{(.*)\}$", r"\1", entry["title"].strip())  # Remove surrounding braces if present
    authors = format_bib_authors(entry["author"])
    
    # Venue
    venue = entry.get("booktitle") or entry.get("journal") or entry.get("howpublished") or ""
    
    # Links
    links = []
    
    # Check for URL field
    if "url" in entry:
        url = entry["url"].strip()
        # Try to map the URL to a known label
        label = map_link(url) or "Link"
        links.append((label, url))
       
    # Check for eprint/arxiv
    if "eprint" in entry or "arxiv" in entry:
        arxiv_id = entry.get("eprint", entry.get("arxiv", "")).strip()
        if arxiv_id:
            arxiv_url = f"https://arxiv.org/abs/{arxiv_id}"
            links.append(("ArXiv", arxiv_url))
       
    return PublicationEntry(authors=authors, title=title, venue=venue, links=links)


def find_matching_bibfile_entries(bib_filename: str, author_query: str, year: int) -> List[Dict]:
    """Parse a BibTeX file and return formatted entries matching the author and year."""
    try:
        with open(bib_filename, 'r', encoding='utf-8') as bibfile:
            parser = BibTexParser(common_strings=True)
            bib_database = bibtexparser.load(bibfile, parser=parser)
    except Exception as e:
        print(f"Error reading BibTeX file {bib_filename}: {e}", file=sys.stderr)
        return []
    
    matches = []
    author_norm = strip_accents(author_query.lower())
    
    for entry in bib_database.entries:
        entry_year = entry.get("year", 0)
        if int(entry_year) != year:
            continue
        if author_norm not in strip_accents(entry.get("author", "").lower()):
            continue
        matches.append(entry)
    
    return matches

def find_repo_link(paper_text: str) -> Optional[tuple[str, str]]:
    """Find repository link following 'our code' or 'our data' in paper text.

    Returns a tuple of (label, url) if found, None otherwise.
    """
    # Pattern to match "our code/data" followed by up to 10 words and a URL
    pattern = r'(?:our\s+(?:code|data|implementation|dataset))(?:\s+\w+){0,10}?\s+(https?://[^\s\)]+)'

    match = re.search(pattern, paper_text, re.IGNORECASE)
    if match:
        url = match.group(1).rstrip('.,;:')  # Clean trailing punctuation
        # Determine label based on what was mentioned
        label = "Code" if "code" in match.group(0).lower() or "implementation" in match.group(0).lower() else "Data"
        return (label, url)

    return None


def main(argv: Optional[List[str]] = None):
    """Main entry point."""
    ap = argparse.ArgumentParser(description="Scrape UFAL biblio and reformat entries.")
    ap.add_argument("--author", required=True, help="Author name (substring match, case-insensitive)")
    ap.add_argument("--year", required=True, type=int, help="Publication year to filter")
    ap.add_argument("--posters", nargs="*", default=[], help="List of poster PDF filenames to consider as additional links")
    ap.add_argument("--slides", nargs="*", default=[], help="List of slides PDF filenames to consider as additional links")
    ap.add_argument("--biblio", help="Load data from Biblio")
    ap.add_argument("--bibfile", help="Load data from a BibTeX file", type=str, default=None)
    ap.add_argument("--find-repo-links", action="store_true", help="Find repository links in paper PDF, in addition to posters and slides")

    args = ap.parse_args(argv)

    # Prepare poster & slides texts if provided
    poster_texts = None
    if args.posters:
        poster_texts = {poster: extract_pdf_text(poster) for poster in args.posters}
    slides_texts = None
    if args.slides:
        slides_texts = {slide: extract_pdf_text(slide) for slide in args.slides}

    bib_items = []
    # Collect items from biblio scraping
    if args.biblio:
        html = fetch_biblio_html()
        matching_biblio_entries = find_matching_biblio_entries(html, author_query=args.author, year=args.year)
        bib_items = [reformat_biblio_entry(li, poster_texts=poster_texts, slides_texts=slides_texts) for li in matching_biblio_entries]

    # Collect items from BibTeX file if provided
    if args.bibfile:
        bib_entries = find_matching_bibfile_entries(args.bibfile, author_query=args.author, year=args.year)
        bib_items += [reformat_bibtex_entry(entry, poster_texts=poster_texts, slides_texts=slides_texts) for entry in bib_entries]

    # add poster and slides links
    for entry in bib_items:
        if args.find_repo_links:
            # Attempt to find repository link in the main paper PDF
            if entry.links:
                repo_link = find_repo_link(extract_pdf_text(entry.links[0][1], lowercase=False))
                if repo_link:
                    entry.links.append(repo_link)

        if poster_texts:
            poster_link = find_additional_link(entry.title, poster_texts, "Poster")
            if poster_link:
                entry.links.append(poster_link)
        if slides_texts:
            slides_link = find_additional_link(entry.title, slides_texts, "Slides")
            if slides_link:
                entry.links.append(slides_link)

    if not bib_items:
        print("No matching items found.")
    else:
        # Output as an HTML list
        for entry in bib_items:
            print(entry)


if __name__ == "__main__":
    main()
