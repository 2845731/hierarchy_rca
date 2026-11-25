"""Fatal LLM errors shared by configuration, inference and batch scheduling."""


class AllProvidersFailedError(RuntimeError):
    """API retries exhausted: stop the case and the batch, never fabricate output."""
    failure_kind = "llm_exhausted"


class LLMConfigurationError(AllProvidersFailedError):
    """Dependency/configuration failures require repair, not HTTP retries."""
    failure_kind = "llm_configuration"
