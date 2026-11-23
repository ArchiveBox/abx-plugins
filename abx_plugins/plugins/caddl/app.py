"""Expose the CAD viewer templates and static assets through Django app discovery."""

from django.apps import AppConfig as DjangoAppConfig


class AppConfig(DjangoAppConfig):
    name = "abx_plugins.plugins.caddl"
    label = "abx_caddl"
