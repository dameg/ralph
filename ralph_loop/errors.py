class RalphError(Exception):
    """Base error displayed without a Python traceback."""


class ConfigError(RalphError):
    """Invalid configuration or task contract."""


class GitError(RalphError):
    """Unsafe or failed Git operation."""


class AgentError(RalphError):
    """Agent failed or returned an invalid result."""


class AgentInfrastructureError(AgentError):
    """Agent process could not complete for an infrastructure reason."""


class GateError(RalphError):
    """A deterministic quality gate failed."""


class ScopeError(RalphError):
    """A role changed a file outside its allowed scope."""
