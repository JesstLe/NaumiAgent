"""Release artifact construction, installed slots, and atomic activation."""

from naumi_agent.release.launcher import (
    RELEASE_LAUNCH_RESOLUTION_POLICY,
    ReleaseLaunchResolution,
    default_release_root,
    resolve_launch,
)
from naumi_agent.release.slots import (
    RELEASE_ACTIVE_POINTER_POLICY,
    RELEASE_BOOT_RECEIPT_POLICY,
    RELEASE_SLOT_POLICY,
    ReleaseActivePointer,
    ReleaseInstalledSlot,
    ReleaseSlotBootReceipt,
    ReleaseSlotError,
    ReleaseSlotStore,
    ResolvedBootedReleaseSlot,
    host_release_target,
)

__all__ = [
    "RELEASE_ACTIVE_POINTER_POLICY",
    "RELEASE_BOOT_RECEIPT_POLICY",
    "RELEASE_SLOT_POLICY",
    "RELEASE_LAUNCH_RESOLUTION_POLICY",
    "ReleaseActivePointer",
    "ReleaseInstalledSlot",
    "ReleaseLaunchResolution",
    "ReleaseSlotBootReceipt",
    "ReleaseSlotError",
    "ReleaseSlotStore",
    "ResolvedBootedReleaseSlot",
    "default_release_root",
    "host_release_target",
    "resolve_launch",
]
