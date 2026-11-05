"""Mesure la latence et le débit du serveur de modèles, à travers l'API.

Aucune dépendance : bibliothèque standard de Python uniquement. Exemple, depuis le laptop :

    python scripts/bench_llm.py --url https://pc-fixe.<tailnet>.ts.net \\
        --model mistral-small3.2:24b --model gemma4:12b --concurrency 1 4 8

Pour chaque modèle : une requête de préchauffage (chargement en mémoire GPU), puis, pour chaque
niveau de concurrence c, N requêtes identiques envoyées c par c (N = 2 × c, au moins 4).
Le résultat est un tableau Markdown, à coller dans la documentation.
"""

import argparse
import json
import statistics
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

PROMPT = (
    "Explique en français, en environ 200 mots, comment un système de RAG retrouve les "
    "passages utiles dans des documents avant de répondre à une question."
)


def one_request(url: str, model: str | None, max_tokens: int, timeout: float) -> dict:
    payload = {"prompt": PROMPT, "max_tokens": max_tokens}
    if model:
        payload["model"] = model
    request = urllib.request.Request(
        f"{url.rstrip('/')}/llm/generate",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": f"HTTP {exc.code} : {exc.read().decode(errors='replace')}"}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    latency = time.perf_counter() - start
    return {"ok": True, "latency": latency, "tokens": body.get("completion_tokens", 0)}


def percentile(values: list[float], p: int) -> float:
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[p - 1]


def run_level(args: argparse.Namespace, model: str | None, concurrency: int) -> dict:
    n = args.requests or max(4, 2 * concurrency)
    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(
            pool.map(
                lambda _: one_request(args.url, model, args.max_tokens, args.timeout), range(n)
            )
        )
    wall = time.perf_counter() - start
    ok = [r for r in results if r["ok"]]
    errors = [r["error"] for r in results if not r["ok"]]
    row = {"model": model or "défaut de l'API", "concurrency": concurrency, "n": n}
    row["errors"] = len(errors)
    row["first_error"] = errors[0] if errors else ""
    if ok:
        latencies = [r["latency"] for r in ok]
        row["p50"] = statistics.median(latencies)
        row["p95"] = percentile(latencies, 95)
        row["per_request"] = statistics.mean(r["tokens"] / r["latency"] for r in ok)
        row["total"] = sum(r["tokens"] for r in ok) / wall
    return row


def print_table(rows: list[dict]) -> None:
    print()
    print(
        "| Modèle | Concurrence | Requêtes | Erreurs | Latence p50 (s) | Latence p95 (s) "
        "| Débit par requête (tok/s) | Débit total (tok/s) |"
    )
    print("|---|---|---|---|---|---|---|---|")
    for row in rows:
        if "p50" in row:
            latency = f"{row['p50']:.1f} | {row['p95']:.1f}"
            measures = f"{latency} | {row['per_request']:.0f} | {row['total']:.0f}"
        else:
            measures = "– | – | – | –"
        print(
            f"| {row['model']} | {row['concurrency']} | {row['n']} | {row['errors']} | {measures} |"
        )
    for row in rows:
        if row["first_error"]:
            print(f"\nPremière erreur ({row['model']}, concurrence {row['concurrency']}) :")
            print(f"  {row['first_error']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://localhost:8000", help="adresse de l'API")
    parser.add_argument(
        "--model",
        action="append",
        help="modèle à mesurer (option répétable ; défaut : celui de l'API)",
    )
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 4, 8])
    parser.add_argument(
        "--requests", type=int, help="requêtes par niveau (défaut : 2 × concurrence)"
    )
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--timeout", type=float, default=600.0, help="délai max par requête (s)")
    args = parser.parse_args()

    rows = []
    for model in args.model or [None]:
        label = model or "défaut de l'API"
        warm = one_request(args.url, model, 16, args.timeout)
        if not warm["ok"]:
            print(f"{label} : échec du préchauffage — {warm['error']}")
            continue
        print(f"{label} : préchauffage en {warm['latency']:.1f} s")
        for concurrency in args.concurrency:
            row = run_level(args, model, concurrency)
            rows.append(row)
            print(f"  concurrence {concurrency} : {row['n']} requêtes, {row['errors']} erreur(s)")
    if rows:
        print_table(rows)


if __name__ == "__main__":
    main()
