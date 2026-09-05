from enum import Enum


class ToolPermission(str, Enum):
    READ = "READ"
    WRITE = "WRITE"
    DANGEROUS = "DANGEROUS"

def has_permission(required: ToolPermission, granted: ToolPermission) -> bool:
    """Check if the granted permission satisfies the required permission."""
    levels = {
        ToolPermission.READ: 1,
        ToolPermission.WRITE: 2,
        ToolPermission.DANGEROUS: 3,
    }
    return levels[granted] >= levels[required]
