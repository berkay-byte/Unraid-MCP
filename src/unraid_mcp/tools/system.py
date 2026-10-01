"""System information tools."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .. import queries
from ..client import UnraidClient
from ..config import Settings
from ..errors import UnraidGraphQLError
from ..formatting import (
    shape_flash,
    shape_metrics,
    shape_services,
    shape_system_info,
    shape_system_time,
)
from ._base import (
    MUTATING,
    READ_ONLY,
    feature_unsupported,
    get_app_context,
    guarded,
    require_confirm,
    safe_query,
    unsupported_field_error,
)


async def fetch_system_info(client: UnraidClient) -> dict[str, Any]:
    info = shape_system_info(await client.execute(queries.SYSTEM_INFO))
    # `flash` is a separate root query (not nested under `info`), fetched here
    # as a second, independently-degrading call so older API builds without
    # this field still return system info — just without flash device identity.
    flash = await safe_query(client, queries.FLASH, shape_flash, None)
    if flash is not None:
        info = {**info, "flash": flash}
    return info


async def fetch_system_time(
    client: UnraidClient, *, api_version: str | None = None
) -> dict[str, Any]:
    try:
        return shape_system_time(await client.execute(queries.SYSTEM_TIME))
    except UnraidGraphQLError as exc:
        if unsupported_field_error(exc):
            raise feature_unsupported(
                "system time info", requires="7.1+", api_version=api_version
            ) from None
        raise


async def fetch_metrics(client: UnraidClient, *, api_version: str | None = None) -> dict[str, Any]:
    try:
        return shape_metrics(await client.execute(queries.SYSTEM_METRICS))
    except UnraidGraphQLError as exc:
        if unsupported_field_error(exc):
            raise feature_unsupported(
                "live system metrics", requires="7.2+", api_version=api_version
            ) from None
        raise


async def fetch_services(
    client: UnraidClient, *, api_version: str | None = None
) -> list[dict[str, Any]]:
    try:
        return shape_services(await client.execute(queries.SERVICES))
    except UnraidGraphQLError as exc:
        if unsupported_field_error(exc):
            raise feature_unsupported("the services health list", api_version=api_version) from None
        raise


def register(mcp: MCPServer, settings: Settings) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def get_system_info(ctx: Context) -> dict[str, Any]:
        """Get Unraid host system information: OS/kernel, CPU, memory layout,
        motherboard, Unraid + API versions, and uptime."""
        info = await guarded(ctx, fetch_system_info)
        # Surface the startup-probed versions explicitly at the top level so
        # agents can self-diagnose capability gaps even if info.versions is
        # absent on this API build.
        app = get_app_context(ctx)
        return {
            **info,
            "api_version": app.api_version,
            "unraid_version": app.unraid_version,
        }

    @mcp.tool(annotations=READ_ONLY)
    async def get_system_metrics(ctx: Context) -> dict[str, Any]:
        """Get live utilization: total/per-core CPU %, memory/swap usage,
        temperatures."""
        api_version = get_app_context(ctx).api_version
        return await guarded(ctx, fetch_metrics, api_version=api_version)

    @mcp.tool(annotations=READ_ONLY)
    async def get_services(ctx: Context) -> list[dict[str, Any]]:
        """Health of the Unraid services stack (API, dynamix, etc.): name,
        online, uptime, version."""
        api_version = get_app_context(ctx).api_version
        return await guarded(ctx, fetch_services, api_version=api_version)

    @mcp.tool(annotations=READ_ONLY)
    async def get_system_time(ctx: Context) -> dict[str, Any]:
        """Get server time, timezone, and NTP config — correlate log timestamps
        and spot NTP misconfig."""
        api_version = get_app_context(ctx).api_version
        return await guarded(ctx, fetch_system_time, api_version=api_version)


async def do_start_flash_backup(
    client: UnraidClient,
    remote_name: str,
    source_path: str,
    destination_path: str,
    options: dict[str, Any] | None = None,
    confirm: bool = False,
    *,
    api_version: str | None = None,
) -> dict[str, Any]:
    """Start a flash-drive backup to a pre-configured rclone remote."""
    require_confirm(
        confirm,
        f"start a flash backup of '{source_path}' to remote '{remote_name}':'{destination_path}' "
        "(uploads data to the remote; may overwrite files there)",
    )
    for name, value in (
        ("remote_name", remote_name),
        ("source_path", source_path),
        ("destination_path", destination_path),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ToolError(f"{name} must be a non-empty string.")
    if options is not None and not isinstance(options, dict):
        raise ToolError("options must be an object of backup options (e.g. {'--dry-run': true}).")
    variables: dict[str, Any] = {
        "remoteName": remote_name,
        "sourcePath": source_path,
        "destinationPath": destination_path,
    }
    if options is not None:
        variables["options"] = options
    try:
        result = await client.execute(queries.INITIATE_FLASH_BACKUP, {"input": variables})
    except UnraidGraphQLError as exc:
        if unsupported_field_error(exc):
            raise feature_unsupported("flash backup", api_version=api_version) from None
        raise
    status = (result or {}).get("initiateFlashBackup")
    if not isinstance(status, dict) or not status.get("status"):
        raise ToolError("The Unraid API returned no status for the flash backup request.")
    return {"status": status["status"], "job_id": status.get("jobId")}


def register_mutations(mcp: MCPServer, settings: Settings) -> None:
    @mcp.tool(annotations=MUTATING)
    async def start_flash_backup(
        ctx: Context,
        remote_name: str,
        source_path: str,
        destination_path: str,
        options: dict[str, Any] | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Start a flash-drive backup to an rclone remote already configured on the
        server (typical source_path "/boot"). Uploads data to the remote and may
        overwrite files at destination_path. options: optional rclone flags, e.g.
        {"--dry-run": true}. Returns {"status", "job_id"}; the backup runs
        asynchronously. Requires confirm=true."""
        api_version = get_app_context(ctx).api_version
        return await guarded(
            ctx,
            do_start_flash_backup,
            remote_name,
            source_path,
            destination_path,
            options,
            confirm,
            api_version=api_version,
        )
