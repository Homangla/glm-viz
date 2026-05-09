#!/usr/bin/env python3
"""
gLM 논문 자동 수집 스크립트
소스: arXiv · bioRxiv · medRxiv · PubMed (Nature Methods, Cell, Genome Research 등 모든 저널 포함)
"""
import json
import time
import re
import sys
from datetime import datetime, timedelta
from urllib.request import urlopen, Request
from urllib.parse import urlencode
from urllib.error import URLError, HTTPError
import xml.etree.ElementTree as ET

# ── 설정 ──────────────────────────────────────────────────────────────────────
DAYS_BACK = 31
MAX_PER_SOURCE = 100
OUTPUT_FILE = "papers.json"
MAX_TOTAL = 300        # 누적 보관 최대 논문 수
ABSTRACT_LEN = 450

# gLM 관련 검색어 (arXiv 제목 + 초록, PubMed Title/Abstract 대상)
SEARCH_TERMS = [
    "genomic language model",
    "genome language model",
    "DNA language model",
    "RNA language model",
    "nucleotide language model",
    "genome foundation model",
    "DNABERT",
    "HyenaDNA",
    "Nucleotide Transformer",
    "genomic transformer",
    "SpliceBERT",
    "Evo genomic",
    "Caduceus genomic",
    "protein language model genomic",
    "genomic pre-trained model",
    "DNA foundation model",
    "splicing language model",
    "CodonBERT",
    "GENA-LM",
    "genomic BERT",
]

# 태그 자동 분류 키워드
TAGS_MAP = {
    "DNA": ["dna", "dnabert", "nucleotide", "genome", "genomic", "hyenadna",
            "caduceus", "grover", "gena-lm"],
    "RNA": ["rna", "mrna", "ncrna", "lncrna", "mirna", "transcript", "codonbert", "ernie-rna"],
    "단백질": ["protein", "amino acid", "esm-", "progen", "proteinbert"],
    "스플라이싱": ["splicing", "splice", "splicebert", "pangolin", "spliceai",
                   "intron", "exon", "pre-mrna", "spliceosome"],
    "변이 효과": ["variant", "snv", "mutation", "vep", "pathogenic", "clinical", "rare variant"],
    "유전자 발현": ["gene expression", "enformer", "borzoi", "enhancer", "promoter", "eqtl"],
    "구조 예측": ["structure prediction", "esm", "esm-fold", "3d structure", "alphafold"],
    "에피게놈": ["epigenome", "chromatin", "atac", "chip-seq", "histone", "methylation"],
    "CRISPR": ["crispr", "cas9", "guide rna", "grna", "gene editing"],
    "SSM": ["mamba", "hyena", "state space model", "ssm", "stripedhydena"],
}


def log(msg):
    print(msg, flush=True)


def fetch_url(url, retries=3, delay=1.5):
    headers = {
        "User-Agent": "gLM-viz/1.0 (https://github.com/Homangla/glm-viz; research bot)",
        "Accept": "application/json, application/xml, text/xml, */*",
    }
    for attempt in range(retries):
        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except (URLError, HTTPError) as e:
            if attempt < retries - 1:
                time.sleep(delay * (attempt + 1))
            else:
                log(f"  [오류] {url[:80]}... → {e}")
                return None


def assign_tags(title, abstract=""):
    text = (title + " " + abstract).lower()
    tags = [tag for tag, kws in TAGS_MAP.items() if any(kw in text for kw in kws)]
    return tags[:5]


def normalize(title):
    return re.sub(r"\s+", " ", title).strip().lower()


def trunc(text, n=ABSTRACT_LEN):
    text = re.sub(r"\s+", " ", text).strip()
    return text[:n] + "…" if len(text) > n else text


