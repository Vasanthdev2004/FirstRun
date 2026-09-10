"""Public session shape, separate from opaque tokens and OAuth configuration."""

from firstrun.domain.web import SafeText, WebModel


class SessionUserView(WebModel):
    id: int
    login: SafeText


class SessionView(WebModel):
    authenticated: bool
    configured: bool
    user: SessionUserView | None
    csrf_token: SafeText | None
    can_write: bool
    reason: SafeText | None


__all__ = ["SessionUserView", "SessionView"]
