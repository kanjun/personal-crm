from crm.queries import _address_phrases


def test_address_phrases_split_each_address_into_its_word_parts() -> None:
    assert _address_phrases(("ada@example.com", "a.b+c@x.io")) == '"ada example com" OR "a b c x io"'
    assert _address_phrases(()) == '""'
