"""Media back end: FFmpeg/FFprobe, yt-dlp downloads, thumbnails, waveforms, export."""


class MediaError(Exception):
    """A media operation failed. ``str(exc)`` is safe to show to the user;
    technical detail (FFmpeg stderr etc.) goes to the log and ``detail``."""

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


class DependencyError(MediaError):
    """FFmpeg / FFprobe could not be found or run."""
