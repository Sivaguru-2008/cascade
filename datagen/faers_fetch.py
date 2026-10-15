"""Optional: fetch openFDA FAERS report counts for each known pair's index drug + reaction (section 4.7).

    python -m datagen.faers_fetch          # writes data/faers_cache.json

The system works without this file. FAERS counts are spontaneous reports, not incidence and not proof of
causation. Novacor is fictional and gets no FAERS entry. Rate-limited to ~1 request/s (openFDA allows 240/min
without a key).
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.parse
from datetime import date
from typing import Any

import httpx

from common.config import get_settings
from datagen.catalogue import load_catalogue

API = "https://api.fda.gov/drug/event.json"
# Reaction term per known pair as a MedDRA preferred term (the vocabulary FAERS uses). # VERIFY with the team.
MEDDRA_PT = {"K1": "Oedema peripheral", "K2": "Cough", "K3": "Parkinsonism", "K4": "Extrapyramidal disorder",
             "K5": "Urinary incontinence", "K6": "Dizziness", "K7": "Oedema peripheral", "K8": "Gout",
             "K9": "Blood pressure increased"}


def query_url(generic: str, reaction: str) -> str:
    search = (f'patient.drug.openfda.generic_name:"{generic}"+AND+'
              f'patient.reaction.reactionmeddrapt.exact:"{reaction}"')
    safe = '+:"'
    return f"{API}?search={urllib.parse.quote(search, safe=safe)}&limit=1"


def fetch_total(client: httpx.Client, url: str) -> int | None:
    for attempt in range(3):
        try:
            r = client.get(url, timeout=20)
            if r.status_code == 404:  # openFDA returns 404 when nothing matches
                return 0
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            return int(r.json()["meta"]["results"]["total"])
        except (httpx.HTTPError, KeyError, ValueError):
            time.sleep(2 * (attempt + 1))
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.parse_args()
    cat = load_catalogue()
    out: dict[str, Any] = {"source": API, "fetched": date.today().isoformat(),
                           "note": "spontaneous-report counts; not incidence, not causation", "pairs": []}
    with httpx.Client(headers={"User-Agent": "CASCADE-datathon-research"}) as client:
        for k in cat.known:
            reaction = MEDDRA_PT[k.pair_id]
            total, urls, failed = 0, [], False
            for name in k.index_drugs:
                generic = name.split(" and ")[0]
                url = query_url(generic, reaction)
                n = fetch_total(client, url)
                urls.append(url)
                failed |= n is None
                total += n or 0
                time.sleep(1.0)
            for a in k.index_l4:
                for b in k.marker_l4:
                    out["pairs"].append({"pair_id": k.pair_id, "index_atc_l4": a, "marker_atc_l4": b,
                                         "index_drug": "|".join(k.index_drugs), "reaction": reaction,
                                         "count": None if failed else total, "query_urls": urls})
            print(f"{k.pair_id}: {'|'.join(k.index_drugs)} + '{reaction}': {'fetch failed' if failed else total}")
    path = get_settings().data_dir / "faers_cache.json"
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
