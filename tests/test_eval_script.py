"""Script d'évaluation (scripts/eval_ask.py) : lecture des questions, mesures, juge, résumé.
Sans base ni modèle : l'API est remplacée par un faux."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import eval_ask  # noqa: E402

CITATIONS = [
    {"n": 1, "filename": "autre.pdf", "page": 3},
    {"n": 2, "filename": "Rapport-Alpha.pdf", "page": 12},
]


def test_example_file_is_valid():
    cases = eval_ask.load_cases((ROOT / "eval" / "questions.example.toml").read_text("utf-8"))
    assert [c.id for c in cases][:2] == ["roi-alpha", "contrat-maintenance-duree"]
    roi = cases[0]
    assert [t.question for t in roi.turns][1] == "Et pour le projet Bêta ?"
    assert roi.turns[0].pages == (12,)
    assert cases[-1].turns[0].answerable is False


@pytest.mark.parametrize(
    ("toml", "message"),
    [
        ("", "aucune question"),
        ('[[case]]\nid = "a"\n', "question"),
        ('[[case]]\nquestion = "x ?"\n', "documents"),
        ('[[case]]\nid = "a"\nquestion = "x"\nanswerable = false\n' * 2, "double"),
    ],
)
def test_invalid_files(toml, message):
    with pytest.raises(ValueError, match=message):
        eval_ask.load_cases(toml)


def test_measures():
    docs = ("rapport-alpha.pdf",)  # casse ignorée
    assert eval_ask.retrieval_rank(CITATIONS, docs) == 2
    assert eval_ask.retrieval_rank(CITATIONS, ("absent.pdf",)) is None
    assert eval_ask.page_found(CITATIONS, docs, (12,)) is True
    assert eval_ask.page_found(CITATIONS, docs, (3,)) is False  # page 3, mais d'un autre document
    assert eval_ask.page_found(CITATIONS, docs, ()) is None
    assert eval_ask.cites_expected("Le ROI est de 18 % [2].", CITATIONS, docs) is True
    assert eval_ask.cites_expected("Le ROI est de 18 % [1].", CITATIONS, docs) is False
    assert eval_ask.cites_expected("Sans citation.", CITATIONS, ()) is None


@pytest.mark.parametrize(
    ("text", "verdict"),
    [
        ('{"verdict": "correct", "raison": "tout y est"}', "correct"),
        ('Voici :\n```json\n{"verdict": "Partiel", "raison": "manque 12 %"}\n```', "partiel"),
        ("La réponse est fausse.", "?"),  # « fausse » n'est pas « faux »
        ("Verdict : faux, elle invente.", "faux"),
        ("correct ou partiel, difficile à dire", "?"),
    ],
)
def test_parse_verdict(text, verdict):
    assert eval_ask.parse_verdict(text)[0] == verdict


class FakeApi:
    """Répond à /ask selon la question, et le juge renvoie un verdict fixé."""

    def __init__(self):
        self.calls = []
        self.judge_answer = '{"verdict": "correct", "raison": "ok"}'

    def call(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if path == "/ask":
            if payload["question"].startswith("Panne"):
                raise eval_ask.ApiError("POST /ask : HTTP 503")
            follow = "conversation_id" in payload
            return {
                "conversation_id": 7,
                "model": payload.get("model", "défaut"),
                "search_query": "ROI du projet Bêta ?" if follow else None,
                "answer": "Le ROI est de 18 % [2]." if not follow else "Je ne sais pas [1].",
                "citations": [{**c, "score": 0.1, "excerpt": "…"} for c in CITATIONS],
                "latency_ms": 2000 if follow else 1000,
                "completion_tokens": 10,
            }
        if path == "/llm/generate":
            return {"answer": self.judge_answer}
        return None


QUESTIONS = """
[[case]]
id = "roi"
question = "ROI d'Alpha ?"
documents = ["rapport-alpha.pdf"]
facts = ["18 %"]
  [[case.followup]]
  question = "Et Bêta ?"
  documents = ["rapport-beta.pdf"]

[[case]]
id = "panne"
question = "Panne ?"
facts = ["x"]
  [[case.followup]]
  question = "jamais posée"
  facts = ["y"]
