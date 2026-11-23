"""Interfaces supplied by a host to optional plugin web integrations."""

from typing import Any, Protocol


class Host(Protocol):
    def domain_favicons(self, request: Any, domains: list[str]) -> dict[str, str]: ...

    def group_domains(self, request: Any, hosts: list[str]) -> dict[str, str]: ...

    def outlink_records(
        self,
        request: Any,
        *,
        after: str | None = None,
        limit: int = 32,
    ) -> dict[str, Any]: ...

    def collection_records(
        self,
        request: Any,
        *,
        after: str | None = None,
        limit: int = 512,
    ) -> dict[str, Any]: ...

    def domain_records(
        self,
        request: Any,
        *,
        cursor: Any = None,
        limit: int = 16,
    ) -> dict[str, Any]: ...

    def snapshot_records(
        self,
        request: Any,
        *,
        domain: str,
        after: str | None = None,
        limit: int = 16,
    ) -> dict[str, Any]: ...

    def resolve_snapshot_urls(
        self,
        request: Any,
        urls: list[str],
    ) -> dict[str, str]: ...

    def process_records(
        self,
        request: Any,
        *,
        after: str | None = None,
        ids: tuple[str, ...] = (),
        limit: int = 128,
    ) -> dict[str, Any]: ...

    def request_config(self, request: Any = None) -> dict[str, Any]: ...

    def admin_context(self, request: Any) -> dict[str, Any]: ...

    def route_urls(self, request: Any) -> dict[str, str]: ...

    def authenticate(self, request: Any, *, api: bool = False) -> Any: ...

    def capture_context(
        self,
        request: Any,
        *,
        snapshot_id: str | None = None,
        crawl_id: str | None = None,
    ) -> dict[str, Any]: ...

    def websocket_request(self, scope: dict[str, Any]) -> Any: ...

    def browser_sources(self) -> dict[str, Any]: ...

    def browser_binding(
        self,
        *,
        persona_name: str | None = None,
        snapshot_id: str | None = None,
        crawl_id: str | None = None,
        browser_id: str | None = None,
        current: bool = False,
        create: bool = False,
    ) -> dict[str, Any]: ...

    def cli_command(self, plugin: str, command: str) -> list[str]: ...

    def collection_context(
        self,
        *,
        crawl_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]: ...
