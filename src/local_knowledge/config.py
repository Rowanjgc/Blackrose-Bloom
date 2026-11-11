from dataclasses import dataclass, fields
from pathlib import Path
import os
import re
import tomllib


def config_path():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "local-knowledge/config.toml"


@dataclass
class Config:
    data_dir: str = ""
    database_path: str = ""
    cache_dir: str = ""
    model_id: str = "google/embeddinggemma-2"
    model_revision: str = "914f7f89142e33e77833254d9c9b90c3cef7303b"
    dimensions: int = 768
    device: str = "cpu"
    threads: int = 4
    batch_size: int = 8
    chunk_tokens: int = 500
    chunk_overlap: int = 75
    vector_candidates: int = 40
    keyword_candidates: int = 40
    rrf_constant: int = 60
    default_limit: int = 5
    max_output_chars: int = 24000
    search_snippet_chars: int = 1200
    max_file_bytes: int = 20_000_000
    logging_level: str = "WARNING"

    def __post_init__(self):
        self.data_dir = str(Path(self.data_dir or Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "local-knowledge").expanduser().absolute())
        self.database_path = str(Path(self.database_path or Path(self.data_dir) / "knowledge.db").expanduser().absolute())
        self.cache_dir = str(Path(self.cache_dir or Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "local-knowledge").expanduser().absolute())
        if self.model_id != "google/embeddinggemma-2":
            raise ValueError("v1 supports google/embeddinggemma-2 only")
        if self.model_revision and not re.fullmatch(r"[0-9a-f]{40}", self.model_revision):
            raise ValueError("model_revision must be an immutable 40-character commit SHA")
        if self.device != "cpu":
            raise ValueError("v1 supports CPU FP32 only")
        if self.dimensions not in (128, 256, 512, 768):
            raise ValueError("dimensions must be 128, 256, 512, or 768")
        if not 32 <= self.chunk_tokens <= 2048 or not 0 <= self.chunk_overlap < self.chunk_tokens // 2:
            raise ValueError("chunk_tokens must be 32..2048; overlap must be less than half")
        for name in ("threads", "batch_size", "vector_candidates", "keyword_candidates", "rrf_constant", "default_limit", "max_output_chars", "max_file_bytes"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if max(self.vector_candidates, self.keyword_candidates) > 1000 or self.default_limit > 50:
            raise ValueError("candidate pools max 1000; result limit max 50")
        if not 1024 <= self.max_output_chars <= 1_000_000:
            raise ValueError("max_output_chars must be 1024..1000000")
        if type(self.search_snippet_chars) is not int or not (self.search_snippet_chars == 0 or 300 <= self.search_snippet_chars <= 100000):
            raise ValueError("search_snippet_chars must be 0 (full chunks) or 300..100000")
        if self.logging_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise ValueError("invalid logging_level")

    @classmethod
    def load(cls, path=None):
        path = Path(path or config_path())
        if not path.exists():
            return cls()
        values = tomllib.loads(path.read_text())
        if set(values) - {f.name for f in fields(cls)}:
            raise ValueError("Unknown configuration keys")
        return cls(**values)

    def save(self, path):
        import json
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(f"{f.name} = {json.dumps(getattr(self, f.name))}" for f in fields(self)) + "\n")
