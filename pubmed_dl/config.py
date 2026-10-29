import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    out_dir: Path
    csv_path: Path | None = None
    profile_dir: Path = field(default_factory=lambda: Path("/tmp/pubmed_dl_chrome"))
    port: int = 9222
    display: str = ":99"
    interactive: bool = False
    unpaywall_email: str = "yangzs@csu.edu.cn"
    min_pdf_bytes: int = 20_000
    delay_range: tuple[float, float] = (0.8, 2.0)
    timeout_ms: int = 45_000
    shard: int = 0
    shards: int = 1
    attempt_walled: bool = False
    use_pmc: bool = False
    oa_first: bool = False
    sd_pace: float = field(default_factory=lambda: float(os.environ.get("PUBMED_DL_SD_PACE", "20")))
    proxy_url: str | None = field(default_factory=lambda: os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"))
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
    )

    def __post_init__(self):
        self.out_dir = Path(self.out_dir).expanduser().resolve()
        for sub in ("pdf", "tmp"):
            (self.out_dir / sub).mkdir(parents=True, exist_ok=True)

    @property
    def state_path(self) -> Path:
        if self.shards > 1:
            return self.out_dir / f"state.shard{self.shard}.jsonl"
        return self.out_dir / "state.jsonl"

    @property
    def index_csv(self) -> Path:
        return self.out_dir / "index.csv"

    @property
    def bib_path(self) -> Path:
        return self.out_dir / "references.bib"

    @property
    def log_path(self) -> Path:
        return self.out_dir / "run.log"


def env_defaults() -> dict:
    return {
        "display": os.environ.get("PUBMED_DL_DISPLAY", ":99"),
        "port": int(os.environ.get("PUBMED_DL_PORT", "9222")),
    }
