"""A plain, detached view of the mirrored catalogue.

One type, two uses:

- it is what the in-process cache holds, because SQLAlchemy rows expire the moment
  their session closes and blow up when read from the next request;
- it is what gets written to the Blob store after every mirror write, so the storefront
  can still serve products when Postgres refuses connections (see the quota outage of
  2026-09-22).

`StorefrontProduct` reads its row through plain attribute access, so a snapshot stands
in for a row with no change to the adapter.
"""

from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from app.models import TiendaNubeProduct

SNAPSHOT_PATH = "snapshots/catalog.json"
SNAPSHOT_VERSION = 1


@dataclass
class ProductSnapshot:
    """Everything `StorefrontProduct` reads off a `TiendaNubeProduct` row."""

    tn_id: int
    name: str
    description: Optional[str] = None
    category: Optional[str] = None
    published: bool = True
    canonical_url: Optional[str] = None
    price: Optional[str] = None
    currency: Optional[str] = None
    stock: Optional[int] = None
    handle: Optional[str] = None
    variants: list[dict[str, Any]] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    updated_at: Optional[datetime] = None

    @property
    def in_stock(self) -> bool:
        """Same rule as the model: unmanaged stock means unlimited, not sold out."""
        return self.stock is None or self.stock > 0

    @classmethod
    def from_row(cls, row: TiendaNubeProduct) -> "ProductSnapshot":
        # The JSON columns come back as SQLAlchemy's mutable wrappers, which stay bound
        # to the instance; copying cuts that tie before the row is handed across
        # requests.
        return cls(
            tn_id=row.tn_id,
            name=row.name,
            description=row.description,
            category=row.category,
            published=bool(row.published),
            canonical_url=row.canonical_url,
            price=row.price,
            currency=row.currency,
            stock=row.stock,
            handle=row.handle,
            variants=copy.deepcopy(row.variants) if row.variants else [],
            images=copy.deepcopy(row.images) if row.images else [],
            raw=copy.deepcopy(row.raw) if isinstance(row.raw, dict) else {},
            updated_at=row.updated_at,
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["updated_at"] = self.updated_at.isoformat() if self.updated_at else None
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProductSnapshot":
        raw_updated = data.get("updated_at")
        updated_at: Optional[datetime] = None
        if raw_updated:
            try:
                updated_at = datetime.fromisoformat(raw_updated)
            except ValueError:
                updated_at = None
        return cls(
            tn_id=int(data["tn_id"]),
            name=data.get("name") or "",
            description=data.get("description"),
            category=data.get("category"),
            published=bool(data.get("published", True)),
            canonical_url=data.get("canonical_url"),
            price=data.get("price"),
            currency=data.get("currency"),
            stock=data.get("stock"),
            handle=data.get("handle"),
            variants=data.get("variants") or [],
            images=data.get("images") or [],
            raw=data.get("raw") or {},
            updated_at=updated_at,
        )


def serialize(snapshots: list[ProductSnapshot]) -> bytes:
    document = {
        "version": SNAPSHOT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(snapshots),
        "products": [snap.to_dict() for snap in snapshots],
    }
    return json.dumps(document, ensure_ascii=False).encode("utf-8")


def deserialize(data: bytes) -> list[ProductSnapshot]:
    document = json.loads(data.decode("utf-8"))
    if int(document.get("version", 0)) != SNAPSHOT_VERSION:
        return []
    return [ProductSnapshot.from_dict(item) for item in document.get("products", [])]


def save(snapshots: list[ProductSnapshot]) -> bool:
    """Write the snapshot to the media store. Never raises: it is a backup, not a write
    anybody is waiting on."""
    from app.services import media_store

    try:
        media_store.get_store().put(SNAPSHOT_PATH, serialize(snapshots), "application/json")
        return True
    except Exception as exc:  # noqa: BLE001 — a missing backup must not fail a sync
        _log_warning("Catalog snapshot could not be written: %s", exc)
        return False


def load() -> list[ProductSnapshot]:
    """Read the last snapshot back, or an empty list if there is none."""
    from app.services import media_store

    try:
        with media_store.get_store().open(SNAPSHOT_PATH) as handle:
            return deserialize(handle.read())
    except Exception as exc:  # noqa: BLE001 — falling back is already the sad path
        _log_warning("Catalog snapshot could not be read: %s", exc)
        return []


def _log_warning(message: str, *args: object) -> None:
    from flask import current_app

    try:
        current_app.logger.warning(message, *args)
    except RuntimeError:
        pass
