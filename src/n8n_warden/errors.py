"""The one exception type the CLI treats as expected."""


class Fatal(Exception):
    """Unrecoverable but anticipated — printed cleanly rather than traced.

    Anything raised as Fatal is a message for the operator, not a bug report.
    Genuine defects should propagate as ordinary exceptions so the traceback
    survives.
    """
