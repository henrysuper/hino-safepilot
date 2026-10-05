"""Content-addressed context cache that never embeds HINO identifiers in paths."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class ContextCache:
    def __init__(self, root: Path):
        self.root = Path(root)

    @staticmethod
    def _key(provider: str, dataset: str, request_identity: str) -> str:
        material = json.dumps(
            [provider, dataset, request_identity], ensure_ascii=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    def path_for(self, provider: str, dataset: str, request_identity: str) -> Path:
        safe_provider = "".join(c for c in provider.lower() if c.isalnum() or c in "-_")
        if not safe_provider:
            raise ValueError("provider must contain a safe character")
        return self.root / safe_provider / f"{self._key(provider, dataset, request_identity)}.json"

    def put_json(
        self, provider: str, dataset: str, request_identity: str, payload: Any
    ) -> Path:
        path = self.path_for(provider, dataset, request_identity)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        return path

    def get_json(self, provider: str, dataset: str, request_identity: str) -> Any | None:
        path = self.path_for(provider, dataset, request_identity)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
