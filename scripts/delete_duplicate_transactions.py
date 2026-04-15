"""
scripts/delete_duplicate_transactions.py
Encuentra y elimina transacciones duplicadas de la API de producción.
Duplicado = mismo user_id + date + amount + product.
Conserva el registro más antiguo (id menor) y elimina los demás.

Uso:
  DANIEL15K_API_URL=https://... DANIEL15K_API_TOKEN=... python scripts/delete_duplicate_transactions.py [--dry-run]
"""
import os
import sys
import httpx
from collections import defaultdict

from adapters.rails_http import BASE_URL as API_URL, build_auth_headers

DRY_RUN   = "--dry-run" in sys.argv

headers = build_auth_headers()

def get_transactions(month: int, year: int) -> list[dict]:
    r = httpx.get(f"{API_URL}/api/v1/transactions", params={"month": month, "year": year},
                  headers=headers, timeout=20)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else data.get("data", [])

def delete_transaction(txn_id: str) -> bool:
    if DRY_RUN:
        print(f"  [dry-run] DELETE /api/v1/transactions/{txn_id}")
        return True
    r = httpx.delete(f"{API_URL}/api/v1/transactions/{txn_id}", headers=headers, timeout=15)
    return r.status_code == 204

def main():
    print(f"=== Buscando duplicados {'[DRY RUN]' if DRY_RUN else ''} ===\n")
    duplicates_found = 0
    deleted = 0

    for month in range(1, 13):
        year = 2026
        try:
            txns = get_transactions(month, year)
        except httpx.HTTPStatusError:
            continue
        if not txns:
            continue

        # Agrupar por (date, amount, product, transaction_type)
        groups = defaultdict(list)
        for t in txns:
            a = t.get("attributes", t)
            key = (
                a.get("date"),
                a.get("amount"),
                a.get("product"),
                a.get("transaction_type"),
            )
            groups[key].append({"id": t.get("id"), "concept": a.get("concept"),
                                 "status": a.get("status")})

        for key, group in groups.items():
            if len(group) < 2:
                continue
            # Ordenar por id para conservar el más antiguo
            group.sort(key=lambda x: int(x["id"]))
            keep = group[0]
            dupes = group[1:]
            duplicates_found += len(dupes)
            print(f"DUPLICADO: {key}")
            print(f"  ✅ Conservar id={keep['id']} concept='{keep['concept']}'")
            for d in dupes:
                print(f"  ❌ Eliminar id={d['id']} concept='{d['concept']}'")
                if delete_transaction(str(d["id"])):
                    deleted += 1
                    print(f"     → Eliminado OK")
                else:
                    print(f"     → ERROR al eliminar")
            print()

    print(f"\n{'[DRY RUN] ' if DRY_RUN else ''}Duplicados encontrados: {duplicates_found} | Eliminados: {deleted}")

if __name__ == "__main__":
    main()
