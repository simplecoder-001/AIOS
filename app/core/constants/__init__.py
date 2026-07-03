# app/core/constants/__init__.py
"""
AIOS core constants package.

A single, dependency-light import surface for every process-wide constant:
identity, states, events, permissions, models, languages, paths, settings,
limits, and errors.

Usage:
    from app.core.constants import ErrorCode, Role, VoiceState
    from app.core.constants import limits, paths
    from app.core.constants import APP_VERSION, MAX_PROMPT_TOKENS

Import order is intentional and cycle-free:
    app -> (states, events, permissions, models, languages, errors)
    app -> paths -> settings          (settings & paths import from app only)

No submodule imports another peer except `paths` and `settings`, which depend
only on `app`. This module performs NO side effects at import time — directory
creation remains an explicit call to `paths.ensure_dirs()`.
"""

from __future__ import annotations

# Submodules (exposed for namespaced access, e.g. constants.limits.clamp)
from app.core.constants import app as app
from app.core.constants import states as states
from app.core.constants import events as events
from app.core.constants import permissions as permissions
from app.core.constants import models as models
from app.core.constants import languages as languages
from app.core.constants import paths as paths
from app.core.constants import settings as settings
from app.core.constants import limits as limits
from app.core.constants import errors as errors

# Flat re-exports of each module's curated public API.
from app.core.constants.app import *          # noqa: F401,F403
from app.core.constants.states import *       # noqa: F401,F403
from app.core.constants.events import *       # noqa: F401,F403
from app.core.constants.permissions import *  # noqa: F401,F403
from app.core.constants.models import *       # noqa: F401,F403
from app.core.constants.languages import *    # noqa: F401,F403
from app.core.constants.paths import *        # noqa: F401,F403
from app.core.constants.settings import *     # noqa: F401,F403
from app.core.constants.limits import *       # noqa: F401,F403
from app.core.constants.errors import *       # noqa: F401,F403

# Convenience top-level version alias.
from app.core.constants.app import APP_VERSION as __version__


def _build_all() -> list[str]:
    """Aggregate every submodule's __all__ plus the submodule names."""
    names: list[str] = [
        "app",
        "states",
        "events",
        "permissions",
        "models",
        "languages",
        "paths",
        "settings",
        "limits",
        "errors",
        "__version__",
    ]
    for module in (
        app,
        states,
        events,
        permissions,
        models,
        languages,
        paths,
        settings,
        limits,
        errors,
    ):
        names.extend(getattr(module, "__all__", ()))
    # De-duplicate while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            unique.append(name)
    return unique


__all__ = _build_all()
