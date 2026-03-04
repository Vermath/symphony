"""Domain-specific error types."""


class SymphonyError(Exception):
    """Base class for service-level errors."""


class WorkflowError(SymphonyError):
    """Raised when workflow parsing/loading fails."""


class ConfigValidationError(SymphonyError):
    """Raised when dispatch preflight config validation fails."""


class TrackerError(SymphonyError):
    """Raised for issue tracker adapter failures."""


class WorkspaceError(SymphonyError):
    """Raised for workspace lifecycle failures."""


class TemplateParseError(SymphonyError):
    """Raised when prompt template cannot be parsed."""


class TemplateRenderError(SymphonyError):
    """Raised when prompt template cannot be rendered."""


class AppServerError(SymphonyError):
    """Raised for codex app-server protocol/runtime failures."""

