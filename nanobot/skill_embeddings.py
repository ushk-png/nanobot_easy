"""Optional embedding helpers for skill routing."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from pathlib import Path

from nanobot.config.schema import Config, ModelPresetConfig
from nanobot.providers.factory import make_provider
from nanobot.skill_store import SkillStore


def skill_embedding_enabled(config: Config) -> bool:
    embedding = config.skills.embedding
    return bool(embedding.provider and embedding.model)


def make_skill_embedding_fn(config: Config) -> tuple[Callable[[list[str]], list[list[float]]], str, int | None] | None:
    """Return a synchronous embedding function for SkillStore, or None when disabled.

    This helper is intended for CLI/reindex scripts. Runtime code that already
    runs inside an event loop should call the provider's async ``embed`` method
    directly and pass the resulting query vector into ``SkillStore.search``.
    """
    embedding = config.skills.embedding
    if not embedding.provider or not embedding.model:
        return None
    preset = ModelPresetConfig(model=embedding.model, provider=embedding.provider)
    provider = make_provider(config, preset=preset)

    def _embed(texts: list[str]) -> list[list[float]]:
        return asyncio.run(provider.embed(texts, embedding.model or ""))

    return _embed, embedding.model, embedding.dimensions


async def embed_skill_query(
    config: Config,
    text: str,
    *,
    workspace: Path | None = None,
    store: SkillStore | None = None,
) -> list[float] | None:
    embedding = config.skills.embedding
    if not embedding.provider or not embedding.model or not text.strip():
        return None
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
    active_store = store or SkillStore(workspace or config.workspace_path)
    cached = active_store.get_cached_query_vector(digest, embedding_model=embedding.model)
    if cached is not None:
        return cached
    preset = ModelPresetConfig(model=embedding.model, provider=embedding.provider)
    provider = make_provider(config, preset=preset)
    vectors = await provider.embed([text], embedding.model)
    if not vectors:
        return None
    vector = vectors[0]
    active_store.set_cached_query_vector(
        digest,
        embedding_model=embedding.model,
        vector=vector,
        embedding_dimensions=embedding.dimensions,
    )
    return vector
