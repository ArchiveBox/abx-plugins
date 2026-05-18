from pathlib import Path

from abx_plugins.plugins.base.utils import has_netscape_cookie_entries


def test_has_netscape_cookie_entries_rejects_missing_empty_and_header_only(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.txt"
    empty = tmp_path / "empty.txt"
    header_only = tmp_path / "header.txt"

    empty.write_text("")
    header_only.write_text(
        "\n".join(
            [
                "# Netscape HTTP Cookie File",
                "# https://curl.se/docs/http-cookies.html",
                "",
            ],
        ),
    )

    assert has_netscape_cookie_entries(missing) is False
    assert has_netscape_cookie_entries(empty) is False
    assert has_netscape_cookie_entries(header_only) is False


def test_has_netscape_cookie_entries_accepts_cookie_rows(tmp_path: Path) -> None:
    cookies = tmp_path / "cookies.txt"
    cookies.write_text(
        "\n".join(
            [
                "# Netscape HTTP Cookie File",
                ".example.com\tTRUE\t/\tFALSE\t2145916800\tsid\tabc123",
                "",
            ],
        ),
    )

    assert has_netscape_cookie_entries(cookies) is True
