"""Find article PDFs in NLM's public PMC Article Datasets."""

import asyncio
import hashlib
import re
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from xml.etree import ElementTree

import aiohttp
from loguru import logger
from bs4 import BeautifulSoup

from parse.parse import parse_ids_from_text
from providers import publisher

SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
DATASET = "https://pmc-oa-opendata.s3.amazonaws.com/"
TIMEOUT = aiohttp.ClientTimeout(total=30)
USER_AGENT = (
    "Mozilla/5.0 (compatible; papers-dl/0.0.25; +https://github.com/benmuth/papers-dl)"
)
S3_NAMESPACE = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}


async def get_urls(session, identifier):
    identifier = unquote(identifier.strip())
    if identifier.startswith(("http://", "https://")):
        identifier = urlsplit(identifier).path.strip("/")
    pmcid = re.search(r"\bPMC\d+(?:\.\d+)?\b", identifier, re.IGNORECASE)
    dois = parse_ids_from_text(identifier, ["doi"])
    if pmcid:
        identifier = pmcid.group().upper()
        query = None
    elif dois:
        identifier = dois[0]["id"]
        query = f'DOI:"{identifier}"'
    elif identifier.isdigit():
        query = f"EXT_ID:{identifier} AND SRC:MED"
    else:
        return []

    logger.info("Searching PubMed Central: {}", identifier)
    try:
        if query:
            async with session.get(
                SEARCH,
                params={"query": query, "format": "json"},
                timeout=TIMEOUT,
            ) as response:
                response.raise_for_status()
                result = await response.json()
            records = result.get("resultList", {}).get("result", [])
            pmcids = [
                record["pmcid"]
                for record in records
                if record.get("pmcid")
                and (
                    record.get("doi", "").lower() == identifier.lower()
                    if dois
                    else str(record.get("pmid")) == identifier
                )
            ]
        else:
            pmcids = [identifier]

        urls = []
        for pmcid in dict.fromkeys(pmcids):
            if not re.fullmatch(r"PMC\d+(?:\.\d+)?", pmcid):
                continue
            if "." in pmcid:
                keys = [f"metadata/{pmcid}.json"]
            else:
                # List available metadata, not article files: supplements can
                # also be PDFs, and version 1 is not necessarily distributed.
                keys = []
                params = {"list-type": "2", "prefix": f"metadata/{pmcid}."}
                while True:
                    async with session.get(
                        DATASET, params=params, timeout=TIMEOUT
                    ) as response:
                        response.raise_for_status()
                        listing = ElementTree.fromstring(await response.read())
                    keys.extend(
                        key.text
                        for key in listing.findall("s3:Contents/s3:Key", S3_NAMESPACE)
                        if re.fullmatch(rf"metadata/{pmcid}\.\d+\.json", key.text or "")
                    )
                    token = listing.findtext(
                        "s3:NextContinuationToken", namespaces=S3_NAMESPACE
                    )
                    if not token:
                        break
                    params["continuation-token"] = token

            versions = []
            for key in keys:
                async with session.get(DATASET + key, timeout=TIMEOUT) as response:
                    # Only licensed, publicly distributed versions are in S3.
                    if response.status == 404:
                        continue
                    response.raise_for_status()
                    # NLM's S3 JSON objects are served as binary/octet-stream.
                    metadata = await response.json(content_type=None)
                if metadata.get("pdf_url"):
                    versions.append(metadata)
            # Prefer published articles to author manuscripts, then the newest
            # deposited version of that kind. Do not pick a supplemental PDF.
            versions.sort(
                key=lambda v: (v.get("is_manuscript", False), -int(v["version"]))
            )
            for metadata in versions:
                pdf_url = metadata["pdf_url"]
                url = urlsplit(pdf_url)
                if url.scheme == "s3" and url.netloc == "pmc-oa-opendata":
                    pdf_url = urlunsplit(
                        (
                            "https",
                            "pmc-oa-opendata.s3.amazonaws.com",
                            url.path,
                            url.query,
                            "",
                        )
                    )
                if urlsplit(pdf_url).scheme in ("https", "http"):
                    urls.append(pdf_url)
                    break
            if not versions and "." not in pmcid:
                # Some author manuscripts expose only text/XML in the dataset.
                # Their public article page still advertises the original PDF.
                urls.extend(
                    await publisher.get_urls(
                        session,
                        f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/",
                        headers={
                            "User-Agent": session.headers.get("User-Agent", USER_AGENT)
                        },
                    )
                )
        return urls
    except (
        aiohttp.ClientError,
        asyncio.TimeoutError,
        ValueError,
        ElementTree.ParseError,
    ) as error:
        logger.info("Couldn't find a PubMed Central PDF: {}", error)
        return []


async def complete_pdf_download(session, url, content, headers):
    """Complete PMC's automatic SHA-256 download proof, without executing JS.

    This is the public viewer's POW_CHALLENGE protocol, not a login or CAPTCHA.
    Bound the work and recognize it only on NCBI's PDF download endpoint.
    """
    if (
        url.scheme != "https"
        or url.host != "pmc.ncbi.nlm.nih.gov"
        or "/pdf/" not in url.path
    ):
        return None
    page = BeautifulSoup(content, "html.parser")
    script = "\n".join(tag.get_text() for tag in page.find_all("script", src=False))
    challenge = re.search(r'const POW_CHALLENGE = "([A-Za-z0-9_:.-]+)"', script)
    difficulty = re.search(r'const POW_DIFFICULTY = "([1-4])"', script)
    cookie = re.search(r'const POW_COOKIE_NAME = "(cloudpmc-viewer-pow)"', script)
    if not (challenge and difficulty and cookie):
        return None
    challenge = challenge.group(1)
    prefix = "0" * int(difficulty.group(1))

    def solve():
        for nonce in range(1 << 24):
            if (
                hashlib.sha256(f"{challenge}{nonce}".encode())
                .hexdigest()
                .startswith(prefix)
            ):
                return nonce
        return None

    nonce = await asyncio.to_thread(solve)
    if nonce is None:
        return None
    session.cookie_jar.update_cookies(
        {cookie.group(1): quote(f"{challenge},{nonce}", safe=":")}, response_url=url
    )
    async with session.get(url, timeout=TIMEOUT, headers=headers) as response:
        response.raise_for_status()
        return await response.read(), str(response.url)
