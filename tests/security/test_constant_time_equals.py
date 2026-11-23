from nanobot.security.secrets import constant_time_equals


def test_matching_and_mismatching_ascii() -> None:
    assert constant_time_equals("secret", "secret")
    assert not constant_time_equals("secret", "Secret")
    assert not constant_time_equals("", "secret")


def test_non_ascii_input_is_a_mismatch_not_an_exception() -> None:
    assert not constant_time_equals("café", "secret")
    assert constant_time_equals("café", "café")


def test_lone_surrogates_from_header_parsing_do_not_raise() -> None:
    assert not constant_time_equals("\udcff", "secret")
    assert not constant_time_equals("\udcff", "\udcfe")
    assert constant_time_equals("\udcff", "\udcff")
