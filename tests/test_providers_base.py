import typing as t

import pytest

from dictum.enums import DefinitionSource
from dictum.enums import SpellingKind
from dictum.providers.base import merge_by_part_of_speech
from dictum.providers.base import valid_spellings
from dictum.schemas.lookup import DraftDefinition
from dictum.schemas.lookup import DraftEntry
from dictum.schemas.word import SpellingIn


def spelling(text: str, kind: SpellingKind = SpellingKind.PRIMARY) -> SpellingIn:
    return SpellingIn(text=text, kind=kind)


def definition(text: str) -> DraftDefinition:
    return DraftDefinition(text=text, source=DefinitionSource.MW)


def draft(
    *spellings: str, part_of_speech: str = "noun", definitions: t.Sequence[str] = (), **fields: t.Any
) -> DraftEntry:
    """Draft entry (first spelling primary, rest variants); `fields` sets pronunciation, audio_path or source."""
    values: dict[str, t.Any] = {"pronunciation": None, "audio_path": None, "source": DefinitionSource.MW, **fields}
    return DraftEntry(
        spellings=[
            spelling(s, SpellingKind.PRIMARY if i == 0 else SpellingKind.VARIANT) for i, s in enumerate(spellings)
        ],
        part_of_speech=part_of_speech,
        definitions=[definition(d) for d in definitions],
        **values,
    )


class TestMergeByPartOfSpeech:
    def test_empty(self) -> None:
        assert merge_by_part_of_speech(entries=[]) == []
        assert merge_by_part_of_speech(entries=iter(())) == []

    def test_distinct_parts_of_speech_kept_in_first_seen_order(self) -> None:
        entries = [
            draft("a", part_of_speech="verb"),
            draft("a", part_of_speech="noun"),
            draft("a", part_of_speech="adverb"),
        ]
        merged = merge_by_part_of_speech(entries=entries)
        assert [e.part_of_speech for e in merged] == ["verb", "noun", "adverb"]
        assert merged == entries

    def test_single_entry_copied_with_normalised_label(self) -> None:
        original = draft("abandon", part_of_speech=" Verb ", definitions=["d"])
        [merged] = merge_by_part_of_speech(entries=[original])
        assert merged.part_of_speech == "verb"
        assert merged is not original
        assert original.part_of_speech == " Verb "
        assert merged.model_dump(exclude={"part_of_speech"}) == original.model_dump(exclude={"part_of_speech"})

    @pytest.mark.parametrize(
        "labels", [("noun", "noun"), ("Noun", "noun"), (" noun", "NOUN "), ("plural noun", "plural\tnoun")]
    )
    def test_same_label_after_normalisation_folds(self, labels: tuple[str, str]) -> None:
        first, second = labels
        merged = merge_by_part_of_speech(entries=[draft("a", part_of_speech=first), draft("a", part_of_speech=second)])
        assert len(merged) == 1
        assert merged[0].part_of_speech == " ".join(first.split()).casefold()

    def test_definitions_concatenated_in_order(self) -> None:
        entries = [
            draft("bank", definitions=["a slope", "a shoal"]),
            draft("bank", part_of_speech="verb", definitions=["to heap"]),
            draft("bank", definitions=["a financial institution"]),
            draft("bank", definitions=[]),
            draft("bank", part_of_speech="verb", definitions=["to deposit"]),
        ]
        merged = merge_by_part_of_speech(entries=entries)
        assert [(e.part_of_speech, [d.text for d in e.definitions]) for e in merged] == [
            ("noun", ["a slope", "a shoal", "a financial institution"]),
            ("verb", ["to heap", "to deposit"]),
        ]

    def test_spellings_unioned_and_deduplicated_by_key(self) -> None:
        entries = [
            draft("color", "colour"),
            draft("Color", "colors", "COLOUR"),
            draft("colour", "coloured"),
        ]
        [merged] = merge_by_part_of_speech(entries=entries)
        assert [(s.text, s.kind) for s in merged.spellings] == [
            ("color", SpellingKind.PRIMARY),
            ("colour", SpellingKind.VARIANT),
            ("colors", SpellingKind.VARIANT),
            ("coloured", SpellingKind.VARIANT),
        ]

    def test_first_pronunciation_and_audio_win(self) -> None:
        entries = [
            draft("a", pronunciation=None, audio_path=None),
            draft("a", pronunciation="first", audio_path=None),
            draft("a", pronunciation="second", audio_path="/audio/first.mp3"),
            draft("a", pronunciation="third", audio_path="/audio/second.mp3"),
        ]
        [merged] = merge_by_part_of_speech(entries=entries)
        assert (merged.pronunciation, merged.audio_path) == ("first", "/audio/first.mp3")

    @pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
    def test_blank_label_becomes_other(self, blank: str) -> None:
        entries = [
            draft("a", part_of_speech=blank, definitions=["d1"]),
            draft("a", part_of_speech="other", definitions=["d2"]),
        ]
        [merged] = merge_by_part_of_speech(entries=entries)
        assert merged.part_of_speech == "other"
        assert [d.text for d in merged.definitions] == ["d1", "d2"]

    def test_source_of_first_entry_kept(self) -> None:
        entries = [draft("a", source=DefinitionSource.FREE_DICT), draft("a", source=DefinitionSource.MW)]
        [merged] = merge_by_part_of_speech(entries=entries)
        assert merged.source is DefinitionSource.FREE_DICT

    def test_later_entries_not_mutated(self) -> None:
        later = draft("b", definitions=["d2"], pronunciation="p")
        before = later.model_copy(deep=True)
        merge_by_part_of_speech(entries=[draft("a", definitions=["d1"]), later])
        assert later == before

    def test_accepts_generator(self) -> None:
        merged = merge_by_part_of_speech(entries=(draft("a", definitions=[str(i)]) for i in range(3)))
        assert [d.text for d in merged[0].definitions] == ["0", "1", "2"]


class TestValidSpellings:
    def test_drops_invalid_and_duplicates_keeps_first_kind(self) -> None:
        candidates = [
            ("abandon", SpellingKind.PRIMARY),
            ("", SpellingKind.VARIANT),
            ("a / b", SpellingKind.VARIANT),
            ("ABANDON", SpellingKind.VARIANT),
            (" abandonne ", SpellingKind.VARIANT),
            ("x" * 256, SpellingKind.VARIANT),
        ]
        assert [(s.text, s.kind) for s in valid_spellings(candidates=candidates)] == [
            ("abandon", SpellingKind.PRIMARY),
            ("abandonne", SpellingKind.VARIANT),
        ]

    def test_empty(self) -> None:
        assert valid_spellings(candidates=[]) == []
