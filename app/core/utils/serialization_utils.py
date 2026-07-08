# app/core/utils/serialization_utils.py
"""
Generic object ↔ dict/JSON/bytes serialization helpers.

Provides the canonical round-trip primitives for converting dataclasses (plain
and frozen), named-tuples, and simple objects to/from dicts, JSON strings, and
UTF-8 bytes. These are used by the Event Bus (EventSerializer), the Queue layer
(QueueMessage envelopes), the RPC bridge (tool results for FG3), and the Audit
Logger (AuditEntry).

The module is deliberately conservative:

* Only handles dataclasses and objects with a ``to_dict()`` / ``from_dict()``
  protocol — no magic introspection of arbitrary classes (security boundary).
* Frozen dataclass attribute restoration uses ``object.__setattr__`` exactly as
  ``EventSerializer`` does — no bypassing checks, just the frozen constraint.
* ``init=False`` fields are explicitly preserved on round-trip (identity fields
  like ``event_id``, ``context_id``, ``created_at``).
* All encoding errors surface as :class:`ValidationError`.

This module is a **convenience layer** over :mod:`json_utils` and
:mod:`yaml_utils`; it does NOT replace domain-specific serializers (the
``EventSerializer`` class itself, the audit compressor, the vector store
protobuf converter). It just gives the other 90% of the codebase a simple
``to_json(obj)`` → ``from_json(Type, raw)`` pair.

Dependency order
----------------
Standard library → ``json_utils`` → ``exceptions.validation`` → here.
"""

from __future__ import annotations

import dataclasses
from dataclasses import is_dataclass
from typing import Any, Callable, Dict, List, Optional, Type, TypeVar, Union

from app.core.exceptions.validation import ValidationError
from app.core.utils.json_utils import json_decode, json_encode, json_bytes_decode, json_bytes_encode

__all__ = [
    "Serializable",
    "dict_from_dataclass",
    "dict_from_object",
    "object_from_dict",
    "object_from_dict_immutable",
    "to_dict",
    "from_dict",
    "to_json",
    "from_json",
    "try_from_json",
    "to_bytes",
    "from_bytes",
    "to_yaml",
    "from_yaml",
    "list_from_json",
    "list_to_json",
]

T = TypeVar("T")

Serializable = Union[Dict[str, Any], List[Any], str, int, float, bool, None]


def dict_from_dataclass(obj: Any, *, include_init_false: bool = True) -> Dict[str, Any]:
    """Convert a dataclass instance to a plain dict via ``dataclasses.asdict``.

    ``include_init_false=True`` ensures fields declared ``init=False`` (identity
    fields like ``event_id``, ``context_id``, ``created_at``) appear in the
    dict so they can be restored on the other side.
    """
    if not is_dataclass(obj) or not isinstance(obj, object):
        raise ValidationError(
            f"dict_from_dataclass expects a dataclass instance, got {type(obj).__name__}",
        )
    raw = dataclasses.asdict(obj)
    if not include_init_false:
        # Filter out fields that were not part of __init__ — leaving only
        # the "user-settable" surface.
        field_names = {f.name for f in dataclasses.fields(obj) if f.init}
        raw = {k: v for k, v in raw.items() if k in field_names}
    return raw


def dict_from_object(obj: Any) -> Dict[str, Any]:
    """Get a dict representation from an object implementing ``to_dict()``.

    Falls back to :func:`dataclasses.asdict` if the object is a dataclass and
    has no explicit ``to_dict``. Raises :class:`ValidationError` if neither
    protocol is available.
    """
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        return obj.to_dict()
    if is_dataclass(obj):
        return dict_from_dataclass(obj)
    raise ValidationError(
        f"Object of type {type(obj).__name__} has no to_dict() and is not a dataclass",
    )


def to_dict(obj: Any) -> Dict[str, Any]:
    """Convenience alias for :func:`dict_from_object`."""
    return dict_from_object(obj)


def object_from_dict(
    cls: Type[T],
    data: Dict[str, Any],
    *,
    strict: bool = True,
) -> T:
    """Construct an instance of ``cls`` from a dict via its ``from_dict(data)``
    classmethod, or via the default ``**data`` constructor.

    ``strict=True`` signals that missing required constructor arguments should
    raise (default). ``strict=False`` passes only the keys that the constructor
    accepts — useful for payloads from third-party sources that may have
    unknown extra fields.
    """
    if hasattr(cls, "from_dict") and callable(cls.from_dict):
        instance = cls.from_dict(data)
        if not isinstance(instance, cls):
            raise ValidationError(
                f"from_dict() on {cls.__name__} returned {type(instance).__name__} "
                f"instead of {cls.__name__}",
            )
        return instance

    if not is_dataclass(cls):
        raise ValidationError(
            f"{cls.__name__} is not a dataclass and has no from_dict() classmethod",
        )

    if not strict:
        # Only pass fields the constructor actually declares.
        field_names = {f.name for f in dataclasses.fields(cls) if f.init}
        data = {k: v for k, v in data.items() if k in field_names}

    try:
        instance = cls(**data)
    except TypeError as exc:
        raise ValidationError(
            f"Cannot construct {cls.__name__} from dict: {exc}",
        ) from exc

    # Restore init=False fields that were present in the dict but not accepted
    # by the constructor.
    _restore_init_false_fields(instance, data)
    return instance


