#!/usr/bin/env python3
"""Rebuild the publication list in publications.html from OpenAlex.

Runs weekly through GitHub Actions (.github/workflows/publications.yml) and can
also be run by hand:  python3 scripts/build_publications.py

What it does
  1. Downloads every work that OpenAlex attributes to the author below.
  2. Keeps peer-reviewed journal articles and reviews; drops preprints,
     discussion papers, conference abstracts, datasets, corrections, and
     duplicates of the same paper.
  3. Applies two hand-edited lists in data/:
       publications-exclude.txt  DOIs or OpenAlex IDs to remove
       publications-include.txt  DOIs to add (papers OpenAlex misses or drops)
       publications-exclude-affiliations.txt  institutions of a different
                                 Ryan C. Sullivan; papers listing Ryan there go
       publications-exclude-coauthors.txt  co-authors marking papers by a
                                 different Ryan C. Sullivan
  4. Bolds Ryan Sullivan and underlines group members (data/group-members.txt).
     Years are the journal issue year from Crossref (the year papers are cited
     by), falling back to OpenAlex's first-online year when Crossref has none.
     Book chapters are listed in their own section.
  5. Replaces the text between the PUBS markers in publications.html, but only
     if the list itself changed, so the page is not rewritten every week.

Safety: if OpenAlex cannot be reached, or the new list is much shorter than
the current one, the script stops with an error and changes nothing. GitHub
then emails the repository owner that the workflow failed.

Optional: an OpenAlex API key in the environment variable OPENALEX_API_KEY
(stored as a GitHub Actions secret, never in this repository).

Only the Python standard library is used.
"""

import datetime as dt
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------- settings
AUTHOR_ID = "A5039898956"            # OpenAlex ID for Ryan C. Sullivan
ORCID = "0000-0003-0701-7158"
CONTACT = "sullivan-lab-trentu.github.io"  # identifies the site to OpenAlex

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "publications.html")
DATA = os.path.join(ROOT, "data")
START, END = "<!-- PUBS:START -->", "<!-- PUBS:END -->"

KEEP_TYPES = {"article", "review", "letter"}
CHAPTER_TYPES = {"book-chapter"}     # listed in their own section
MY_NAME = "Ryan C. Sullivan"         # how Ryan's own name is shown
DROP_SOURCE_TYPES = {"repository", "conference", "ebook platform", "book series"}
# Source names that mark preprints, discussion papers, or meeting abstracts.
# Matched as whole words. "Proceedings" is deliberately absent (PNAS).
DROP_SOURCE_WORDS = [
    "discussions", "egusphere", "abstracts", "meeting", "conference",
    "research square", "chemrxiv", "biorxiv", "medrxiv", "arxiv", "ssrn",
    "essoar", "ess open archive", "authorea", "preprints", "zenodo",
    "figshare", "osti", "dryad", "pangaea",
]
DROP_SOURCE_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in DROP_SOURCE_WORDS) + r")\b")
DROP_TITLE = re.compile(r"^\s*(correction|erratum|corrigendum|retraction|"
                        r"author correction|reply to|response to)\b", re.I)
AUTHOR_LIMIT = 12     # longer author lists are shortened
SHRINK_LIMIT = 0.7    # refuse to publish a list shorter than 70% of the current one


# ---------------------------------------------------------------- helpers
def read_list(name):
    path = os.path.join(DATA, name)
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if line:
                out.append(line)
    return out


def norm_doi(doi):
    if not doi:
        return ""
    doi = doi.strip().lower()
    return re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", doi)


def norm_id(wid):
    return (wid or "").rsplit("/", 1)[-1].upper()


