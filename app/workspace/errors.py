"""Fail-closed workspace error hierarchy."""


class WorkspaceError(RuntimeError):
    """Base class for workspace failures safe to surface as structured errors."""


class WorkspaceSecurityError(WorkspaceError):
    """A filesystem or policy invariant was violated."""


class InvalidRelativePathError(WorkspaceSecurityError, ValueError):
    pass


class PathNotAllowedError(WorkspaceSecurityError, PermissionError):
    pass


class SensitivePathError(PathNotAllowedError):
    pass


class UnsafeFilesystemEntryError(WorkspaceSecurityError):
    pass


class UnsafeFileContentError(WorkspaceSecurityError):
    pass


class QuotaExceededError(WorkspaceSecurityError):
    pass


class CaseCollisionError(WorkspaceSecurityError):
    pass


class WorkspaceExistsError(WorkspaceSecurityError):
    pass


class OriginalProjectModifiedError(WorkspaceSecurityError):
    pass

