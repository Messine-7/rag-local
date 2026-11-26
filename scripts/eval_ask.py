"""Évalue POST /ask sur un jeu de questions : bons passages retrouvés, réponses justes.

Sert à choisir le modèle par défaut et à juger les réglages (reformulation, nombre de
passages, découpage). Aucune dépendance : bibliothèque standard de Python uniquement.

Questions : un fichier CSV séparé par « ; » (eval/questions.csv, colonnes ci-dessous, facile
à tenir dans un tableur) ou TOML (modèle : eval/questions.example.toml). Les vrais fichiers et
les résultats (eval/results/) ne sont pas versionnés : ils citent le contenu des documents, et
le repo est public.

Colonnes du CSV (en-tête obligatoire, ordre libre) :
  id, question            obligatoires
  reference_answer        réponse de référence, pour le juge
  source_document         fichier(s) attendu(s) parmi les extraits, séparés par « | »
  pages                   pages de la réponse, séparées par « , »
  question_type           « hors-corpus » : la réponse n'est pas dans les documents
  difficulty              facile, moyenne, difficile… (résultats ventilés)
  expected_keywords       mots-clés attendus dans la réponse, séparés par « | »
  follow_up_of            facultatif : id de la question dont celle-ci est la suite
  collections             facultatif : limiter la recherche, séparées par « | »

Depuis le laptop, avec la clé API d'un compte qui lit les collections concernées :

    RAG_TOKEN=rag_k_... python scripts/eval_ask.py --url https://pc-fixe.<tailnet>.ts.net \\
        --model mistral-small3.2:24b --model gemma4:12b --judge-model mistral-small3.2:24b

Pour chaque modèle, chaque question est posée dans une nouvelle conversation, suivie de ses
questions de suivi. Toutes les réponses sont recueillies, puis jugées à la fin (un seul
changement de modèle dans la VRAM). Mesures :
- recherche : le document attendu est-il parmi les extraits, en tête, à la bonne page ?
- citations : la réponse cite-t-elle (par [n]) un extrait du document attendu ?
- réponse : un modèle juge la compare à la réponse de référence (correct, partiel, faux) ;
  pour une question hors documents, il vérifie que l'assistant dit ne pas savoir au lieu
  d'inventer ; mots-clés attendus présents tels quels (sans juge) ;
- temps de réponse (médiane).
Sortie : un tableau Markdown par modèle, les échecs, et le détail en JSONL dans eval/results/.
Les conversations créées sont supprimées ensuite (--keep pour les garder).
"""

import argparse
import csv
import io
import json
import os
import re
import statistics
import sys
import time
import tomllib
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
# Début de la réponse fixe de l'API quand aucun passage n'est accessible (app.conversations)
NO_PASSAGE = "Je n'ai trouvé aucun passage pertinent"
VERDICTS = ("correct", "partiel", "faux")


# --- Jeu de questions -------------------------------------------------------------------


@dataclass(frozen=True)
class Turn:
    question: str
    documents: tuple[str, ...]  # noms de fichiers attendus parmi les extraits (l'un d'eux)
    pages: tuple[int, ...]  # pages attendues dans ces documents (facultatif)
    facts: tuple[str, ...]  # éléments attendus dans la réponse, jugés par le modèle juge
    answerable: bool  # false : la réponse n'est pas dans les documents
    collections: tuple[str, ...] | None
    reference: str = ""  # réponse de référence, pour le juge
    keywords: tuple[str, ...] = ()  # à trouver tels quels dans la réponse
    qtype: str = ""  # type de question (factuelle, liste…), pour ventiler les résultats
    difficulty: str = ""


@dataclass(frozen=True)
class Case:
    id: str
    turns: tuple[Turn, ...]  # la question, puis ses questions de suivi


def _turn(raw: dict[str, Any], collections: Any, where: str) -> Turn:
    question = str(raw.get("question", "")).strip()
    if not question:
        raise ValueError(f"{where} : « question » manquante")
    turn = Turn(
        question=question,
        documents=tuple(raw.get("documents", [])),
        pages=tuple(int(p) for p in raw.get("pages", [])),
        facts=tuple(raw.get("facts", [])),
        answerable=bool(raw.get("answerable", True)),
        collections=tuple(collections) if collections else None,
    )
    _check(turn, where)
    return turn


def _check(turn: Turn, where: str) -> None:
    if turn.answerable and not (turn.documents or turn.facts or turn.reference or turn.keywords):
        raise ValueError(
            f"{where} : indiquer documents, éléments ou réponse attendus (ou hors documents)"
        )