def fold(text):
    """Lower-case ASCII form of a name or title, for matching."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower()).strip()


def title_key(title):
    title = re.sub(r"<[^>]+>", "", title or "")
    return re.sub(r"\s+", "", fold(title))


def get_json(url, tries=4):
    key = os.environ.get("OPENALEX_API_KEY", "").strip()
    sep = "&" if "?" in url else "?"
    full = url + (f"{sep}api_key={urllib.parse.quote(key)}" if key else "")
    req = urllib.request.Request(full, headers={"User-Agent": f"publication-list ({CONTACT})"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if attempt == tries - 1:
                raise
        except urllib.error.URLError:
            if attempt == tries - 1:
                raise
        time.sleep(5 * (attempt + 1))
    return None


FIELDS = ("id,doi,display_name,publication_year,publication_date,type,"
          "primary_location,authorships,biblio,cited_by_count,is_retracted")


def fetch_author_works():
    works, cursor = [], "*"
    while cursor:
        url = ("https://api.openalex.org/works?filter=author.id:" + AUTHOR_ID +
               "&per-page=200&select=" + FIELDS + "&cursor=" + urllib.parse.quote(cursor))
        page = get_json(url)
        if page is None:
            raise RuntimeError("OpenAlex returned nothing for the author query")
        works.extend(page.get("results", []))
        cursor = page.get("meta", {}).get("next_cursor")
        if not page.get("results"):
            break
    return works


def crossref_record(doi):
    """The Crossref record for a DOI, or {}."""
    try:
        req = urllib.request.Request("https://api.crossref.org/works/" + urllib.parse.quote(doi, safe="/"),
                                     headers={"User-Agent": f"publication-list ({CONTACT})"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r).get("message", {}) or {}
    except Exception:
        return {}


def crossref_year(m):
    """Journal issue year from a Crossref record, or None."""
    for path in (("journal-issue", "published-print"), ("published-print",),
                 ("issued",), ("published",)):
        d = m
        for k in path:
            d = d.get(k) if isinstance(d, dict) else None
        try:
            return int(d["date-parts"][0][0])
        except (TypeError, KeyError, IndexError, ValueError):
            continue
    return None


def set_years(pubs):
    """Replace OpenAlex's first-online year with the journal issue year, and
    give book chapters their book title, publisher and pages from Crossref."""
    misses = 0
    for w in pubs:
        doi = norm_doi(w.get("doi"))
        m = crossref_record(doi) if doi else {}
        if w.get("type") in CHAPTER_TYPES and m:
            w["_book"] = (m.get("container-title") or [""])[0]
            w["_publisher"] = m.get("publisher") or ""
            w["_pages"] = m.get("page") or ""
        y = crossref_year(m)
        if y:
            w["publication_year"] = y
        else:
            misses += 1
        time.sleep(0.2)
    if misses:
        print(f"Crossref gave no year for {misses} papers; OpenAlex years used.")


def fetch_doi(doi):
    return get_json("https://api.openalex.org/works/doi:" + urllib.parse.quote(doi, safe="/") +
                    "?select=" + FIELDS)


# ---------------------------------------------------------------- filtering
def source_of(w):
    loc = w.get("primary_location") or {}
    return loc.get("source") or {}


def drop_reason(w):
    if w.get("is_retracted"):
        return "retracted"
    if (w.get("type") or "") not in KEEP_TYPES | CHAPTER_TYPES:
        return "type " + str(w.get("type"))
    src = source_of(w)
    if not src.get("display_name") and w.get("type") not in CHAPTER_TYPES:
        return "no journal"
    if (src.get("type") or "") in DROP_SOURCE_TYPES and w.get("type") not in CHAPTER_TYPES:
        return "source type " + str(src.get("type"))
    if DROP_SOURCE_RE.search(fold(src.get("display_name") or "")):
        return "source " + src["display_name"]
    if DROP_TITLE.match(w.get("display_name") or ""):
        return "correction or reply"
    return None


def better(a, b):
    """Pick one of two records of the same paper."""
    def score(w):
        src = source_of(w)
        return ((src.get("type") == "journal"), bool(w.get("doi")),
                w.get("cited_by_count") or 0)
    return a if score(a) >= score(b) else b


def other_ryan_affiliation(w, places):
    """The institution, if this paper lists Ryan at one of the other Ryan's."""
    for a in w.get("authorships") or []:
        if norm_id((a.get("author") or {}).get("id")) != AUTHOR_ID:
            continue
        names = [i.get("display_name") or "" for i in a.get("institutions") or []]
        names += a.get("raw_affiliation_strings") or []
        for n in names:
            if any(p in fold(n) for p in places):
                return n
    return None


def select(works, exclude, include_works):
    excl = {norm_doi(x) for x in exclude} | {norm_id(x) for x in exclude}
    # Co-authors who only appear on papers by a different Ryan C. Sullivan,
    # whose works OpenAlex has merged into this author record.
    other_ryan = member_keys("publications-exclude-coauthors.txt")
    # Institutions of the other Ryan C. Sullivan, where this Ryan never worked.
    other_places = [fold(x) for x in read_list("publications-exclude-affiliations.txt")]
    kept, dropped = {}, []
    for w in works:
        if norm_doi(w.get("doi")) in excl or norm_id(w.get("id")) in excl:
            dropped.append((w, "on exclude list"))
            continue
        place = other_ryan_affiliation(w, other_places)
        if place:
            dropped.append((w, "other Ryan C. Sullivan (listed at " + place + ")"))
            continue
        hit = next((a["author"].get("display_name") for a in (w.get("authorships") or [])
                    if name_key((a.get("author") or {}).get("display_name")) in other_ryan), None)
        if hit:
            dropped.append((w, "other Ryan C. Sullivan (co-author " + hit + ")"))
            continue
        reason = drop_reason(w)
        if reason:
            dropped.append((w, reason))
            continue
        k = title_key(w.get("display_name"))
        if k in kept:
            loser = w if better(kept[k], w) is kept[k] else kept[k]
            kept[k] = better(kept[k], w)
            dropped.append((loser, "duplicate"))
        else:
            kept[k] = w
    for w in include_works:          # include list overrides the filters
        kept[title_key(w.get("display_name"))] = w
    pubs = sorted(kept.values(),
                  key=lambda w: (w.get("publication_date") or str(w.get("publication_year") or "")),
                  reverse=True)
    return pubs, dropped


