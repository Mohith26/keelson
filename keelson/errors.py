"""Exception hierarchy for keelson.

Everything that can go wrong at model-compile time raises a subclass of
ModelError and carries the source line so the message points at the .ks file
rather than at a stack frame inside the parser.
"""


class KeelsonError(Exception):
    pass


class ModelError(KeelsonError):
    """Raised while lexing, parsing or resolving a type model."""

    def __init__(self, message, line=None, col=None):
        self.message = message
        self.line = line
        self.col = col
        if line is None:
            super().__init__(message)
        else:
            super().__init__("line %d:%d: %s" % (line, col or 0, message))


class LexError(ModelError):
    pass


class ParseError(ModelError):
    pass


class ResolveError(ModelError):
    pass


class QueryError(KeelsonError):
    """Raised for a malformed filter expression or an unknown field in a query."""


class StoreError(KeelsonError):
    """Raised when a store is asked to do something its backend cannot do."""


class MigrationError(KeelsonError):
    """Raised when a model change cannot be applied without losing data."""
