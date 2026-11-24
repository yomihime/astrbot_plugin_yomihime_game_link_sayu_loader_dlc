class Unsupported(ValueError):
    """A requested plugin operation exceeds the available host capabilities."""


class OfflineNetwork(PermissionError):
    """An isolated offline profile attempted a network operation."""


class StartupTransferLimit(Unsupported):
    """The generation's cumulative startup transfer window has expired."""
