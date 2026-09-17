from dataclasses import dataclass
import os
from pathlib import Path
import sys


@dataclass(frozen=True)
class Config:
    data_dir: Path
    download_dir: Path
    index_workers: int = 3
    download_workers: int = 2
    request_delay: float = 0.4
    connect_timeout: float = 6
    read_timeout: float = 35
    page_size: int = 150
    testing: bool = False

    @classmethod
    def from_env(cls):
        base = Path.home() / ("Library/Application Support" if sys.platform == "darwin" else ".local/share")
        return cls(
            data_dir=Path(os.environ.get("OPENSHELF_DATA_DIR", base / "OpenShelf")).expanduser().resolve(),
            download_dir=Path(os.environ.get("OPENSHELF_DOWNLOAD_DIR", Path.home() / "Downloads/OpenShelf")).expanduser().resolve(),
        )

    def prepare(self):
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in ("covers", "partial"):
            (self.data_dir / name).mkdir(exist_ok=True, mode=0o700)
        self.download_dir.mkdir(parents=True, exist_ok=True)

    @property
    def database(self):
        return self.data_dir / "openshelf.sqlite"
