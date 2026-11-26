"""Optional Prodata integration - NOT implemented yet.

The stock app is fully usable without it. This module is the single place where
Prodata will plug in, once we have its API documentation (base URL, auth method,
and the endpoints/fields for items and stock documents).

Configure with environment variables:
    VINEX_PRODATA_URL   e.g. https://server/prodata/api
    VINEX_PRODATA_KEY   API key / token

Planned directions (to confirm against the real API):
    pull  - products and prices from Prodata  -> products table (match on prodata_code / sku)
    push  - stock movements (movements.prodata_synced = 0) -> Prodata stock documents,
            then mark them synced = 1
"""
import os


class ProdataNotConfigured(Exception):
    pass


def _config():
    return os.environ.get("VINEX_PRODATA_URL", "").strip(), os.environ.get("VINEX_PRODATA_KEY", "").strip()


def status():
    url, key = _config()
    return {"configured": bool(url and key), "implemented": False}


def sync(db):
    url, key = _config()
    if not (url and key):
        raise ProdataNotConfigured("Prodata nuk është konfiguruar. Vendosni VINEX_PRODATA_URL dhe VINEX_PRODATA_KEY.")
    raise ProdataNotConfigured("Sinkronizimi me Prodata nuk është implementuar ende - nevojitet dokumentacioni i API-së së Prodata.")
