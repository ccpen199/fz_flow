from app.services.input_correction_lexicon_service import normalize_correction_word


def test_normalize_correction_word_ignores_case_spacing_and_full_width_forms() -> None:
    assert normalize_correction_word(" THE NORTH FACE ") == "thenorthface"
    assert normalize_correction_word("Ｔｈｅ－Ｎｏｒｔｈ　Ｆａｃｅ") == "thenorthface"


def test_normalize_correction_word_keeps_chinese_characters() -> None:
    assert normalize_correction_word(" 北面（中国） ") == "北面中国"