def object_from_dict_immutable(
    cls: Type[T],
    data: Dict[str, Any],
    *,
    strict: bool = True,
) -> T:
    """Like :func:`object_from_dict` but fully *restoration-only* — applies
    ``object.__setattr__`` to every field matching a dataclass field name whose
    ``init=False``, after constructing from a subset that avoids frozen-field
    conflicts. This is the pattern used by :class:`EventSerializer` for frozen
    Event and EventContext instances.
    """
    if not is_dataclass(cls):
        raise ValidationError(
            f"{cls.__name__} is not a dataclass",
        )

    field_map = {f.name: f for f in dataclasses.fields(cls)}
    init_fields = {n for n, f in field_map.items() if f.init}
    frozen: bool = getattr(cls, "__dataclass_params__", None) is not None and cls.__dataclass_params__.frozen  # type: ignore[attr-defined]

    if frozen:
        init_data = {k: v for k, v in data.items() if k in init_fields}
        try:
            instance = cls(**init_data)
        except TypeError as exc:
            raise ValidationError(
                f"Cannot construct frozen {cls.__name__} from init-only dict: {exc}",
            ) from exc
    else:
        instance = object_from_dict(cls, data, strict=strict)

    _restore_init_false_fields(instance, data)
    return instance


def from_dict(cls: Type[T], data: Dict[str, Any]) -> T:
    """Convenience alias for :func:`object_from_dict_immutable` (restoration
    for both frozen and mutable dataclasses)."""
    return object_from_dict_immutable(cls, data)


def _restore_init_false_fields(instance: Any, data: Dict[str, Any]) -> None:
    """Write init=False fields back onto ``instance`` after construction."""
    if not is_dataclass(instance):
        return
    field_map = {f.name: f for f in dataclasses.fields(instance)}
    for name, value in data.items():
        field = field_map.get(name)
        if field is not None and not field.init:
            try:
                object.__setattr__(instance, name, value)
            except TypeError:
                pass  # frozen + already set (constructor populated it)


def to_json(obj: Any, **kwargs: Any) -> str:
    """Serialize an object to JSON string via its ``to_dict()``."""
    return json_encode(dict_from_object(obj), **kwargs)


def from_json(
    cls: Type[T],
    data: Union[str, bytes],
    *,
    strict: bool = True,
) -> T:
    """Parse a JSON string into a dict, then construct ``cls``."""
    parsed = json_decode(data)
    if not isinstance(parsed, dict):
        raise ValidationError(
            f"Expected JSON object (dict) to construct {cls.__name__}, "
            f"got {type(parsed).__name__}",
        )
    return object_from_dict_immutable(cls, parsed, strict=strict)


def try_from_json(
    cls: Type[T],
    data: Union[str, bytes],
    *,
    strict: bool = False,
    default: Optional[T] = None,
) -> Optional[T]:
    """Best-effort parse + construct — returns ``default`` on any failure."""
    try:
        return from_json(cls, data, strict=strict)
    except (ValidationError, ValueError, TypeError):
        return default


def to_bytes(obj: Any) -> bytes:
    """Serialize an object to compact UTF-8 JSON bytes."""
    return json_bytes_encode(dict_from_object(obj))


def from_bytes(cls: Type[T], data: bytes, *, strict: bool = True) -> T:
    """Reconstruct an object from UTF-8 JSON bytes."""
    parsed = json_bytes_decode(data)
    if not isinstance(parsed, dict):
        raise ValidationError(
            f"Expected JSON object (dict) to construct {cls.__name__}, "
            f"got {type(parsed).__name__}",
        )
    return object_from_dict_immutable(cls, parsed, strict=strict)


def to_yaml(obj: Any, **kwargs: Any) -> str:
    """Serialize an object to YAML string via its ``to_dict()``.

    Imports :mod:`yaml_utils` lazily to avoid a hard YAML dependency in every
    module that only needs JSON serialization.
    """
    from app.core.utils.yaml_utils import yaml_encode

    return yaml_encode(dict_from_object(obj), **kwargs)


def from_yaml(
    cls: Type[T],
    data: Union[str, bytes],
    *,
    strict: bool = True,
) -> T:
    """Parse YAML into a dict, then construct ``cls``."""
    from app.core.utils.yaml_utils import yaml_decode

    parsed = yaml_decode(data)
    if not isinstance(parsed, dict):
        raise ValidationError(
            f"Expected YAML mapping to construct {cls.__name__}, "
            f"got {type(parsed).__name__}",
        )
    return object_from_dict_immutable(cls, parsed, strict=strict)


def list_from_json(
    cls: Type[T],
    data: Union[str, bytes],
    *,
    strict: bool = True,
) -> List[T]:
    """Parse a JSON array of objects and construct each as ``cls``."""
    parsed = json_decode(data)
    if not isinstance(parsed, list):
        raise ValidationError(
            f"Expected JSON array to produce list of {cls.__name__}, "
            f"got {type(parsed).__name__}",
        )
    return [object_from_dict_immutable(cls, item, strict=strict) for item in parsed]


def list_to_json(items: List[Any], **kwargs: Any) -> str:
    """Serialize a list of dict-convertible objects to a JSON array."""
    dicts = [dict_from_object(item) for item in items]
    return json_encode(dicts, **kwargs)