# ── 1. arXiv ──────────────────────────────────────────────────────────────────
def fetch_arxiv(start_dt, end_dt):
    log("  → arXiv 쿼리 중…")
    results = []

    # 제목 검색
    ti_parts = " OR ".join(f'ti:"{t}"' for t in SEARCH_TERMS[:12])
    # 초록 검색 (핵심 용어)
    abs_parts = " OR ".join(f'abs:"{t}"' for t in SEARCH_TERMS[:6])
    query = f"({ti_parts}) OR ({abs_parts})"

    params = {
        "search_query": query,
        "start": 0,
        "max_results": MAX_PER_SOURCE,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    xml_text = fetch_url("http://export.arxiv.org/api/query?" + urlencode(params))
    if not xml_text:
        return results

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        log(f"  [파싱 오류] arXiv XML: {e}")
        return results

    for entry in root.findall("atom:entry", ns):
        pub_raw = entry.find("atom:published", ns)
        if pub_raw is None:
            continue
        pub_str = pub_raw.text[:10]
        pub_dt = datetime.strptime(pub_str, "%Y-%m-%d")
        if pub_dt < start_dt:
            continue

        title = (entry.find("atom:title", ns).text or "").replace("\n", " ").strip()
        abstract = (entry.find("atom:summary", ns).text or "").replace("\n", " ").strip()
        arxiv_id = (entry.find("atom:id", ns).text or "").strip()
        authors = [
            (a.find("atom:name", ns).text or "")
            for a in entry.findall("atom:author", ns)
        ]
        categories = [
            c.get("term", "")
            for c in entry.findall("atom:category", ns)
        ]

        results.append({
            "title": title,
            "authors": authors[:6],
            "abstract": trunc(abstract),
            "date": pub_str,
            "source": "arXiv",
            "journal": "arXiv (" + ", ".join(categories[:3]) + ")",
            "url": arxiv_id,
            "tags": assign_tags(title, abstract),
        })

    log(f"    arXiv: {len(results)}건 수집")
    return results


# ── 2. bioRxiv / medRxiv ──────────────────────────────────────────────────────
def fetch_biorxiv_server(server, start_dt, end_dt, keywords_lower):
    results = []
    start_str = start_dt.strftime("%Y-%m-%d")
    end_str = end_dt.strftime("%Y-%m-%d")
    cursor = 0

    while True:
        url = f"https://api.biorxiv.org/details/{server}/{start_str}/{end_str}/{cursor}/json"
        data = fetch_url(url)
        if not data:
            break

        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            break

        collection = payload.get("collection", [])
        if not collection:
            break

        for item in collection:
            title = item.get("title", "")
            abstract = item.get("abstract", "")
            text = (title + " " + abstract).lower()
            if not any(kw in text for kw in keywords_lower):
                continue

            authors_raw = item.get("authors", "")
            authors = [a.strip() for a in re.split(r"[;,]", authors_raw) if a.strip()][:6]
            doi = item.get("doi", "")

            results.append({
                "title": title,
                "authors": authors,
                "abstract": trunc(abstract),
                "date": item.get("date", ""),
                "source": server,
                "journal": f"{server.capitalize()} preprint",
                "url": f"https://doi.org/{doi}" if doi else "",
                "tags": assign_tags(title, abstract),
            })

        total = payload.get("messages", [{}])[0].get("total", 0)
        cursor += len(collection)
        if cursor >= min(int(total), MAX_PER_SOURCE):
            break
        time.sleep(0.5)

    return results


def fetch_biorxiv(start_dt, end_dt):
    log("  → bioRxiv / medRxiv 쿼리 중…")
    kws = [t.lower() for t in SEARCH_TERMS]
    results = []
    for server in ("biorxiv", "medrxiv"):
        r = fetch_biorxiv_server(server, start_dt, end_dt, kws)
        results.extend(r)
        time.sleep(0.8)
    log(f"    bioRxiv/medRxiv: {len(results)}건 수집")
    return results


# ── 3. PubMed (모든 저널 포함) ───────────────────────────────────────────────
MONTH_MAP = {
    "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04",
    "May": "05", "Jun": "06", "Jul": "07", "Aug": "08",
    "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12",
}

def pubmed_search(term, start_dt, end_dt):
    """PMIDs 검색"""
    params = {
        "db": "pubmed",
        "term": term,
        "datetype": "pdat",
        "mindate": start_dt.strftime("%Y/%m/%d"),
        "maxdate": end_dt.strftime("%Y/%m/%d"),
        "retmode": "json",
        "retmax": MAX_PER_SOURCE,
        "usehistory": "y",
    }
    data = fetch_url("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?" + urlencode(params))
    if not data:
        return []
    try:
        return json.loads(data).get("esearchresult", {}).get("idlist", [])
    except json.JSONDecodeError:
        return []


def pubmed_fetch(pmids):
    """PMIDs → 논문 메타데이터"""
    if not pmids:
        return []
    params = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "xml",
    }
    xml_text = fetch_url("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?" + urlencode(params))
    if not xml_text:
        return []

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        log(f"  [파싱 오류] PubMed XML: {e}")
        return []

    results = []
    for article in root.findall(".//PubmedArticle"):
        try:
            citation = article.find("MedlineCitation")
            art = citation.find("Article")

            # 제목
            title_el = art.find("ArticleTitle")
            title = "".join(title_el.itertext()) if title_el is not None else ""

            # 초록
            abstract_parts = []
            for ab in art.findall(".//AbstractText"):
                label = ab.get("Label", "")
                text = "".join(ab.itertext())
                abstract_parts.append(f"{label}: {text}" if label else text)
            abstract = " ".join(abstract_parts)

            # 저자
            authors = []
            for author in art.findall(".//Author")[:6]:
                last = author.findtext("LastName", "")
                fore = author.findtext("ForeName", "")
                if last:
                    authors.append(f"{last} {fore}".strip())

            # 날짜
            pub_date = art.find(".//PubDate")
            year = pub_date.findtext("Year", "") if pub_date is not None else ""
            month = pub_date.findtext("Month", "01") if pub_date is not None else "01"
            month = MONTH_MAP.get(month, month.zfill(2))
            date_str = f"{year}-{month.zfill(2)}-01" if year else ""

            # 저널
            journal = art.findtext(".//Journal/Title", "PubMed")

            # PMID & DOI
            pmid = citation.findtext("PMID", "")
            doi = ""
            for loc in art.findall(".//ELocationID"):
                if loc.get("EIdType") == "doi":
                    doi = loc.text or ""
                    break

            url = f"https://doi.org/{doi}" if doi else f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"

            results.append({
                "title": title.strip(),
                "authors": authors,
                "abstract": trunc(abstract),
                "date": date_str,
                "source": "PubMed",
                "journal": journal,
                "url": url,
                "tags": assign_tags(title, abstract),
            })
        except Exception as e:
            log(f"  [PubMed 항목 건너뜀] {e}")
            continue

    return results