def _split(value: str | None, sep: str = "|") -> tuple[str, ...]:
    return tuple(v.strip() for v in (value or "").split(sep) if v.strip())


def load_cases_csv(text: str) -> list[Case]:
    """CSV séparé par « ; » (voir les colonnes en tête du fichier)."""
    rows = list(csv.DictReader(io.StringIO(text.lstrip("\ufeff")), delimiter=";"))
    cases: dict[str, list[Turn]] = {}
    for line, raw in enumerate(rows, start=2):
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        case_id = row.get("id") or f"ligne{line}"
        where = f"{case_id} (ligne {line})"
        if not row.get("question"):
            raise ValueError(f"{where} : « question » manquante")
        reference = row.get("reference_answer", "")
        qtype = row.get("question_type", "")
        answerable = qtype.lower() != "hors-corpus" and not reference.upper().startswith(
            "HORS CORPUS"
        )
        try:
            pages = tuple(int(p) for p in _split(row.get("pages"), ","))
        except ValueError as exc:
            raise ValueError(f"{where} : pages illisibles « {row.get('pages')} »") from exc
        turn = Turn(
            question=row["question"],
            documents=_split(row.get("source_document")),
            pages=pages,
            facts=(),
            answerable=answerable,
            collections=_split(row.get("collections")) or None,
            reference=reference,
            keywords=_split(row.get("expected_keywords")),
            qtype=qtype,
            difficulty=row.get("difficulty", ""),
        )
        _check(turn, where)
        parent = row.get("follow_up_of", "")
        if parent:
            if parent not in cases:
                raise ValueError(f"{where} : suite de « {parent} », absente plus haut")
            cases[parent].append(turn)
        elif case_id in cases:
            raise ValueError(f"identifiant en double : {case_id}")
        else:
            cases[case_id] = [turn]
    if not cases:
        raise ValueError("aucune question")
    return [Case(case_id, tuple(turns)) for case_id, turns in cases.items()]


def load_questions(path: Path) -> list[Case]:
    text = path.read_text(encoding="utf-8")
    return load_cases_csv(text) if path.suffix.lower() == ".csv" else load_cases(text)


def load_cases(text: str) -> list[Case]:
    data = tomllib.loads(text)
    cases = []
    seen: set[str] = set()
    for i, raw in enumerate(data.get("case", []), start=1):
        case_id = str(raw.get("id") or f"q{i}")
        if case_id in seen:
            raise ValueError(f"identifiant en double : {case_id}")
        seen.add(case_id)
        collections = raw.get("collections")
        turns = [_turn(raw, collections, case_id)]
        for k, follow in enumerate(raw.get("followup", []), start=1):
            turns.append(_turn(follow, follow.get("collections", collections), f"{case_id}#{k}"))
        cases.append(Case(case_id, tuple(turns)))
    if not cases:
        raise ValueError("aucune question ([[case]])")
    return cases


# --- Mesures ----------------------------------------------------------------------------


def _norm_name(name: str) -> str:
    """Nom de fichier comparable : un même PDF peut avoir été envoyé sous un nom un peu
    différent (« Séance_2_-_Cours.pdf » / « Séance 2 - Cours (1).pdf ») : sans extension,
    sans suffixe de copie « (1) », lettres et chiffres seulement, sans accents ni casse."""
    stem = re.sub(r"\.pdf$", "", name.strip(), flags=re.IGNORECASE)
    stem = re.sub(r"\s*\(\d+\)$", "", stem)
    return re.sub(r"[\W_]+", "", _plain(stem))


def _same_file(a: str, b: str) -> bool:
    return _norm_name(a) == _norm_name(b)


def _expected(citation: dict[str, Any], documents: tuple[str, ...]) -> bool:
    return any(_same_file(citation["filename"], d) for d in documents)


def retrieval_rank(citations: list[dict[str, Any]], documents: tuple[str, ...]) -> int | None:
    """Rang (1 = premier extrait) du premier extrait d'un document attendu ; None : absent."""
    for rank, citation in enumerate(citations, start=1):
        if _expected(citation, documents):
            return rank
    return None


def page_found(
    citations: list[dict[str, Any]], documents: tuple[str, ...], pages: tuple[int, ...]
) -> bool | None:
    """Un extrait du bon document à une page attendue ? None : pas de page attendue."""
    if not pages:
        return None
    return any(c["page"] in pages and (not documents or _expected(c, documents)) for c in citations)


