from pathlib import Path
from http.cookiejar import MozillaCookieJar
from urllib.request import HTTPCookieProcessor, build_opener

import pytest

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


@pytest.mark.parametrize("prefix", ["", "#HttpOnly_"])
def test_netscape_cookie_entries_reach_http_client(
    tmp_path: Path,
    httpserver,
    prefix: str,
) -> None:
    """HttpOnly rows are credentials, not comments: real clients must receive them."""
    cookies = tmp_path / "cookies.txt"
    cookies.write_text(
        "# Netscape HTTP Cookie File\n"
        f"{prefix}127.0.0.1\tFALSE\t/\tFALSE\t2145916800\tsid\tauthorized\n",
    )
    jar = MozillaCookieJar(str(cookies))
    jar.load()
    assert len(jar) == 1
    httpserver.expect_request(
        "/private",
        headers={"Cookie": "sid=authorized"},
    ).respond_with_data("private bytes")
    url = httpserver.url_for("/private").replace("localhost", "127.0.0.1")
    with build_opener(HTTPCookieProcessor(jar)).open(url) as response:
        assert response.status == 200
        assert response.read() == b"private bytes"
    httpserver.check_assertions()
    assert has_netscape_cookie_entries(cookies) is True
