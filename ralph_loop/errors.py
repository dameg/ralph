class RalphError(Exception):
    """Base error displayed without a Python traceback."""


class ConfigError(RalphError):
    """Invalid configuration or task contract."""


class GitError(RalphError):
    """Unsafe or failed Git operation."""


class AgentError(RalphError):
    """Agent failed or returned an invalid result."""


class AgentInfrastructureError(AgentError):
    """The role could not be executed because its runtime failed."""


class AgentContractError(AgentError):
    """The role returned an invalid machine or artifact contract."""


class GateError(RalphError):
    """Base class for quality-gate errors."""


class GateFailure(GateError):
    """A quality gate ran successfully and rejected the candidate."""


class GateInfrastructureError(GateError):
    """A quality gate could not produce a deterministic result."""


class ScopeError(RalphError):
    """A role changed a file outside its allowed scope."""


class RuntimeBusyError(RalphError):
    """Another Ralph process owns the runtime lock."""


class RalphInternalError(RalphError):
    """Ralph hit an unexpected internal workflow error."""