def cited_numbers(answer: str) -> set[int]:
    return {int(n) for n in re.findall(r"\[(\d+)\]", answer)}


def cites_expected(
    answer: str, citations: list[dict[str, Any]], documents: tuple[str, ...]
) -> bool | None:
    """La réponse cite-t-elle un extrait d'un document attendu ? None : pas de document."""
    if not documents:
        return None
    by_number = {c["n"]: c for c in citations}
    return any(n in by_number and _expected(by_number[n], documents) for n in cited_numbers(answer))


def _plain(text: str) -> str:
    """Minuscules, sans accents ni espaces dans les nombres (« 1 000 » = « 1000 »)."""
    text = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("\u2019", "'").replace("\u2212", "-")
    return re.sub(r"(?<=\d)\s+(?=\d)", "", text)


def keywords_found(answer: str, keywords: tuple[str, ...]) -> float | None:
    """Part des mots-clés présents tels quels dans la réponse ; None : aucun mot-clé."""
    if not keywords:
        return None
    plain = _plain(answer)
    return sum(_plain(k) in plain for k in keywords) / len(keywords)


JUDGE_PROMPT = """Tu évalues la réponse d'un assistant qui répond à partir de documents.

Question : {question}

{expectation}

Réponse de l'assistant :
\"\"\"
{answer}
\"\"\"

Réponds uniquement par un objet JSON sur une seule ligne, sans autre texte :
{{"verdict": "correct" ou "partiel" ou "faux", "raison": "une phrase courte"}}"""

EXPECT_FACTS = """Éléments attendus dans la réponse (même sens suffit, formulation libre) :
{facts}

- correct : tous les éléments attendus sont présents et rien ne les contredit ;
- partiel : une partie seulement des éléments attendus ;
- faux : aucun élément attendu, ou une affirmation qui les contredit."""

EXPECT_REFERENCE = """Réponse de référence :
{reference}
{keywords}
- correct : l'essentiel de la réponse de référence est présent (même sens suffit, formulation \
libre) et rien ne la contredit ; des détails en plus ne comptent pas contre la réponse ;
- partiel : une partie seulement de l'essentiel ;
- faux : l'essentiel manque, ou une affirmation contredit la référence."""

EXPECT_REFUSAL = """Les documents ne contiennent pas la réponse à cette question.{context}

- correct : l'assistant dit qu'il ne trouve pas l'information, sans rien inventer ;
- partiel : il le dit, mais avance quand même une réponse ;
- faux : il répond comme s'il savait."""


def judge_prompt(turn: Turn, answer: str) -> str | None:
    """Prompt du juge ; None : rien à juger (aucun élément attendu)."""
    if not turn.answerable:
        # « HORS CORPUS — le document dit seulement… » : ce que disent les documents aide le juge
        context = re.sub(r"^HORS CORPUS\s*[—–-]?\s*", "", turn.reference, flags=re.IGNORECASE)
        expectation = EXPECT_REFUSAL.format(
            context=f"\nCe que disent les documents : {context}" if context else ""
        )
    elif turn.reference:
        keywords = f"Éléments clés : {' ; '.join(turn.keywords)}\n" if turn.keywords else ""
        expectation = EXPECT_REFERENCE.format(reference=turn.reference, keywords=keywords)
    elif turn.facts or turn.keywords:
        facts = "\n".join(f"- {f}" for f in turn.facts or turn.keywords)
        expectation = EXPECT_FACTS.format(facts=facts)
    else:
        return None
    return JUDGE_PROMPT.format(question=turn.question, expectation=expectation, answer=answer)


