"""Lazy CPU-only provider. Normal calls are strictly local_files_only."""
import hashlib
import json
import math
from threading import RLock
from typing import Protocol

from .diagnostics import record_exception

QUERY_PREFIX = "task: search result | query: "
DOCUMENT_FORMAT = "title: {title} | text: {content}"
# MCP runs sync tools in worker threads. Serialize dependency imports and lazy
# construction across providers; Python's import lock does not protect their
# mutable lazy-module attributes or our model/tokenizer caches.
_INITIALIZATION_LOCK = RLock()


class EmbeddingInitializationError(RuntimeError):
    """The original exception is retained as __cause__ and in private diagnostics."""


def document_input(title, content):
    return DOCUMENT_FORMAT.format(title=title or "none", content=content)


def validate_vector(vector, dimensions):
    values = [float(v) for v in vector]
    if len(values) != dimensions or not all(math.isfinite(v) for v in values):
        raise ValueError("Embedding has invalid dimensions or non-finite values")
    norm = math.sqrt(sum(v * v for v in values))
    if abs(norm - 1) > 1e-4:
        raise ValueError("Embedding must have unit L2 norm")
    return values


class EmbeddingProvider(Protocol):
    def embed_documents(self, items): ...
    def embed_query(self, query): ...
    def count_tokens(self, text): ...
    def identity(self): ...


class GemmaProvider:
    def __init__(self, config):
        self.config = config
        self._model = None
        self._tokenizer = None

    def _snapshot(self):
        if not self.config.model_revision:
            raise ValueError("No pinned model revision. Run knowledge prefetch before indexing/search.")
        from huggingface_hub import snapshot_download
        try:
            return snapshot_download(self.config.model_id, revision=self.config.model_revision,
                                     cache_dir=self.config.cache_dir, local_files_only=True)
        except OSError as e:
            raise ValueError("Model unavailable offline; run knowledge prefetch with network access") from e

    @property
    def tokenizer(self):
        with _INITIALIZATION_LOCK:
            if self._tokenizer is None:
                try:
                    # Import the defining module directly so lazy export lookup
                    # cannot obscure an underlying dependency traceback.
                    from transformers.models.auto.tokenization_auto import AutoTokenizer
                    self._tokenizer = AutoTokenizer.from_pretrained(self._snapshot(), local_files_only=True, trust_remote_code=False)
                except Exception as error:
                    self._initialization_failed("tokenizer", error)
            return self._tokenizer

    def _initialization_failed(self, component, error):
        record_exception(f"Embedding {component} initialization", error)
        raise EmbeddingInitializationError(
            f"Embedding {component} initialization failed ({type(error).__name__}); "
            "see local-knowledge diagnostics/initialization.log in the XDG state directory."
        ) from error

    def initialize(self):
        """Check production tokenizer, weights and inference before serving searches.

        Failed objects are never cached. A later explicit search can try again;
        there is no hidden retry, fallback model or fabricated empty result.
        """
        try:
            self.embed_query("startup health check")
        except EmbeddingInitializationError:
            raise
        except Exception as error:
            self._initialization_failed("inference", error)

    def count_tokens(self, text):
        return len(self.tokenizer.encode(text, add_special_tokens=True, truncation=False))

    @property
    def model(self):
        with _INITIALIZATION_LOCK:
            try:
                return self._load_model()
            except Exception as error:
                self._initialization_failed("model", error)

    def _load_model(self):
        if self._model is None:
            import torch
            from sentence_transformers import SentenceTransformer
            torch.set_num_threads(self.config.threads)
            model = SentenceTransformer(
                self._snapshot(), device="cpu", local_files_only=True, trust_remote_code=False,
                config_kwargs={"vision_config": None, "audio_config": None},
                model_kwargs={"torch_dtype": torch.float32, "use_safetensors": True},
            )
            if model.max_seq_length < 8192:
                raise ValueError("Unexpected model token limit; verify installed library compatibility")
            if model.prompts.get("SearchQuery") != QUERY_PREFIX:
                raise ValueError("Model SearchQuery prompt is incompatible")
            self._model = model
        return self._model

    def _encode(self, texts, query=False):
        for text in texts:
            if self.count_tokens(QUERY_PREFIX + text if query else text) > 8192:
                raise ValueError("Embedding input exceeds 8192 tokens; content was not truncated")
        kwargs = {"prompt_name": "SearchQuery"} if query else {"prompt": ""}
        vectors = self.model.encode(texts, batch_size=self.config.batch_size,
                                    normalize_embeddings=True, truncate_dim=self.config.dimensions,
                                    show_progress_bar=False, **kwargs)
        return [validate_vector(v, self.config.dimensions) for v in vectors]

    def embed_documents(self, items):
        return self._encode([document_input(title, text) for title, text in items])

    def embed_query(self, query):
        return self._encode([query], query=True)[0]

    def get_dimensions(self):
        return self.config.dimensions

    def identity(self):
        # Include tokenizer runtime and immutable snapshot identity. Query identity is
        # checked separately so query-only changes do not require document rebuilds.
        from importlib.metadata import version
        return {"model_id": self.config.model_id, "revision": self.config.model_revision,
                "dimensions": self.config.dimensions, "precision": "float32", "normalize": True,
                "document_format": DOCUMENT_FORMAT, "tokenizer_revision": self.config.model_revision,
                "transformers": version("transformers"), "sentence_transformers": version("sentence-transformers"),
                "special_tokens": True, "truncation": False}

    def get_configuration_id(self):
        return hashlib.sha256(json.dumps(self.identity(), sort_keys=True).encode()).hexdigest()


def prefetch(config):
    from huggingface_hub import HfApi, snapshot_download
    revision = config.model_revision or HfApi().model_info(config.model_id).sha
    snapshot_download(config.model_id, revision=revision, cache_dir=config.cache_dir,
                      ignore_patterns=["*.bin", "*.h5", "*.msgpack", "onnx/*", "openvino/*"])
    config.model_revision = revision
    return revision
