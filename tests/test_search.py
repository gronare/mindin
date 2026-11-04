from __future__ import annotations

from pathlib import Path

import pytest

from mindin.schema import Schema, instructions
from mindin.search import SearchResult, render, search
from mindin.vault import Vault


def keys(result) -> list[str]:
    return [row.note.key for row in result.rows]


def run(vault: Vault, query: str, **kwargs):
    return search(vault.index, vault.schema, query, **kwargs)


def test_terms_are_anded(vault: Vault):
    found = keys(run(vault, "orchard bokning"))
    assert "Resources/booking-trap.md" in found
    assert "Projects/orchard-draft.md" not in found
    assert "Areas/workshop.md" not in found


def test_synonyms_from_the_vault_schema_are_expanded(vault: Vault):
    assert keys(run(vault, "komponent")) == ["Projects/orchard-fresh.md"]


def test_terms_match_as_prefixes(vault: Vault):
    assert "Projects/orchard-fresh.md" in keys(run(vault, "bokning"))


def test_diacritics_are_folded(vault: Vault):
    assert "Areas/orchard.md" in keys(run(vault, "domän"))


def test_a_sha_looks_up_the_notes_that_mention_it(vault: Vault):
    assert keys(run(vault, "9ca31382")) == ["Resources/booking-trap.md"]


def test_a_sha_prefix_finds_the_same_note(vault: Vault):
    assert keys(run(vault, "9ca3138")) == ["Resources/booking-trap.md"]


def test_archive_is_hidden_and_counted(vault: Vault):
    result = run(vault, "orchard")
    assert result.hidden_archive == 2
    assert not any(key.startswith("Archive/") for key in keys(result))


def test_archive_is_returned_when_asked_for(vault: Vault):
    result = run(vault, "orchard", include_archive=True)
    assert result.hidden_archive == 0
    assert "Archive/old-plan.md" in keys(result)


def test_superseded_is_hidden_and_counted(vault: Vault):
    result = run(vault, "domän")
    assert result.hidden_superseded == 1
    assert "Resources/old-domain-howto.md" not in keys(result)


def test_superseded_is_returned_when_asked_for(vault: Vault):
    result = run(vault, "domän", include_superseded=True)
    assert "Resources/old-domain-howto.md" in keys(result)


def test_folder_weight_puts_the_system_note_first(vault: Vault):
    assert keys(run(vault, "lundkod"))[0] == "Areas/orchard.md"


def test_limit_is_respected(vault: Vault):
    result = run(vault, "orchard", limit=2)
    assert len(result.rows) == 2
    assert result.total == 5


def test_folder_filter_narrows_the_result(vault: Vault):
    assert keys(run(vault, "orchard", folder="Log")) == ["Log/orchard-log.md"]


def test_status_filter_narrows_the_result(vault: Vault):
    assert keys(run(vault, "orchard", status="draft")) == ["Projects/orchard-draft.md"]


def test_tag_filter_narrows_the_result(vault: Vault):
    assert keys(run(vault, "orchard", tag="ui")) == ["Projects/orchard-fresh.md"]


def test_kind_filter_narrows_the_result(vault: Vault):
    assert keys(run(vault, "orchard", kind="system")) == ["Areas/orchard.md"]


def test_path_prefix_filter_narrows_the_result(vault: Vault):
    found = keys(run(vault, "release", path_prefix="~/projects/orchard"))
    assert "Areas/workshop.md" not in found
    assert "Projects/orchard-draft.md" in found


def test_a_quoted_phrase_stays_a_phrase(vault: Vault):
    assert keys(run(vault, '"frame-addons och"')) == ["Projects/orchard-fresh.md"]


def test_render_states_the_counts_in_the_header(vault: Vault):
    text = render(run(vault, "orchard", limit=2))
    assert text.splitlines()[0] == "2 of 5 (archive: 2 hidden, superseded: 0 hidden)"


def test_render_says_so_when_nothing_matched(vault: Vault):
    assert "No notes matched." in render(run(vault, "kvantdator"))


def add_note(vault: Vault, vault_dir: Path, key: str, title: str, body: str, status: str = "active") -> None:
    path = vault_dir / key
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = f"title: {title}\ndate: 2026-02-01\nupdated: 2026-02-01\ntags: [workshop]\nstatus: {status}"
    path.write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")
    vault.index.sync(force=True)


