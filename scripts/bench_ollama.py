"""Mesure directe du serveur de modèles (sans passer par l'API), en streaming.

Complète scripts/bench_llm.py : même méthode (préchauffage, puis N = max(4, 2 × c) requêtes
envoyées c par c, réponses de 256 tokens), mais mesure aussi le temps avant le premier mot de
réponse et sépare réponse et raisonnement (modèles à raisonnement comme gemma4).

Depuis le laptop, Ollama du PC fixe relayé par Tailscale (pas de jeton : tailnet seulement) :

    uv run scripts/bench_ollama.py --url https://pc-fixe.<tailnet>.ts.net:11434/v1 \\
        --model mistral-small3.2:24b --model gemma4:12b --concurrency 1 4 8

--with-reasoning ajoute, pour chaque modèle, une série sans reasoning_effort="none".
Le résultat est un tableau Markdown, à coller dans docs/benchmarks/.
"""

import argparse
import asyncio
import statistics
import sys
import time

from openai import AsyncOpenAI

PROMPT = (
    "Explique en français, en environ 200 mots, comment un système de RAG retrouve les "
    "passages utiles dans des documents avant de répondre à une question."
)


async def one(client: AsyncOpenAI, model: str, extra: dict, max_tokens: int) -> dict:
    start = time.perf_counter()
    first_word = None
    content = reasoning = tokens = 0
    stream = await client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": PROMPT}],
        max_tokens=max_tokens,
        temperature=0,
        stream=True,
        stream_options={"include_usage": True},
        **extra,
    )
    async for chunk in stream:
        if chunk.choices:
            delta = chunk.choices[0].delta
            if delta.content:
                if first_word is None:
                    first_word = time.perf_counter() - start
                content += 1
            if (delta.model_extra or {}).get("reasoning"):
                reasoning += 1
        if chunk.usage:
            tokens = chunk.usage.completion_tokens
    return {
        "latency": time.perf_counter() - start,
        "first_word": first_word,
        "tokens": tokens,
        "content": content,
        "reasoning": reasoning,
    }


def percentile(values: list[float], p: int) -> float:
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[p - 1]


async def run_level(client, model, extra, concurrency, max_tokens) -> dict:
    n = max(4, 2 * concurrency)
    slots = asyncio.Semaphore(concurrency)

    async def limited():
        async with slots:
            return await one(client, model, extra, max_tokens)

    start = time.perf_counter()
    results = await asyncio.gather(*(limited() for _ in range(n)), return_exceptions=True)
    wall = time.perf_counter() - start
    ok = [r for r in results if isinstance(r, dict)]
    errors = [r for r in results if not isinstance(r, dict)]
    row = {"concurrency": concurrency, "n": n, "errors": len(errors)}
    if errors:
        row["first_error"] = repr(errors[0])[:200]
    if ok:
        latencies = [r["latency"] for r in ok]
        first_words = [r["first_word"] for r in ok if r["first_word"] is not None]
        row |= {
            "p50": statistics.median(latencies),
            "p95": percentile(latencies, 95),
            "first_word": statistics.median(first_words) if first_words else None,
            "per_request": statistics.mean(r["tokens"] / r["latency"] for r in ok),
            "total": sum(r["tokens"] for r in ok) / wall,
            "content": statistics.mean(r["content"] for r in ok),
            "reasoning": statistics.mean(r["reasoning"] for r in ok),
        }
    return row


async def main(args: argparse.Namespace) -> None:
    client = AsyncOpenAI(base_url=args.url, api_key="ollama", timeout=args.timeout, max_retries=0)
    series = [(m, {"reasoning_effort": "none"}, "none") for m in args.model]
    if args.with_reasoning:
        series += [(m, {}, "défaut") for m in args.model]
    rows = []
    for model, extra, label in series:
        start = time.perf_counter()
        await one(client, model, extra, args.max_tokens)  # préchauffage : chargement en VRAM
        print(f"{model} [{label}] : préchauffage en {time.perf_counter() - start:.1f} s")
        for c in args.concurrency:
            row = await run_level(client, model, extra, c, args.max_tokens)
            rows.append({"model": model, "label": label, **row})
            print(f"    concurrence {c} : {row['n']} requêtes, {row['errors']} erreur(s)")
            if row.get("first_error"):
                print(f"      première erreur : {row['first_error']}")
    print()
    print(
        "| Modèle | reasoning_effort | Concurrence | Latence p50 (s) | Latence p95 (s) |"
        " 1er mot p50 (s) | Débit par requête (tok/s) | Débit total (tok/s) |"
        " Morceaux de réponse | Morceaux de raisonnement |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        if "p50" not in r:
            print(f"| {r['model']} | {r['label']} | {r['concurrency']} | échec | | | | | | |")
            continue
        first = f"{r['first_word']:.2f}" if r["first_word"] is not None else "aucun mot"
        print(
            f"| {r['model']} | {r['label']} | {r['concurrency']} | {r['p50']:.1f} |"
            f" {r['p95']:.1f} | {first} | {r['per_request']:.0f} | {r['total']:.0f} |"
            f" {r['content']:.0f} | {r['reasoning']:.0f} |"
        )


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # PowerShell 5.1 : accents corrects
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--url", required=True, help="adresse OpenAI d'Ollama (…:11434/v1)")
    parser.add_argument("--model", action="append", required=True, help="répétable")
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 4, 8])
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--timeout", type=float, default=600.0, help="délai max par requête (s)")
    parser.add_argument(
        "--with-reasoning", action="store_true", help="ajoute une série sans reasoning_effort"
    )
    asyncio.run(main(parser.parse_args()))
