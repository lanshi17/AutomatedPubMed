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
    wall_wait_s: float = field(default_factory=lambda: float(os.environ.get("PUBMED_DL_WALL_WAIT", "90")))
    wall_cooldown_s: float = field(
        default_factory=lambda: float(os.environ.get("PUBMED_DL_COOLDOWN", "180")))
    resolve_rounds: int = field(
        default_factory=lambda: int(os.environ.get("PUBMED_DL_ROUNDS", "2")))
    act_first: bool = field(
        default_factory=lambda: os.environ.get("PUBMED_DL_ACT_FIRST", "").lower() in ("1", "true", "yes"))
    act_delay_s: float = field(
        default_factory=lambda: float(os.environ.get("PUBMED_DL_ACT_DELAY", "5")))
    wall_backoff_s: float = field(
        default_factory=lambda: float(os.environ.get("PUBMED_DL_WALL_BACKOFF", "20")))
    # browser-act browser name; empty/absent disables the backend.
    browser_act: str | None = field(
        default_factory=lambda: os.environ.get("PUBMED_DL_BROWSER_ACT", "pubmed-publishers") or None)
    browser_act_session: str = "pubmed-pub"
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