def add_gasket_notes(vault: Vault, vault_dir: Path) -> None:
    add_note(vault, vault_dir, "Projects/cider-press-gasket-repair.md", "Packningsbyte", "Packningen läcker.")
    add_note(vault, vault_dir, "Areas/cellar.md", "cellar", "Se [[cider-press-gasket-repair]] för packningen.")


def test_a_hyphenated_stem_finds_the_note_itself_before_a_note_linking_to_it(vault: Vault, vault_dir: Path):
    add_gasket_notes(vault, vault_dir)
    found = keys(run(vault, "cider-press-gasket-repair"))
    assert found[0] == "Projects/cider-press-gasket-repair.md"
    assert "Areas/cellar.md" in found


def test_a_stem_phrase_outranks_the_same_words_in_a_linking_body(vault: Vault, vault_dir: Path):
    add_note(vault, vault_dir, "Projects/cider-press-gasket-repair.md", "Packningsbyte", "Packningen läcker.")
    add_note(vault, vault_dir, "Projects/cellar-shelves.md", "Hyllor", "Se [[cider-press-gasket-repair]] först.")
    found = keys(run(vault, "cider-press-gasket"))
    assert found == ["Projects/cider-press-gasket-repair.md", "Projects/cellar-shelves.md"]


@pytest.mark.parametrize(
    "query",
    [
        "[[cider-press-gasket-repair]]",
        "[[cider-press-gasket-repair|packningen]]",
        "Projects/cider-press-gasket-repair",
        "cider-press-gasket-repair.md",
        "CIDER-Press-Gasket-Repair",
    ],
)
def test_an_exact_stem_in_any_wikilink_shape_puts_the_note_first(vault: Vault, vault_dir: Path, query: str):
    add_gasket_notes(vault, vault_dir)
    assert keys(run(vault, query))[0] == "Projects/cider-press-gasket-repair.md"


def test_an_exact_stem_finds_an_archived_note_without_include_archive(vault: Vault):
    result = run(vault, "old-plan")
    assert keys(result)[0] == "Archive/old-plan.md"


def test_an_exact_stem_finds_a_superseded_note_without_include_superseded(vault: Vault):
    assert keys(run(vault, "old-domain-howto"))[0] == "Resources/old-domain-howto.md"


def test_filters_still_exclude_an_exact_stem_match(vault: Vault):
    assert "Archive/old-plan.md" not in keys(run(vault, "old-plan", folder="Projects"))
    assert "Archive/old-plan.md" not in keys(run(vault, "old-plan", status="active"))
    assert "Archive/old-plan.md" not in keys(run(vault, "old-plan", tag="ui"))


def test_a_vault_schema_with_four_weights_gets_the_default_stem_and_stemmed_weights():
    schema = Schema({"search": {"title_weight": 8, "summary_weight": 4, "tags_weight": 2, "body_weight": 1}})
    assert schema.bm25_weights == (8.0, 4.0, 2.0, 1.0, 20.0, 0.5)


def test_the_stemmed_weight_is_configurable():
    assert Schema({"search": {"stemmed_weight": 2}}).bm25_weights[5] == 2.0


def add_mill_notes(vault: Vault, vault_dir: Path) -> None:
    add_note(vault, vault_dir, "Projects/mill-wheel-service.md", "Kvarnservice", "Kvarnhjul, remskiva och smörjfett.")
    add_note(vault, vault_dir, "Areas/mill.md", "kvarnhjul", "Kvarnhjul kvarnhjul kvarnhjul.")
    add_note(vault, vault_dir, "Archive/mill-belt.md", "Gammal rem", "En remskiva från förr.", status="complete")


def test_a_missing_word_falls_back_to_notes_matching_most_terms(vault: Vault, vault_dir: Path):
    add_mill_notes(vault, vault_dir)
    result = run(vault, "kvarnhjul remskiva smörjfett zeppelinare")
    assert keys(result) == ["Projects/mill-wheel-service.md", "Areas/mill.md"]
    assert [row.terms for row in result.rows] == [3, 1]
    assert result.hidden_archive == 1


def test_the_fallback_header_says_the_result_is_partial(vault: Vault, vault_dir: Path):
    add_mill_notes(vault, vault_dir)
    lines = render(run(vault, "kvarnhjul remskiva smörjfett zeppelinare")).splitlines()
    assert lines[0] == "0 matched all 4 terms; 2 match some (best 3 of 4) (archive: 1 hidden, superseded: 0 hidden)"
    assert lines[1].startswith("Projects/mill-wheel-service.md | Kvarnservice | ")


