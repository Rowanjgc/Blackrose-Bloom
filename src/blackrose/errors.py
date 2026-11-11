"""Failures shared by request admission, turn execution and the desktop session."""


class ChatBusy(RuntimeError):
    pass


class InputRejected(ValueError):
    pass


class VisionUnsupported(InputRejected):
    pass


class RecoveryRequired(RuntimeError):
    """A completed effect is retained, but inference cannot safely continue."""
