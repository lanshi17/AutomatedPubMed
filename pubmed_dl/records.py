"""Input record handling: PubMed CSV export -> normalized records."""
import csv
import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

csv.field_size_limit(10_000_000)


@dataclass
class Record:
    pmid: str
    doi: str
    pmcid: str
    title: str
    authors: str
    citation: str
    journal: str
    year: str
    first_author: str

    @property
    def key(self) -> str:
        """Stable identifier for the state file."""
        base = self.doi.lower().strip() or self.pmid or self.title
        return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]

    @property
    def prefix(self) -> str:
        return self.doi.split("/")[0].lower() if "/" in self.doi else ""

    def filename(self) -> str:
        j = _slug(self.journal) or "unknown"
        j = re.sub(r"[^A-Za-z0-9]+", "", j) or "journal"
        y = re.sub(r"\D", "", self.year) or "0000"
        a = _slug(self.first_author).split("-")[0] if self.first_author else "na"
        a = re.sub(r"[^A-Za-z0-9]+", "", a) or "na"
        ident = self.pmid or self.key
        return f"{y}_{j[:40]}_{a[:20]}_{ident}.pdf"


def _slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = s.encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")
    return s.lower()


def load_csv(path: str | Path) -> list[Record]:
    path = Path(path)
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        out = []
        for row in reader:
            rec = Record(
                pmid=(row.get("PMID") or "").strip(),
                doi=(row.get("DOI") or "").strip(),
                pmcid=(row.get("PMCID") or "").strip(),
                title=(row.get("Title") or "").strip(),
                authors=(row.get("Authors") or "").strip(),
                citation=(row.get("Citation") or "").strip(),
                journal=(row.get("Journal/Book") or "").strip(),
                year=(row.get("Publication Year") or "").strip(),
                first_author=(row.get("First Author") or "").strip(),
            )
            if rec.pmid or rec.doi:
                out.append(rec)
        return out


def normalize_title(t: str) -> str:
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode()
    t = t.lower()
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def title_similarity(a: str, b: str) -> float:
    ta, tb = set(normalize_title(a).split()), set(normalize_title(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))