def test_a_query_every_term_matches_keeps_the_normal_header(vault: Vault, vault_dir: Path):
    add_mill_notes(vault, vault_dir)
    result = run(vault, "kvarnhjul remskiva")
    assert keys(result) == ["Projects/mill-wheel-service.md"]
    assert not result.partial
    assert render(result).splitlines()[0] == "1 of 1 (archive: 0 hidden, superseded: 0 hidden)"


def test_a_single_missing_term_never_falls_back(vault: Vault, vault_dir: Path):
    add_mill_notes(vault, vault_dir)
    result = run(vault, "zeppelinare")
    assert not result.partial
    assert render(result).splitlines()[:2] == ["0 of 0 (archive: 0 hidden, superseded: 0 hidden)", "No notes matched."]


def test_the_languages_put_the_primary_first_and_drop_repeats():
    schema = Schema({"language": "sv", "other_languages": ["en", "sv", "de"]})
    assert schema.languages == ("sv", "en", "de")


def test_an_inflected_query_finds_another_inflection_in_the_primary_language(vault: Vault, vault_dir: Path):
    add_note(vault, vault_dir, "Projects/cellar-press.md", "Källaren", "Äppelpressen står i källaren.")
    assert keys(run(vault, "äppelpressar")) == ["Projects/cellar-press.md"]


def test_an_inflected_query_finds_a_note_in_the_other_language(vault: Vault, vault_dir: Path):
    add_note(vault, vault_dir, "Projects/press-log.md", "Cellar", "The old frame presses apples twice a week.")
    assert keys(run(vault, "pressing")) == ["Projects/press-log.md"]


def test_an_inflected_phrase_matches_the_stemmed_phrase(vault: Vault, vault_dir: Path):
    add_note(vault, vault_dir, "Projects/cellar-press.md", "Källaren", "Äppelpressen i källaren läcker.")
    assert keys(run(vault, '"äppelpressar i källarna"')) == ["Projects/cellar-press.md"]


def test_a_synonym_is_stemmed_too(vault: Vault, vault_dir: Path):
    add_note(vault, vault_dir, "Projects/seed-sort.md", "Sortering", "Kärnor sorteras för hand.")
    assert "Projects/seed-sort.md" in keys(run(vault, "core"))


def test_the_exact_form_outranks_a_stemmed_only_match(vault: Vault, vault_dir: Path):
    add_note(vault, vault_dir, "Projects/press-a.md", "Cellar", "Presses the cider in the cellar.")
    add_note(vault, vault_dir, "Projects/press-b.md", "Cellar", "Pressing the cider in the cellar.")
    assert keys(run(vault, "pressing")) == ["Projects/press-b.md", "Projects/press-a.md"]


def test_an_unsupported_language_is_ignored_for_stemming(vault: Vault, tmp_path: Path):
    schema = Schema({"language": "sv", "other_languages": ["xx"]})
    opened = Vault(vault.backend, schema, index_path=tmp_path / "xx.sqlite")
    try:
        opened.index.sync(force=True)
        assert opened.index.algorithms == ("swedish",)
        assert "Areas/orchard.md" in keys(search(opened.index, schema, "lundkoden"))
        assert "also xx:" in render(search(opened.index, schema, "kvantdator"))
    finally:
        opened.close()


def test_nothing_matched_tells_the_client_to_translate_the_terms(vault: Vault):
    lines = render(run(vault, "kvantdator")).splitlines()
    assert lines[1:] == [
        "No notes matched.",
        "The vault is written in sv, also en: search again with the terms translated into en.",
    ]


def test_nothing_matched_with_hidden_notes_carries_no_translate_hint(vault: Vault):
    lines = render(run(vault, "arkiverad")).splitlines()
    assert lines[1:] == ["No notes matched."]


def test_a_partial_result_suggests_the_translated_query(vault: Vault, vault_dir: Path):
    add_mill_notes(vault, vault_dir)
    lines = render(run(vault, "kvarnhjul remskiva smörjfett zeppelinare")).splitlines()
    assert lines[-1] == "The terms translated into en may match them all."


def test_a_single_language_vault_asks_for_other_words_in_its_language():
    lines = render(SearchResult(languages=("sv",))).splitlines()
    assert lines[1:] == [
        "No notes matched.",
        "The vault is written in sv: search again with other words or the terms in sv.",
    ]


def test_the_instructions_tell_the_client_to_translate_into_the_other_languages():
    text = instructions(Schema({"language": "sv", "other_languages": ["en"]}))
    assert "translate the terms into the vault's other languages (en) and search again" in text
