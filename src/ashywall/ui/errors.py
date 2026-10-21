"""User-facing descriptions of errors from online sources and downloads."""

from ..i18n import _
from ..providers.base import ProviderError
from ..services.http import HttpError, NetworkUnavailable, RateLimited, ResponseTooLarge
from ..services.remote_images import DownloadError


def error_text(error: Exception) -> str:
    if isinstance(error, NetworkUnavailable):
        return _(
            "The server could not be reached. Check your connection and try again."
        )
    if isinstance(error, RateLimited):
        return _("Too many requests in a short time. Wait a minute and try again.")
    if isinstance(error, ResponseTooLarge):
        return _(
            "The server sent more data than announced, so the transfer was stopped."
        )
    if isinstance(error, ProviderError):
        return _("The server sent data that AshyWall could not validate.")
    if isinstance(error, DownloadError):
        return _("The downloaded file was rejected: {reason}").format(reason=error)
    if isinstance(error, HttpError):
        if error.status is not None and 500 <= error.status < 600:
            return _(
                "The service is currently unavailable (HTTP {status}). Try again later."
            ).format(status=error.status)
        return _("The server answered with an error ({status}).").format(
            status=error.status or "?"
        )
    return str(error) or error.__class__.__name__