def fetch_pubmed(start_dt, end_dt):
    log("  → PubMed 쿼리 중 (Nature Methods, Cell, Genome Research 등 포함)…")

    # 여러 쿼리로 나눠 검색 (PubMed 쿼리 길이 제한 대응)
    term_groups = [
        SEARCH_TERMS[:6],
        SEARCH_TERMS[6:12],
        SEARCH_TERMS[12:],
    ]

    all_pmids = set()
    for group in term_groups:
        term = " OR ".join(f'"{t}"[Title/Abstract]' for t in group)
        pmids = pubmed_search(term, start_dt, end_dt)
        all_pmids.update(pmids)
        time.sleep(0.4)

    log(f"    PubMed 검색 결과: {len(all_pmids)}건 PMID")
    if not all_pmids:
        return []

    # 50개씩 나눠 fetch
    pmid_list = list(all_pmids)
    results = []
    for i in range(0, len(pmid_list), 50):
        chunk = pmid_list[i:i+50]
        results.extend(pubmed_fetch(chunk))
        time.sleep(0.4)

    log(f"    PubMed: {len(results)}건 수집")
    return results


# ── 중복 제거 ──────────────────────────────────────────────────────────────────
def deduplicate(papers):
    seen = set()
    out = []
    for p in papers:
        key = normalize(p["title"])[:90]
        if key and key not in seen:
            seen.add(key)
            out.append(p)
    return out


# ── 메인 ───────────────────────────────────────────────────────────────────────
def main():
    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=DAYS_BACK)
    log(f"=== gLM 논문 수집 시작 ({start_dt.date()} ~ {end_dt.date()}) ===\n")

    new_papers = []

    log("[1/3] arXiv")
    new_papers += fetch_arxiv(start_dt, end_dt)
    time.sleep(1)

    log("\n[2/3] bioRxiv / medRxiv")
    new_papers += fetch_biorxiv(start_dt, end_dt)
    time.sleep(1)

    log("\n[3/3] PubMed")
    new_papers += fetch_pubmed(start_dt, end_dt)

    new_papers = deduplicate(new_papers)
    new_papers.sort(key=lambda p: p.get("date", ""), reverse=True)
    log(f"\n신규 논문 (중복 제거 후): {len(new_papers)}건")

    # 기존 papers.json 로드 후 병합
    try:
        with open(OUTPUT_FILE, encoding="utf-8") as f:
            existing = json.load(f)
        old_papers = existing.get("papers", [])
    except (FileNotFoundError, json.JSONDecodeError):
        old_papers = []

    new_keys = {normalize(p["title"])[:90] for p in new_papers}
    kept_old = [p for p in old_papers if normalize(p["title"])[:90] not in new_keys]

    merged = new_papers + kept_old
    merged = merged[:MAX_TOTAL]

    output = {
        "last_updated": end_dt.strftime("%Y-%m-%d"),
        "period_start": start_dt.strftime("%Y-%m-%d"),
        "period_end": end_dt.strftime("%Y-%m-%d"),
        "new_this_run": len(new_papers),
        "total": len(merged),
        "papers": merged,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    log(f"\n=== 완료: 총 {len(merged)}건 → {OUTPUT_FILE} 저장 ===")
    return len(new_papers)


if __name__ == "__main__":
    count = main()
    sys.exit(0)