def parse_verdict(text: str) -> tuple[str, str]:
    """(verdict, raison) lus dans la réponse du juge ; verdict « ? » si illisible."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            verdict = str(data.get("verdict", "")).strip().lower()
            if verdict in VERDICTS:
                return verdict, str(data.get("raison", "")).strip()
        except json.JSONDecodeError:
            pass
    lowered = text.lower()
    found = [v for v in VERDICTS if re.search(rf"\b{v}\b", lowered)]
    if len(found) == 1:
        return found[0], text.strip()[:200]
    return "?", text.strip()[:200]


# --- API --------------------------------------------------------------------------------


class ApiError(Exception):
    pass


class Api:
    def __init__(self, url: str, token: str, timeout: float) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def call(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        request = urllib.request.Request(
            self.url + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise ApiError(f"{method} {path} : HTTP {exc.code} {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ApiError(f"{method} {path} : {exc}") from exc
        return json.loads(body) if body else None


# --- Déroulé ----------------------------------------------------------------------------


def measure(row: dict[str, Any], turn: Turn) -> dict[str, Any]:
    """Mesures sans juge, calculées à partir des extraits et de la réponse : refaites aussi
    par --report (après une correction des questions ou des règles de comparaison)."""
    citations, answer = row["citations"], row["answer"]
    row.update(
        rank=retrieval_rank(citations, turn.documents) if turn.documents else None,
        page_found=page_found(citations, turn.documents, turn.pages),
        cites_expected=cites_expected(answer, citations, turn.documents),
        keywords=keywords_found(answer, turn.keywords),
    )
    return row


def ask_case(api: Api, case: Case, model: str | None, keep: bool) -> list[dict[str, Any]]:
    """Pose la question puis ses suites dans une même conversation ; une ligne par tour."""
    rows: list[dict[str, Any]] = []
    conversation_id = None
    for k, turn in enumerate(case.turns):
        row: dict[str, Any] = {"case": case.id, "turn": k, "question": turn.question}
        body: dict[str, Any] = {"question": turn.question, "stream": False}
        if model:
            body["model"] = model
        if turn.collections:
            body["collections"] = list(turn.collections)
        if conversation_id is not None:
            body["conversation_id"] = conversation_id
        try:
            response = api.call("POST", "/ask", body)
        except ApiError as exc:
            row["error"] = str(exc)
            rows.append(row)
            break  # les suites n'ont plus de sens sans ce tour
        conversation_id = response["conversation_id"]
        citations = response["citations"]
        row.update(
            model=response["model"],
            search_query=response.get("search_query"),
            answer=response["answer"],
            citations=[
                {"n": c["n"], "filename": c["filename"], "page": c["page"]} for c in citations
            ],
            latency_ms=response["latency_ms"],
            completion_tokens=response["completion_tokens"],
        )
        rows.append(measure(row, turn))
    if conversation_id is not None and not keep:
        try:
            api.call("DELETE", f"/conversations/{conversation_id}")
        except ApiError as exc:
            print(f"  conversation {conversation_id} non supprimée : {exc}", file=sys.stderr)
    return rows


def judge_rows(api: Api, cases: list[Case], rows: list[dict[str, Any]], judge_model: str | None):
    turns = {(c.id, k): t for c in cases for k, t in enumerate(c.turns)}
    for row in rows:
        if "answer" not in row:
            continue
        turn = turns[(row["case"], row["turn"])]
        if not turn.answerable and row["answer"].startswith(NO_PASSAGE):
            row["verdict"], row["reason"] = "correct", "aucun passage : réponse fixe de l'API"
            continue
        prompt = judge_prompt(turn, row["answer"])
        if prompt is None:
            continue
        body: dict[str, Any] = {"prompt": prompt, "max_tokens": 200, "temperature": 0}
        if judge_model:
            body["model"] = judge_model
        try:
            text = api.call("POST", "/llm/generate", body)["answer"]
        except ApiError as exc:
            row["verdict"], row["reason"] = "?", f"juge en échec : {exc}"
            continue
        row["verdict"], row["reason"] = parse_verdict(text)


# --- Résumé -----------------------------------------------------------------------------


def _pct(values: list[bool]) -> str:
    if not values:
        return "—"
    return f"{100 * sum(values) / len(values):.0f} % ({sum(values)}/{len(values)})"


def _mean_pct(values: list[float]) -> str:
    return f"{100 * statistics.mean(values):.0f} %" if values else "—"


def summarize(cases: list[Case], rows: list[dict[str, Any]]) -> dict[str, str]:
    turns = {(c.id, k): t for c in cases for k, t in enumerate(c.turns)}
    done = [r for r in rows if "answer" in r]

    def turn(r: dict[str, Any]) -> Turn:
        return turns[(r["case"], r["turn"])]

    with_docs = [r for r in done if turn(r).documents and turn(r).answerable]
    answerable = [r for r in done if turn(r).answerable and "verdict" in r]
    refusals = [r for r in done if not turn(r).answerable and "verdict" in r]
    follow_ups = [r for r in with_docs if r["turn"] > 0]
    latencies = [r["latency_ms"] / 1000 for r in done]
    return {
        "Questions posées": f"{len(done)} / {len(rows)}",
        "Bon document parmi les extraits": _pct([r["rank"] is not None for r in with_docs]),
        "Bon document en tête": _pct([r["rank"] == 1 for r in with_docs]),
        "Bonne page parmi les extraits": _pct(
            [r["page_found"] for r in done if r["page_found"] is not None and turn(r).answerable]
        ),
        "Cite le bon document": _pct([bool(r["cites_expected"]) for r in with_docs]),
        "Réponse correcte": _pct([r["verdict"] == "correct" for r in answerable]),
        "Réponse correcte ou partielle": _pct(
            [r["verdict"] in ("correct", "partiel") for r in answerable]
        ),
        "Mots-clés présents (moyenne)": _mean_pct(
            [r["keywords"] for r in done if turn(r).answerable and r.get("keywords") is not None]
        ),
        "Hors documents : refus correct": _pct([r["verdict"] == "correct" for r in refusals]),
        "Suivis : bon document retrouvé": _pct([r["rank"] is not None for r in follow_ups]),
        "Temps de réponse médian": f"{statistics.median(latencies):.1f} s" if latencies else "—",
        "Temps de réponse max": f"{max(latencies):.1f} s" if latencies else "—",
        "Erreurs": str(len(rows) - len(done)),
    }


def by_group(cases: list[Case], rows: list[dict[str, Any]], attr: str) -> dict[str, str]:
    """Réponses jugées correctes, par valeur de attr (difficulty, qtype), dans l'ordre du
    fichier. Pour une question hors documents, « correct » = refus correct."""
    turns = {(c.id, k): t for c in cases for k, t in enumerate(c.turns)}
    groups: dict[str, list[bool]] = {}
    for c in cases:
        for t in c.turns:
            if getattr(t, attr):
                groups.setdefault(getattr(t, attr), [])
    for r in rows:
        value = getattr(turns[(r["case"], r["turn"])], attr)
        if value and "verdict" in r:
            groups[value].append(r["verdict"] == "correct")
    return {value: _pct(results) for value, results in groups.items()}


def table_lines(title: str, tables: dict[str, dict[str, str]]) -> list[str]:
    """Tableau Markdown : une colonne par modèle ; lignes : les clés de toutes les tables."""
    labels = list(tables)
    keys = list(dict.fromkeys(k for table in tables.values() for k in table))
    if not keys:
        return []
    lines = ["", f"| {title} | " + " | ".join(labels) + " |", "|---|" + "---|" * len(labels)]
    for key in keys:
        lines.append(f"| {key} | " + " | ".join(tables[m].get(key, "—") for m in labels) + " |")
    return lines


def report(
    cases: list[Case], runs: dict[str, list[dict[str, Any]]], header: list[str]
) -> list[str]:
    """Synthèse en Markdown : réglages, tableaux par modèle, puis échecs de chaque modèle."""
    lines = ["# Évaluation de POST /ask", "", *header]
    lines += table_lines("Mesure", {m: summarize(cases, rows) for m, rows in runs.items()})
    judged = any("verdict" in r for rows in runs.values() for r in rows)
    if judged:
        for title, attr in (("par difficulté", "difficulty"), ("par type", "qtype")):
            tables = {m: by_group(cases, rows, attr) for m, rows in runs.items()}
            lines += table_lines(f"Réponse correcte, {title}", tables)
    for model, rows in runs.items():
        lines += ["", f"## Échecs — {model}", ""]
        lines += failures(cases, rows) or ["aucun"]
    return lines


def load_rows(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def failures(cases: list[Case], rows: list[dict[str, Any]]) -> list[str]:
    turns = {(c.id, k): t for c in cases for k, t in enumerate(c.turns)}
    lines = []
    for r in rows:
        label = r["case"] + (f" (suivi {r['turn']})" if r["turn"] else "")
        if "error" in r:
            lines.append(f"- {label} : erreur — {r['error']}")
            continue
        t = turns[(r["case"], r["turn"])]
        problems = []
        if t.documents and t.answerable and r["rank"] is None:
            found = ", ".join(sorted({c["filename"] for c in r["citations"]})) or "aucun extrait"
            problems.append(f"document attendu absent des extraits ({found})")
        if r.get("keywords") is not None and r["keywords"] < 1 and t.answerable:
            missing = [k for k in t.keywords if _plain(k) not in _plain(r["answer"])]
            problems.append("mots-clés absents : " + " | ".join(missing))
        if r.get("verdict") in ("faux", "partiel", "?"):
            problems.append(f"réponse {r['verdict']} : {r.get('reason', '')}")
        if problems:
            if r.get("search_query"):
                problems.append(f"recherche : « {r['search_query']} »")
            lines.append(f"- {label} : " + " ; ".join(problems))
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://localhost:8000", help="adresse de l'API")
    parser.add_argument(
        "--token",
        default=os.environ.get("RAG_TOKEN", ""),
        help="jeton ou clé API (par défaut : variable RAG_TOKEN)",
    )
    parser.add_argument(
        "--questions",
        type=Path,
        help="fichier CSV ou TOML (défaut : eval/questions.csv, sinon eval/questions.toml)",
    )
    parser.add_argument(
        "--model", action="append", help="modèle évalué (option répétable ; défaut : de l'API)"
    )
    parser.add_argument(
        "--judge-model", help="modèle juge (défaut : celui de l'API) ; le même pour tous"
    )
    parser.add_argument("--no-judge", action="store_true", help="mesurer la recherche seulement")
    parser.add_argument("--only", action="append", help="seulement ces questions (id)")
    parser.add_argument("--keep", action="store_true", help="garder les conversations créées")
    parser.add_argument("--timeout", type=float, default=600.0, help="délai max par requête (s)")
    parser.add_argument("--out", type=Path, default=ROOT / "eval" / "results")
    parser.add_argument(
        "--report",
        type=Path,
        nargs="+",
        metavar="JSONL",
        help="refaire la synthèse à partir de résultats enregistrés, sans reposer les questions",
    )
    args = parser.parse_args()
    if not args.token and not args.report:
        parser.error("jeton requis : --token ou variable RAG_TOKEN (l'API exige une connexion)")
    if args.questions is None:
        csv_file = ROOT / "eval" / "questions.csv"
        args.questions = csv_file if csv_file.exists() else ROOT / "eval" / "questions.toml"
    try:
        cases = load_questions(args.questions)
    except (OSError, ValueError, csv.Error, tomllib.TOMLDecodeError) as exc:
        parser.error(f"{args.questions} : {exc}")
    if args.only:
        cases = [c for c in cases if c.id in args.only]
    args.out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    header = [f"- Questions : `{args.questions.name}` ({len(cases)})"]

    runs: dict[str, list[dict[str, Any]]] = {}
    if args.report:
        turns = {(c.id, k): t for c in cases for k, t in enumerate(c.turns)}
        for path in args.report:
            try:
                rows = [r for r in load_rows(path) if (r["case"], r["turn"]) in turns]
            except (OSError, ValueError, KeyError) as exc:
                parser.error(f"{path} : {exc}")
            # Mesures refaites avec les questions et les règles actuelles ; verdicts conservés
            rows = [measure(r, turns[(r["case"], r["turn"])]) if "answer" in r else r for r in rows]
            model = next((r["model"] for r in rows if r.get("model")), path.stem)
            runs[model] = rows
            header.append(f"- Résultats : `{path.name}`")
    else:
        api = Api(args.url, args.token, args.timeout)
        header += [
            f"- API : {args.url}",
            f"- Juge : {'aucun' if args.no_judge else args.judge_model or 'modèle par défaut'}",
            f"- Date : {datetime.now():%Y-%m-%d %H:%M}",
        ]
        for model in args.model or [None]:
            label = model or "défaut de l'API"
            print(f"{label} : {len(cases)} questions", file=sys.stderr)
            rows = []
            start = time.perf_counter()
            for i, case in enumerate(cases, start=1):
                rows += ask_case(api, case, model, args.keep)
                print(f"  {i}/{len(cases)} {case.id}", file=sys.stderr)
            print(f"  réponses en {time.perf_counter() - start:.0f} s", file=sys.stderr)
            if not args.no_judge:
                judge_rows(api, cases, rows, args.judge_model)
            name = re.sub(r"[^\w.-]", "_", model or "defaut")
            details = args.out / f"{stamp}-{name}.jsonl"
            details.write_text(
                "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
            )
            print(f"  détail : {details}", file=sys.stderr)
            runs[label] = rows
            header.append(f"- Résultats ({label}) : `{details.name}`")

    text = "\n".join(report(cases, runs, header)) + "\n"
    summary = args.out / f"{stamp}-synthese.md"
    summary.write_text(text, encoding="utf-8")
    print(text)
    print(f"Synthèse : {summary}", file=sys.stderr)


if __name__ == "__main__":
    main()
