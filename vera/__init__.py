"""VERA — Version-Explicit Retrieval & Answering for clinical-trial protocols.

Answers a coordinator's question about what a trial protocol *currently*
requires, grounded in the governing version and always cited. Abstains rather
than guessing when the protocol does not cover the question.

Public entry points::

    from vera import answer_question, build_registry, get_backend

    result = answer_question("Is concomitant metformin permitted?", trial_id="412")

The CLI is ``python -m vera``; the web UI is ``web.create_app()``.

Everything is exported lazily. Importing this package pulls in no submodule, no
optional provider SDK, and no protocol data -- so ``python -m vera.ingest`` and
friends behave predictably, and a missing optional dependency only matters if
you actually use it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

__version__ = "1.0.0"

# public name -> defining submodule
_EXPORTS = {
    "ABSTAIN": "vera.pipeline",
    "answer_question": "vera.pipeline",
    "clear_cache": "vera.pipeline",
    "load_manifest": "vera.pipeline",
    "trial_versions": "vera.pipeline",

    "Passage": "vera.models",
    "Result": "vera.models",
    "Retrieval": "vera.models",
    "Usage": "vera.models",

    "Settings": "vera.config",
    "load_dotenv": "vera.config",
    "settings": "vera.config",

    "RegistryEntry": "vera.registry",
    "ProtocolRegistry": "vera.registry",
    "build_registry": "vera.registry",
    "resolve_current_version": "vera.registry",

    "build_manifest": "vera.ingest",
    "retrieve": "vera.retrieval",

    "build_work_item": "vera.escalation",
    "default_title": "vera.escalation",
    "get_escalation_sink": "vera.escalation",
    "to_ado_payload": "vera.escalation",

    "BACKEND_CHOICES": "vera.backends",
    "BackendError": "vera.backends",
    "FatalBackendError": "vera.backends",
    "LLMBackend": "vera.backends",
    "RetryableBackendError": "vera.backends",
    "get_backend": "vera.backends",
}

__all__ = ["__version__", *sorted(_EXPORTS)]


def __getattr__(name: str):
    try:
        module_name = _EXPORTS[name]
    except KeyError:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}") from None
    import importlib
    value = getattr(importlib.import_module(module_name), name)
    globals()[name] = value          # cache so the hook runs at most once per name
    return value


def __dir__() -> list[str]:
    return sorted(__all__)


if TYPE_CHECKING:  # editors and type checkers want the real symbols
    from .backends import (
        BACKEND_CHOICES,
        BackendError,
        FatalBackendError,
        LLMBackend,
        RetryableBackendError,
        get_backend,
    )
    from .config import Settings, load_dotenv, settings
    from .escalation import (
        build_work_item,
        default_title,
        get_escalation_sink,
        to_ado_payload,
    )
    from .ingest import build_manifest
    from .models import Passage, Result, Retrieval, Usage
    from .pipeline import (
        ABSTAIN,
        answer_question,
        clear_cache,
        load_manifest,
        trial_versions,
    )
    from .registry import (
        ProtocolRegistry,
        RegistryEntry,
        build_registry,
        resolve_current_version,
    )
    from .retrieval import retrieve
