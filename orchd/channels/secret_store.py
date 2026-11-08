"""Secret references are resolved only through a source-scoped provider."""
import re


class SecretUnavailable(ValueError):
    pass


_REFERENCE = re.compile(r"os-store:[A-Za-z0-9][A-Za-z0-9._/-]*\Z")


def resolve(reference, *, adapter, source, provider):
    if not isinstance(reference, str) or not _REFERENCE.fullmatch(reference):
        raise SecretUnavailable("invalid secret reference")
    if any(part in ("", ".", "..") for part in reference[len("os-store:"):].split("/")):
        raise SecretUnavailable("invalid secret reference")
    if not adapter or not source or provider is None:
        raise SecretUnavailable("secret provider unavailable")
    try:
        value = provider.get(adapter, source, reference)
    except Exception:  # OS providers may include sensitive material in their errors.
        raise SecretUnavailable("secret unavailable for source") from None
    if not isinstance(value, bytes) or not value:
        raise SecretUnavailable("secret unavailable for source")
    return value


class FakeSecretStore:
    """In-memory provider for synthetic tests; never writes secrets to disk."""
    def __init__(self, values):
        self._values = dict(values)

    def get(self, adapter, source, reference):
        return self._values[(adapter, source, reference)]
