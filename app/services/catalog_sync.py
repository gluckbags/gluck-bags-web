"""Sync the Tienda Nube catalogue into our local mirror (headless POC, Fase 2).

Pulls products from the Tienda Nube API and upserts them into `tiendanube_products`
(see app/models/tiendanube.py), keyed by the TN product id. Products that vanished
from the store are pruned — but only when the API actually returned some products,
so a transient empty response never wipes the whole cache.

Runs inside a Flask app context (it uses `db.session`). Call it from a webhook
handler (incremental, per product — a later phase) or as a full resync job.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.factory import db
from app.models import TiendaNubeProduct

if TYPE_CHECKING:
    from app.services.tiendanube_client import TiendaNubeClient


def _after_write(tn_id: int | None = None) -> None:
    """Everything a mirror write has to trigger, in this order.

    The snapshot is written before the purge: purging first would let the CDN
    revalidate against a snapshot that is still the old one.
    """
    from app.services import catalog_cache, catalog_snapshot, cdn_cache

    catalog_cache.invalidate()
    try:
        rows = TiendaNubeProduct.query.filter_by(published=True).all()
        catalog_snapshot.save([catalog_snapshot.ProductSnapshot.from_row(row) for row in rows])
    except Exception:  # noqa: BLE001 — the mirror write already succeeded
        pass
    cdn_cache.purge_catalog(tn_id)


@dataclass
class SyncResult:
    """What a sync did — handy for logging and for the spike/admin to report."""

    created: int = 0
    updated: int = 0
    pruned: int = 0

    @property
    def total_seen(self) -> int:
        return self.created + self.updated

    def __str__(self) -> str:
        return (
            f"{self.total_seen} productos ({self.created} nuevos, "
            f"{self.updated} actualizados), {self.pruned} eliminados"
        )


def sync_products(client: "TiendaNubeClient", *, prune: bool = True) -> SyncResult:
    """Full resync: upsert every product from Tienda Nube, prune the ones gone.

    Idempotent — running it twice against an unchanged store is a no-op (only
    `updated` counts move). Commits once at the end so a mid-sync failure leaves the
    cache untouched rather than half-written.
    """
    result = SyncResult()
    seen_tn_ids: set[int] = set()

    existing = {row.tn_id: row for row in TiendaNubeProduct.query.all()}

    for payload in client.iter_products():
        tn_id = int(payload["id"])
        seen_tn_ids.add(tn_id)
        row = existing.get(tn_id)
        if row is None:
            row = TiendaNubeProduct(tn_id=tn_id)
            db.session.add(row)
            result.created += 1
        else:
            result.updated += 1
        row.apply_payload(payload)

    # Prune only when we actually saw products — guards against an API hiccup
    # returning an empty list and nuking the whole mirror.
    if prune and seen_tn_ids:
        for tn_id, row in existing.items():
            if tn_id not in seen_tn_ids:
                db.session.delete(row)
                result.pruned += 1

    db.session.commit()
    _after_write()
    return result


def upsert_product(payload: dict) -> TiendaNubeProduct:
    """Upsert a single product (for a `product/created|updated` webhook).

    Commits immediately. Returns the stored row.
    """
    tn_id = int(payload["id"])
    row = TiendaNubeProduct.query.filter_by(tn_id=tn_id).one_or_none()
    if row is None:
        row = TiendaNubeProduct(tn_id=tn_id)
        db.session.add(row)
    row.apply_payload(payload)
    db.session.commit()
    _after_write(tn_id)
    return row


def delete_product(tn_id: int) -> bool:
    """Remove a product from the mirror (for a `product/deleted` webhook).

    Returns True if a row was deleted, False if it wasn't cached.
    """
    row = TiendaNubeProduct.query.filter_by(tn_id=int(tn_id)).one_or_none()
    if row is None:
        return False
    db.session.delete(row)
    db.session.commit()
    _after_write(int(tn_id))
    return True