# ---------------------------------------------------------------- formatting
def member_keys(name="group-members.txt"):
    keys = set()
    for line in read_list(name):
        if "," in line:
            last, first = [p.strip() for p in line.split(",", 1)]
        else:
            parts = line.split()
            last, first = parts[-1], " ".join(parts[:-1])
        if last and first:
            keys.add((fold(last).replace(" ", ""), fold(first)[:1]))
    return keys


def name_key(display_name):
    parts = (display_name or "").replace(",", " ").split()
    if len(parts) < 2:
        return None
    return (fold(parts[-1]).replace(" ", ""), fold(parts[0])[:1])


ELEMENTS = set("""H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co
Ni Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La
Ce Pr Nd Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po Rn U""".split())
FORMULA = re.compile(r"(?<![A-Za-z0-9-])((?:[A-Z][a-z]?(?: ?\d{1,2})?)(?:(?: ?[A-Z][a-z]?)(?: ?\d{1,2})?)*)(?![a-z0-9])")


def fix_formula(m):
    """'N 2 O 5' -> N<sub>2</sub>O<sub>5</sub>, only for real element strings with digits."""
    text = m.group(1)
    parts = re.findall(r"([A-Z][a-z]?)( ?)(\d{0,2})", text)
    if not any(n for _, _, n in parts) or not all(e in ELEMENTS for e, _, _ in parts):
        return text
    if len(parts) == 1 and parts[0][1] == "":      # e.g. 'Fe2' written normally: leave
        pass
    return "".join(e + (f"<sub>{n}</sub>" if n else "") for e, _, n in parts)


def clean_title(t):
    t = html.escape(t or "", quote=False)
    # OpenAlex titles sometimes carry sub/superscript and italic tags
    t = re.sub(r"&lt;(/?)(sub|sup|i|em)&gt;", r"<\1\2>", t, flags=re.I).rstrip(".")
    # Split the text around existing tags so formulas are fixed only in plain text
    pieces = re.split(r"(<[^>]+>)", t)
    return "".join(p if p.startswith("<") else FORMULA.sub(fix_formula, p) for p in pieces)


def format_authors(w, members):
    names = []
    me_index = None
    for i, a in enumerate(w.get("authorships") or []):
        author = a.get("author") or {}
        name = html.escape(author.get("display_name") or a.get("raw_author_name") or "")
        if norm_id(author.get("id")) == AUTHOR_ID:
            name, me_index = f"<strong>{MY_NAME}</strong>", i
        elif name_key(author.get("display_name")) in members:
            name = f"<u>{name}</u>"
        names.append(name)
    if len(names) > AUTHOR_LIMIT:
        short = names[:AUTHOR_LIMIT - 2]
        if me_index is not None and me_index >= AUTHOR_LIMIT - 2:
            short += ["&hellip;", names[me_index]]
        return ", ".join(short) + f", et al. ({len(names)} authors)"
    if len(names) > 1:
        return ", ".join(names[:-1]) + ", and " + names[-1] if len(names) > 2 \
            else " and ".join(names)
    return names[0] if names else ""


def title_with_stop(w):
    t = clean_title(w.get("display_name"))
    return t if t.endswith(("?", "!")) else t + "."


def format_chapter(w, members):
    # Crossref's book title; OpenAlex often gives only "Elsevier eBooks" and the like
    book = html.escape(w.get("_book") or "")
    if not book and not re.search(r"\bebooks?\b", (source_of(w).get("display_name") or "").lower()):
        book = html.escape(source_of(w).get("display_name") or "")
    doi = norm_doi(w.get("doi"))
    link = (f' <a href="https://doi.org/{html.escape(doi)}">doi:{html.escape(doi)}</a>'
            if doi else "")
    where = f" In <em>{book}</em>," if book else ""
    if w.get("_publisher"):
        where += f" {html.escape(w['_publisher'])},"
    if w.get("_pages"):
        where += " pp. " + html.escape(w["_pages"]).replace("-", "&ndash;") + ","
    return (f'<li>{format_authors(w, members)}. {title_with_stop(w)}'
            f'{where} {w.get("publication_year")}.{link}</li>')


