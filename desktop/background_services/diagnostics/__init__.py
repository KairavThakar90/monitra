"""Opt-in runtime diagnostics. Nothing here runs in a normal session."""
from background_services.diagnostics.resource_monitor import (
    ResourceMonitorService,
    ResourceSample,
    diagnostics_enabled,
)

__all__ = ["ResourceMonitorService", "ResourceSample", "diagnostics_enabled"]
