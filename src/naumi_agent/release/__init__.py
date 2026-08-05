"""Release artifact construction, installed slots, and atomic activation."""

from naumi_agent.release.slots import (
    RELEASE_ACTIVE_POINTER_POLICY,
    RELEASE_BOOT_RECEIPT_POLICY,
    RELEASE_SLOT_POLICY,
    ReleaseActivePointer,
    ReleaseInstalledSlot,
    ReleaseSlotBootReceipt,
    ReleaseSlotError,
    ReleaseSlotStore,
    host_release_target,
)

__all__ = [
    "RELEASE_ACTIVE_POINTER_POLICY",
    "RELEASE_BOOT_RECEIPT_POLICY",
    "RELEASE_SLOT_POLICY",
    "ReleaseActivePointer",
    "ReleaseInstalledSlot",
    "ReleaseSlotBootReceipt",
    "ReleaseSlotError",
    "ReleaseSlotStore",
    "host_release_target",
]