def format_entry(w, members):
    src = html.escape(source_of(w).get("display_name") or "")
    b = w.get("biblio") or {}
    detail = ""
    if b.get("volume"):
        detail = html.escape(b["volume"])
        if b.get("issue"):
            detail += f"({html.escape(b['issue'])})"
    pages = ""
    if b.get("first_page"):
        pages = html.escape(b["first_page"])
        if b.get("last_page") and b["last_page"] != b["first_page"]:
            pages += "&ndash;" + html.escape(b["last_page"])
    tail = ", ".join(x for x in [detail, pages] if x)
    doi = norm_doi(w.get("doi"))
    link = (f' <a href="https://doi.org/{html.escape(doi)}">doi:{html.escape(doi)}</a>'
            if doi else "")
    return (f'<li>{format_authors(w, members)}. {title_with_stop(w)} '
            f'<em>{src}</em>{", " + tail if tail else ""}, {w.get("publication_year")}.{link}</li>')


def build_list(pubs, members):
    chapters = [w for w in pubs if w.get("type") in CHAPTER_TYPES]
    pubs = [w for w in pubs if w.get("type") not in CHAPTER_TYPES]
    out, year = [], None
    for w in pubs:
        y = w.get("publication_year")
        if y != year:
            if year is not None:
                out.append("</ol>")
            out.append(f'<h3 class="pub-year">{y}</h3>')
            out.append('<ol class="pubs">')
            year = y
        out.append(format_entry(w, members))
    if year is not None:
        out.append("</ol>")
    if chapters:
        out.append('<h3 class="pub-year">Book chapters</h3>')
        out.append('<ol class="pubs">')
        out.extend(format_chapter(w, members) for w in chapters)
        out.append("</ol>")
    return "\n".join(out)


# ---------------------------------------------------------------- page update
def main():
    with open(PAGE, encoding="utf-8") as f:
        page = f.read()
    if START not in page or END not in page:
        sys.exit("publications.html is missing the PUBS markers")
    before, rest = page.split(START, 1)
    current, after = rest.split(END, 1)

    works = fetch_author_works()
    if not works:
        sys.exit("OpenAlex returned no works; nothing changed")
    include_works = []
    for doi in read_list("publications-include.txt"):
        w = fetch_doi(norm_doi(doi))
        if w:
            include_works.append(w)
        else:
            print(f"Include list: OpenAlex has no record for {doi}", file=sys.stderr)
    pubs, dropped = select(works, read_list("publications-exclude.txt"), include_works)
    set_years(pubs)
    pubs.sort(key=lambda w: (w.get("publication_year") or 0, w.get("publication_date") or ""),
              reverse=True)

    old_count = current.count("<li>")
    if old_count and len(pubs) < SHRINK_LIMIT * old_count:
        sys.exit(f"New list has {len(pubs)} papers, down from {old_count}; nothing changed")

    body = build_list(pubs, member_keys())
    old_body = re.sub(r'<p class="pub-updated">.*?</p>\s*', "", current, flags=re.S).strip()
    print(f"{len(works)} works from OpenAlex; {len(pubs)} kept; {len(dropped)} dropped.")
    for w, why in dropped:
        print(f"  dropped ({why}): {w.get('publication_year')} {w.get('display_name')}")
    if body.strip() == old_body:
        print("No change to the list.")
        return

    stamp = dt.date.today().strftime("%-d %B %Y")
    n_papers = sum(1 for w in pubs if w.get("type") not in CHAPTER_TYPES)
    new = (f'\n<p class="pub-updated">{n_papers} papers. List last changed {stamp}.</p>\n'
           f"{body}\n")
    with open(PAGE, "w", encoding="utf-8") as f:
        f.write(before + START + new + END + after)
    with open(os.path.join(DATA, "publications-report.txt"), "w", encoding="utf-8") as f:
        f.write(f"Report from the last change to the list ({stamp}).\n")
        f.write(f"{len(works)} works from OpenAlex; {len(pubs)} kept; {len(dropped)} dropped.\n\n")
        f.write("Dropped (add a DOI to publications-include.txt to restore one):\n")
        for w, why in sorted(dropped, key=lambda x: str(x[0].get("publication_year")), reverse=True):
            f.write(f"  {w.get('publication_year')}  [{why}]  {w.get('display_name')}  "
                    f"{norm_doi(w.get('doi')) or norm_id(w.get('id'))}\n")
    print("publications.html and data/publications-report.txt updated.")


if __name__ == "__main__":
    main()
