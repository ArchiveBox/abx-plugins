"""The runtime trust bundle must reject the compromised emSign CA chain."""

import ssl

import certifi


def test_certifi_does_not_trust_compromised_emsign_root() -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=certifi.where())
    roots = context.get_ca_certs()
    assert roots
    assert not any(
        value == "emSign ECC Root CA - G3"
        for certificate in roots
        for rdn in certificate["subject"]
        for _, value in rdn
    )
