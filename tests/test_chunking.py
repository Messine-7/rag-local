import pytest

from app.chunking import Chunk, chunk_pages, normalize, split_text


def test_normalize_joins_lines_and_keeps_paragraphs():
    text = "Première ligne\ncoupée  en deux.\n\n\n  Second\tparagraphe.\x00 "
    assert normalize(text) == "Première ligne coupée en deux.\n\nSecond paragraphe."


def test_short_text_is_one_chunk():
    assert split_text("Une phrase courte.", max_chars=100, overlap=10) == ["Une phrase courte."]


def test_empty_text_gives_no_chunk():
    assert split_text(" \n\n ", max_chars=100, overlap=10) == []


def test_chunks_respect_size_and_overlap():
    sentences = [f"Phrase numéro {i} avec quelques mots de remplissage." for i in range(60)]
    text = " ".join(sentences)
    chunks = split_text(text, max_chars=300, overlap=60)
    assert len(chunks) > 5
    assert all(len(c) <= 300 + 60 for c in chunks)
    # Rien de perdu : chaque phrase se retrouve dans au moins un chunk
    assert all(any(s in c for c in chunks) for s in sentences)
    # Chevauchement : le début de chaque chunk reprend la fin du précédent
    for previous, current in zip(chunks, chunks[1:], strict=False):
        assert current.split(" ", 1)[0] in previous


def test_cuts_between_paragraphs_first():
    paragraphs = ["a" * 50 + " fin.", "b" * 50 + " fin."]
    chunks = split_text("\n\n".join(paragraphs), max_chars=70, overlap=0)
    assert chunks == paragraphs


def test_word_longer_than_chunk_is_cut():
    chunks = split_text("x" * 250, max_chars=100, overlap=0)
    assert [len(c) for c in chunks] == [100, 100, 50]


def test_invalid_overlap():
    with pytest.raises(ValueError):
        split_text("texte", max_chars=100, overlap=100)


def test_chunk_pages_keeps_page_numbers():
    chunks = chunk_pages(["Page un.", "", "Page trois."], max_chars=100, overlap=10)
    assert chunks == [Chunk(page=1, content="Page un."), Chunk(page=3, content="Page trois.")]
