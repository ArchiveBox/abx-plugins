"""Search sanitization must preserve text across actual file read boundaries."""

import base64
import hashlib
import json

import pytest

from abx_plugins.plugins.base.search_text import clean_index_text, read_indexable_text


@pytest.mark.parametrize("offset", range(65532, 65538))
def test_data_url_crosses_each_prefix_boundary(tmp_path, offset):
    prefix = ("ordinary context " * 5000)[: offset - 1] + "\n"
    payload = base64.b64encode(bytes(range(256)) * 1024).decode("ascii")
    source = prefix + '"data:application/octet-stream;base64,' + payload + '" after ไทย'
    expected = prefix + '"" after ไทย'
    path = tmp_path / "article.txt"
    path.write_text(source)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert read_indexable_text(path, len(expected.encode())) == expected
    assert clean_index_text(source) == expected
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


@pytest.mark.parametrize("length", [255, 256, 131072])
def test_encoded_run_threshold_across_chunks(tmp_path, length):
    prefix = ("ordinary context " * 5000)[: 65536 - 129] + "\n"
    encoded = ("Ab1+/_=-" * (length // 8 + 1))[:length]
    source = prefix + encoded + " after ไทย " + "ไทย" * 1000
    expected = prefix + (encoded if length < 256 else "") + " after ไทย " + "ไทย" * 1000
    path = tmp_path / "article.txt"
    path.write_text(source)
    assert read_indexable_text(path, len(expected.encode())) == expected
    assert clean_index_text(source) == expected


@pytest.mark.parametrize("limit", range(12))
def test_byte_limit_keeps_one_complete_unicode_character(tmp_path, limit):
    source = "aไทย résumé end"
    path = tmp_path / "article.txt"
    path.write_text(source)
    actual = read_indexable_text(path, limit)
    expected = ""
    for character in source:
        expected += character
        if len(expected.encode()) > limit:
            break
    assert actual == expected
    assert limit < len(actual.encode()) <= limit + 4


def test_data_word_boundary_and_json_quotes_are_preserved(tmp_path):
    source = (
        '{"metadata":"useful","image":"DATA:image/png;base64,'
        + "A" * 300
        + '","text":"after"}'
    )
    expected = '{"metadata":"useful","image":"","text":"after"}'
    path = tmp_path / "metadata.json"
    path.write_text(source)
    assert read_indexable_text(path, len(expected.encode())) == expected
    assert clean_index_text(source) == expected


@pytest.mark.parametrize("suffix", ["", "x", "xy", "xyz", "data", "dat", "da", "d"])
def test_non_url_word_boundary_survives_chunk_end(tmp_path, suffix):
    token = "metadata:" + suffix
    source = " " * (65536 - len(token)) + token + " useful text"
    path = tmp_path / "article.txt"
    path.write_text(source)
    assert read_indexable_text(path, len(source.encode())) == source
    assert clean_index_text(source) == source


@pytest.mark.parametrize("following", ['"', "\n"])
def test_metadata_data_url_preserves_json_escapes(tmp_path, following):
    description = "before data:text/plain;base64," + "A" * 300 + following + " after"
    source = json.dumps({"description": description})
    expected = {"description": "before " + following + " after"}
    path = tmp_path / "metadata.json"
    path.write_text(source)
    assert json.loads(read_indexable_text(path, 1024)) == expected
    assert json.loads(clean_index_text(source)) == expected
