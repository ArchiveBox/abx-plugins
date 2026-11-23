from abx_plugins.plugins.networkgraph.app import is_boilerplate_url


def test_graph_ignores_shared_assets_and_widget_links():
    for url in (
        "https://getbootstrap.com/",
        "https://disqus.com/?ref_noscript",
        "https://disqus.com/privacy-policy",
        "https://disqus.com/embed/comments/?f=acm-prod",
        "https://acm-prod.disqus.com/embed.js",
        "https://docs.disqus.com/help/83/",
        "https://fonts.gstatic.com",
        "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js",
        "https://creativecommons.org/licenses/by/4.0/legalcode.en",
    ):
        assert is_boilerplate_url(url), url

    for url in (
        "https://getbootstrap.com/docs/5.3/components/buttons/",
        "https://blog.disqus.com/",
        "https://creativecommons.org/who-we-are/",
        "https://dl.acm.org/doi/10.1145/3607844",
    ):
        assert not is_boilerplate_url(url), url
