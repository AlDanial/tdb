"""Language profiles: everything language- or adapter-specific in one place."""

from tdb.languages.base import (
    AdapterNotFoundError,
    AdapterQuirks,
    AdapterSpec,
    LanguageNotSupportedError,
    LanguageProfile,
    Presentation,
    ProfileCapabilities,
)

__all__ = [
    "AdapterNotFoundError",
    "AdapterQuirks",
    "AdapterSpec",
    "LanguageNotSupportedError",
    "LanguageProfile",
    "Presentation",
    "ProfileCapabilities",
]
