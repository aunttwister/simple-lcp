"""Custom exception hierarchy for the LCP gateway.

Every exception carries:
  - ``code``        — stable machine-readable error code (``LCP-XXXX``)
  - ``status_code`` — HTTP status code to return to the client
  - ``details``     — optional sanitized detail payload

The code space is grouped by category:
  1xxx — client authentication / authorization
  2xxx — upstream provider errors
  3xxx — chain / circuit breaker failures
  4xxx — client request errors (policy, invalid body, config)
  5xxx — internal gateway errors
"""


class LCPError(Exception):
    """Base exception for all LCP gateway errors."""

    code = "LCP-0000"
    status_code = 500
    # Optional client-safe override for the message. When set, ``to_dict()``
    # returns this instead of the (possibly sensitive) exception message.
    client_message: str | None = None

    def __init__(self, message: str = "", *, details: dict | None = None):
        super().__init__(message)
        self.details = details or {}

    def to_dict(self) -> dict:
        """Return a client-safe, sanitized error payload.

        Only ``code`` and ``message`` are guaranteed. Subclasses may attach
        extra ``details`` (always assumed to be sanitized before raising).
        """
        message = self.client_message or (str(self) or self.__class__.__name__)
        payload = {"code": self.code, "message": message}
        if self.details:
            payload["details"] = self.details
        return payload


class ConfigError(LCPError):
    """Configuration validation or loading error."""
    code = "LCP-4001"
    status_code = 500


class AuthError(LCPError):
    """Client authentication or authorization failure."""
    code = "LCP-1001"
    status_code = 401


class ForbiddenError(LCPError):
    """Client authenticated, but lacks permission for the requested resource.

    Distinct from ``AuthError`` (missing/invalid credentials): here the key is
    valid but not allowed for the target profile.
    """
    code = "LCP-1003"
    status_code = 403


class CreditExhaustedError(LCPError):
    """User or team has exhausted their credit limit."""
    code = "LCP-1002"
    status_code = 429


class ProviderError(LCPError):
    """Upstream provider returned an error."""
    code = "LCP-2000"
    status_code = 502


class ProviderTimeoutError(ProviderError):
    """Upstream provider timed out or was unreachable."""
    code = "LCP-2001"
    status_code = 504


class ProviderRateLimitError(ProviderError):
    """Upstream provider rate limited us."""
    code = "LCP-2002"
    status_code = 429


class ProviderAuthError(ProviderError):
    """Upstream provider rejected our API key."""
    code = "LCP-2003"
    status_code = 502


class ProviderCreditsError(ProviderError):
    """Upstream provider account is out of credits / insufficient balance.

    A provider-side condition (e.g. opencode ``CreditsError``): the account
    needs top-up. It will not resolve by retrying the same request, so it
    should trip the circuit breaker and fall back to another provider rather
    than being re-attempted against the same drained account.
    """
    code = "LCP-2006"
    status_code = 402


class ProviderBadRequestError(ProviderError):
    """Upstream provider returned HTTP 400 — the request itself is invalid.

    These errors should NOT trigger fallback to another provider because
    the problem is in the request body, not the provider's availability.
    """
    code = "LCP-2004"
    status_code = 400


class ProviderInternalError(ProviderError):
    """Upstream provider returned a 5xx server error.

    Transient by nature — safe to retry, and safe to fall back to another
    provider when retries are exhausted.
    """
    code = "LCP-2005"
    status_code = 502


class AllProvidersFailedError(LCPError):
    """All providers in chain are unavailable."""
    code = "LCP-3001"
    status_code = 502
    # Don't leak provider names / failure details to the client — those go to
    # the server logs only.
    client_message = "All configured providers failed to fulfill the request. Please try again shortly."


class RequestUnsupportedError(LCPError):
    """Every provider that was actually tried rejected the request body as invalid.

    Distinct from ``AllProvidersFailedError``: there, providers were unavailable or broke;
    here, every provider answered, and every one said "this request is wrong". The caller's
    body is the problem, so the honest status is 4xx, not 502. Measured 2026-10-24: 24 of ~737
    `l2` requests in 72h walked the whole five-step chain and were answered 502, when every
    step had in fact returned a 400 (unsupported `reasoning_effort`, unsupported
    `response_format`) — the operator saw "the gateway is broken" for a request no provider in
    the chain could serve.
    """
    code = "LCP-3002"
    status_code = 400
    client_message = (
        "Every provider in this profile rejected the request as invalid. The request body asks "
        "for something no configured model supports (for example an unsupported "
        "response_format or reasoning_effort). Adjust the request and retry."
    )


class ToolBlockedError(LCPError):
    """Requested tool is blocked by profile policy."""
    code = "LCP-4002"
    status_code = 403


class RequestTooLargeError(LCPError):
    """Request exceeds every routable model's context window.

    Raised before forwarding when the token count of the request (plus an
    output reserve) is larger than the largest context window among the
    models the profile can route to — so no model could ever serve it.
    The client should compact/truncate or use a longer-context profile.
    """
    code = "LCP-4003"
    status_code = 413
    client_message = (
        "Request is too large for any model in this profile's context window. "
        "Compact or shorten the conversation and retry."
    )