"""


def test_run_judge_and_summary():
    api = FakeApi()
    cases = eval_ask.load_cases(QUESTIONS)
    rows = []
    for case in cases:
        rows += eval_ask.ask_case(api, case, "gemma4:12b", keep=False)
    # Suite posée dans la même conversation, puis conversation supprimée
    asks = [p for m, path, p in api.calls if path == "/ask"]
    assert asks[1]["conversation_id"] == 7 and asks[1]["model"] == "gemma4:12b"
    assert ("DELETE", "/conversations/7", None) in api.calls
    # Un tour en erreur arrête ses suites
    assert [(r["case"], r["turn"], "error" in r) for r in rows] == [
        ("roi", 0, False),
        ("roi", 1, False),
        ("panne", 0, True),
    ]
    eval_ask.judge_rows(api, cases, rows, "mistral-small3.2:24b")
    judge_calls = [p for m, path, p in api.calls if path == "/llm/generate"]
    assert len(judge_calls) == 1  # le suivi n'a pas de « facts » : rien à juger
    assert judge_calls[0]["model"] == "mistral-small3.2:24b"
    assert "- 18 %" in judge_calls[0]["prompt"]
    summary = eval_ask.summarize(cases, rows)
    assert summary["Questions posées"] == "2 / 3"
    assert summary["Bon document parmi les extraits"] == "50 % (1/2)"
    assert summary["Suivis : bon document retrouvé"] == "0 % (0/1)"
    assert summary["Réponse correcte"] == "100 % (1/1)"
    assert summary["Temps de réponse médian"] == "1.5 s"
    assert summary["Erreurs"] == "1"
    problems = eval_ask.failures(cases, rows)
    assert any("roi (suivi 1)" in p and "ROI du projet Bêta" in p for p in problems)
    assert any(p.startswith("- panne : erreur") for p in problems)


def test_unanswerable_with_fixed_answer_needs_no_judge():
    api = FakeApi()
    cases = eval_ask.load_cases('[[case]]\nquestion = "Capitale ?"\nanswerable = false\n')
    rows = [{"case": "q1", "turn": 0, "answer": eval_ask.NO_PASSAGE + " pour répondre."}]
    eval_ask.judge_rows(api, cases, rows, None)
    assert rows[0]["verdict"] == "correct"
    assert api.calls == []


# --- Format CSV (« ; », colonnes du tableur) --------------------------------------------

CSV = (
    "﻿id;question;reference_answer;source_document;pages;question_type;difficulty;"
    "expected_keywords;follow_up_of\n"
    "Q01;Combien de pages ?;Entre 50 et 60 pages.;3_EID_Référentiel.pdf;4;factuelle;facile;"
    "50 | 60;\n"
    'Q02;Et la soutenance ?;"45 min ; 15 + 30.";3_EID_Référentiel.pdf;13, 14;numérique;'
    "moyenne;45;Q01\n"
    "Q28;Prix des jetons ?;HORS CORPUS — le document ne donne pas de tarif.;doc.pdf;2;"
    "hors-corpus;difficile;\n"
)


def test_csv_file():
    cases = eval_ask.load_cases_csv(CSV)
    assert [c.id for c in cases] == ["Q01", "Q28"]
    first, follow = cases[0].turns
    assert first.keywords == ("50", "60") and first.pages == (4,)
    assert first.reference == "Entre 50 et 60 pages." and first.difficulty == "facile"
    assert follow.question == "Et la soutenance ?" and follow.pages == (13, 14)
    assert follow.reference == "45 min ; 15 + 30."  # « ; » entre guillemets
    out = cases[1].turns[0]
    assert out.answerable is False and out.qtype == "hors-corpus"


def test_csv_errors():
    header = "id;question;reference_answer;follow_up_of;pages\n"
    with pytest.raises(ValueError, match="absente plus haut"):
        eval_ask.load_cases_csv(header + "Q2;Et ?;x;Q1;\n")
    with pytest.raises(ValueError, match="pages illisibles"):
        eval_ask.load_cases_csv(header + "Q1;Q ?;x;;p. 4\n")


def test_judge_prompt_uses_reference_and_document_context():
    cases = eval_ask.load_cases_csv(CSV)
    prompt = eval_ask.judge_prompt(cases[0].turns[0], "Entre 50 et 60 pages [1].")
    assert "Réponse de référence :\nEntre 50 et 60 pages." in prompt
    assert "Éléments clés : 50 ; 60" in prompt
    refusal = eval_ask.judge_prompt(cases[1].turns[0], "0,50 $")
    assert "ne contiennent pas la réponse" in refusal
    assert "Ce que disent les documents : le document ne donne pas de tarif." in refusal


def test_file_names_and_keywords_are_normalized():
    decomposed = "3_EID_Référentiel.pdf"  # accents décomposés
    citations = [{"n": 1, "filename": decomposed, "page": 4}]
    assert eval_ask.retrieval_rank(citations, ("3_EID_Référentiel.pdf",)) == 1
    keywords = ("1 000", "8 192", "NOM-Prénom-ANNÉE", "taux d'exactitude")
    answer = "Entre 1000 et 8 192 ; nom-prenom-annee ; taux d’exactitude."
    assert eval_ask.keywords_found(answer, keywords) == 1.0
    assert eval_ask.keywords_found("rien", ("a", "b")) == 0.0


def test_breakdown_by_difficulty():
    cases = eval_ask.load_cases_csv(CSV)
    rows = [
        {"case": "Q01", "turn": 0, "answer": "a", "verdict": "correct"},
        {"case": "Q01", "turn": 1, "answer": "b", "verdict": "faux"},
        {"case": "Q28", "turn": 0, "answer": "c", "verdict": "correct"},
    ]
    assert eval_ask.by_group(cases, rows, "difficulty") == {
        "facile": "100 % (1/1)",
        "moyenne": "0 % (0/1)",
        "difficile": "100 % (1/1)",
    }


def test_same_pdf_under_slightly_different_names():
    expected = ("3_EID_Référentiel_Dossier_Professionnel.pdf",)
    for sent in (
        "3 EID Référentiel Dossier Professionnel (1).pdf",
        "3-eid-referentiel-dossier-professionnel.PDF",
    ):
        assert eval_ask.retrieval_rank([{"n": 1, "filename": sent, "page": 1}], expected) == 1
    other = [{"n": 1, "filename": "3_EID_Référentiel_Annexe.pdf", "page": 1}]
    assert eval_ask.retrieval_rank(other, expected) is None
