# app/core/event_bus/events/gui_events.py

"""
GUI events for AIOS.

These events power the entire FG5 GUI & User Experience system.

Components:
    - Main Dashboard
    - System Tray
    - 2D Companion
    - 3D Companion
    - Smart Cursor
    - Notifications
    - Overlays
    - Theme Manager
    - Window Manager
    - Animation System

Design goals:
    - Immutable event objects
    - Thread-safe transport
    - Event replay support
    - State synchronization
    - Low allocation overhead
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from app.core.event_bus.event_priority import EventPriority
from app.core.event_bus.event_types import Event, EventCategory


class GuiEventType(StrEnum):
    """
    Canonical GUI event names.
    """

    # Application Window
    WINDOW_OPENED = "gui.window.opened"
    WINDOW_CLOSED = "gui.window.closed"
    WINDOW_MINIMIZED = "gui.window.minimized"
    WINDOW_MAXIMIZED = "gui.window.maximized"
    WINDOW_FOCUSED = "gui.window.focused"
    WINDOW_MOVED = "gui.window.moved"
    WINDOW_RESIZED = "gui.window.resized"

    # Dashboard
    DASHBOARD_OPENED = "gui.dashboard.opened"
    DASHBOARD_CLOSED = "gui.dashboard.closed"
    DASHBOARD_REFRESH_REQUESTED = "gui.dashboard.refresh"

    # System Tray
    TRAY_STARTED = "gui.tray.started"
    TRAY_MENU_CLICKED = "gui.tray.menu_clicked"

    # Notifications
    NOTIFICATION_CREATED = "gui.notification.created"
    NOTIFICATION_DISMISSED = "gui.notification.dismissed"

    # Theme
    THEME_CHANGED = "gui.theme.changed"

    # Companion
    COMPANION_STARTED = "gui.companion.started"
    COMPANION_STOPPED = "gui.companion.stopped"
    COMPANION_STATE_CHANGED = "gui.companion.state_changed"
    COMPANION_ANIMATION_STARTED = (
        "gui.companion.animation.started"
    )
    COMPANION_ANIMATION_FINISHED = (
        "gui.companion.animation.finished"
    )

    # Smart Cursor
    SMART_CURSOR_ENABLED = "gui.smart_cursor.enabled"
    SMART_CURSOR_DISABLED = "gui.smart_cursor.disabled"
    SMART_CURSOR_TARGET_CHANGED = (
        "gui.smart_cursor.target_changed"
    )

    # Overlay
    OVERLAY_OPENED = "gui.overlay.opened"
    OVERLAY_CLOSED = "gui.overlay.closed"

    # Errors
    GUI_ERROR = "gui.error"


@dataclass(slots=True, kw_only=True)
class GuiEvent(Event):
    """
    Base GUI event.

    Shared metadata for all GUI events.
    """

    name: str
    priority: EventPriority = EventPriority.NORMAL
    payload: dict[str, Any] = field(default_factory=dict)

    event_id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    category: EventCategory = field(
        default=EventCategory.GUI,
        init=False,
    )

    correlation_id: str | None = None
    causation_id: str | None = None


# ============================================================================
# Window Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class WindowOpenedEvent(GuiEvent):
    name: str = field(
        default=GuiEventType.WINDOW_OPENED,
        init=False,
    )

    window_id: str
    title: str | None = None


@dataclass(slots=True, kw_only=True)
class WindowClosedEvent(GuiEvent):
    name: str = field(
        default=GuiEventType.WINDOW_CLOSED,
        init=False,
    )

    window_id: str


@dataclass(slots=True, kw_only=True)
class WindowMinimizedEvent(GuiEvent):
    name: str = field(
        default=GuiEventType.WINDOW_MINIMIZED,
        init=False,
    )

    window_id: str


@dataclass(slots=True, kw_only=True)
class WindowMaximizedEvent(GuiEvent):
    name: str = field(
        default=GuiEventType.WINDOW_MAXIMIZED,
        init=False,
    )

    window_id: str


@dataclass(slots=True, kw_only=True)
class WindowFocusedEvent(GuiEvent):
    name: str = field(
        default=GuiEventType.WINDOW_FOCUSED,
        init=False,
    )

    window_id: str


@dataclass(slots=True, kw_only=True)
class WindowMovedEvent(GuiEvent):
    name: str = field(
        default=GuiEventType.WINDOW_MOVED,
        init=False,
    )

    window_id: str
    x: int
    y: int


@dataclass(slots=True, kw_only=True)
class WindowResizedEvent(GuiEvent):
    name: str = field(
        default=GuiEventType.WINDOW_RESIZED,
        init=False,
    )

    window_id: str
    width: int
    height: int


# ============================================================================
# Dashboard Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class DashboardOpenedEvent(GuiEvent):
    name: str = field(
        default=GuiEventType.DASHBOARD_OPENED,
        init=False,
    )


@dataclass(slots=True, kw_only=True)
class DashboardClosedEvent(GuiEvent):
    name: str = field(
        default=GuiEventType.DASHBOARD_CLOSED,
        init=False,
    )


@dataclass(slots=True, kw_only=True)
class DashboardRefreshRequestedEvent(GuiEvent):
    name: str = field(
        default=GuiEventType.DASHBOARD_REFRESH_REQUESTED,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.HIGH,
        init=False,
    )


# ============================================================================
# System Tray Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class TrayStartedEvent(GuiEvent):
    name: str = field(
        default=GuiEventType.TRAY_STARTED,
        init=False,
    )


@dataclass(slots=True, kw_only=True)
class TrayMenuClickedEvent(GuiEvent):
    name: str = field(
        default=GuiEventType.TRAY_MENU_CLICKED,
        init=False,
    )

    menu_id: str


# ============================================================================
# Notification Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class NotificationCreatedEvent(GuiEvent):
    name: str = field(
        default=GuiEventType.NOTIFICATION_CREATED,
        init=False,
    )

    notification_id: str
    title: str
    message: str
    severity: str = "info"


@dataclass(slots=True, kw_only=True)
class NotificationDismissedEvent(GuiEvent):
    name: str = field(
        default=GuiEventType.NOTIFICATION_DISMISSED,
        init=False,
    )

    notification_id: str


# ============================================================================
# Theme Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class ThemeChangedEvent(GuiEvent):
    name: str = field(
        default=GuiEventType.THEME_CHANGED,
        init=False,
    )

    theme_name: str


# ============================================================================
# Companion Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class CompanionStartedEvent(GuiEvent):
    name: str = field(
        default=GuiEventType.COMPANION_STARTED,
        init=False,
    )

    mode: str  # companion_2d | companion_3d


@dataclass(slots=True, kw_only=True)
class CompanionStoppedEvent(GuiEvent):
    name: str = field(
        default=GuiEventType.COMPANION_STOPPED,
        init=False,
    )

    mode: str


@dataclass(slots=True, kw_only=True)
class CompanionStateChangedEvent(GuiEvent):
    name: str = field(
        default=GuiEventType.COMPANION_STATE_CHANGED,
        init=False,
    )

    previous_state: str
    current_state: str


@dataclass(slots=True, kw_only=True)
class CompanionAnimationStartedEvent(GuiEvent):
    name: str = field(
        default=GuiEventType.COMPANION_ANIMATION_STARTED,
        init=False,
    )

    animation_name: str


@dataclass(slots=True, kw_only=True)
class CompanionAnimationFinishedEvent(GuiEvent):
    name: str = field(
        default=GuiEventType.COMPANION_ANIMATION_FINISHED,
        init=False,
    )

    animation_name: str


# ============================================================================
# Smart Cursor Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class SmartCursorEnabledEvent(GuiEvent):
    name: str = field(
        default=GuiEventType.SMART_CURSOR_ENABLED,
        init=False,
    )


@dataclass(slots=True, kw_only=True)
class SmartCursorDisabledEvent(GuiEvent):
    name: str = field(
        default=GuiEventType.SMART_CURSOR_DISABLED,
        init=False,
    )


@dataclass(slots=True, kw_only=True)
class SmartCursorTargetChangedEvent(GuiEvent):
    name: str = field(
        default=GuiEventType.SMART_CURSOR_TARGET_CHANGED,
        init=False,
    )

    target_id: str | None = None
    x: int | None = None
    y: int | None = None


# ============================================================================
# Overlay Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class OverlayOpenedEvent(GuiEvent):
    name: str = field(
        default=GuiEventType.OVERLAY_OPENED,
        init=False,
    )

    overlay_id: str


@dataclass(slots=True, kw_only=True)
class OverlayClosedEvent(GuiEvent):
    name: str = field(
        default=GuiEventType.OVERLAY_CLOSED,
        init=False,
    )

    overlay_id: str


# ============================================================================
# Error Events
# ============================================================================


@dataclass(slots=True, kw_only=True)
class GuiErrorEvent(GuiEvent):
    name: str = field(
        default=GuiEventType.GUI_ERROR,
        init=False,
    )
    priority: EventPriority = field(
        default=EventPriority.HIGH,
        init=False,
    )

    component: str
    message: str
    exception_type: str | None = None
    recoverable: bool = True


# ============================================================================
# Public API
# ============================================================================

__all__ = [
    "GuiEventType",
    "GuiEvent",
    "WindowOpenedEvent",
    "WindowClosedEvent",
    "WindowMinimizedEvent",
    "WindowMaximizedEvent",
    "WindowFocusedEvent",
    "WindowMovedEvent",
    "WindowResizedEvent",
    "DashboardOpenedEvent",
    "DashboardClosedEvent",
    "DashboardRefreshRequestedEvent",
    "TrayStartedEvent",
    "TrayMenuClickedEvent",
    "NotificationCreatedEvent",
    "NotificationDismissedEvent",
    "ThemeChangedEvent",
    "CompanionStartedEvent",
    "CompanionStoppedEvent",
    "CompanionStateChangedEvent",
    "CompanionAnimationStartedEvent",
    "CompanionAnimationFinishedEvent",
    "SmartCursorEnabledEvent",
    "SmartCursorDisabledEvent",
    "SmartCursorTargetChangedEvent",
    "OverlayOpenedEvent",
    "OverlayClosedEvent",
    "GuiErrorEvent",
]