"""Reformulation des questions de suivi (phase 5.4) : prompt et nettoyage, sans base."""

from datetime import UTC, datetime

import pytest

from app.conversations import Message
from app.rewrite import MAX_MESSAGE_CHARS, build_rewrite_messages, clean_rewrite


def message(role: str, content: str) -> Message:
    return Message(0, role, content, None, None, None, None, None, None, datetime.now(UTC))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Quelle est la grille des salaires 2025 ?", "Quelle est la grille des salaires 2025 ?"),
        ("« Qui a signé le contrat Alpha ? »", "Qui a signé le contrat Alpha ?"),
        ('"Qui a signé ?"', "Qui a signé ?"),
        ("Question reformulée : Où est la base ?", "Où est la base ?"),
        ("\n\n  Où est la base ?\nJ'ai remplacé « elle ».", "Où est la base ?"),
        ("", None),
        ("   \n  ", None),
        ("« »", None),
        ("x" * 2000, None),
    ],
)
def test_clean_rewrite(raw, expected):
    assert clean_rewrite(raw) == expected


def test_prompt_strips_citations_and_truncates_long_answers():
    history = [
        message("user", "Quel est le ROI ?"),
        message("assistant", "Le ROI est de 12 % [1][2]. " + "Détail. " * 200),
    ]
    [system, user] = build_rewrite_messages("Et en 2025 ?", history)
    assert system["role"] == "system"
    content = user["content"]
    assert "Utilisateur : Quel est le ROI ?" in content
    assert "Assistant : Le ROI est de 12 %." in content
    assert "[1]" not in content
    assert len(content) < MAX_MESSAGE_CHARS + 200
    assert content.endswith("Dernière question : Et en 2025 ?")
