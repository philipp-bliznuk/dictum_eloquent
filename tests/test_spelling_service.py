import pytest

from dictum.services import spelling


class TestNormalize:
    @pytest.mark.parametrize(
        ("raw", "clean"),
        [
            ("Abandon", "Abandon"),
            ("  put   paid\tto\n", "put paid to"),
            ("By\u00a0my troth!", "By my troth!"),
            ("", ""),
            ("   ", ""),
        ],
    )
    def test_collapses_whitespace(self, raw: str, clean: str) -> None:
        assert spelling.normalize(text=raw) == clean


class TestValidate:
    @pytest.mark.parametrize("text", ["in extenso", "and/or", "foo/ bar", "foo /bar", "Aide-de-camp"])
    def test_accepts(self, text: str) -> None:
        assert spelling.validate(text=text) == text

    def test_returns_normalised(self) -> None:
        assert spelling.validate(text="  By  my troth! ") == "By my troth!"

    @pytest.mark.parametrize("text", ["", " ", "\t\n"])
    def test_rejects_blank(self, text: str) -> None:
        with pytest.raises(ValueError, match="visible characters"):
            spelling.validate(text=text)

    @pytest.mark.parametrize("text", ["Color / Colour", "a  /  b", "put paid to / put paid"])
    def test_rejects_headline_separator(self, text: str) -> None:
        with pytest.raises(ValueError, match="must not contain ' / '"):
            spelling.validate(text=text)
