"""Minimal test runner.

keelson has no third party dependencies, and I did not want the test suite to
be the thing that introduced one. Tests are plain functions named test_* in
modules registered below; this collects them, runs them, counts assertions and
prints a summary. Assertion counting matters here because several tests loop
over thousands of generated cases, and "42 tests passed" hides how much was
actually checked.
"""

import time
import traceback

_ASSERTS = [0]


def bump(n=1):
    _ASSERTS[0] += n


def assert_count():
    return _ASSERTS[0]


def ok(cond, msg=""):
    bump()
    if not cond:
        raise AssertionError(msg or "expected a truthy value")


def eq(actual, expected, msg=""):
    bump()
    if actual != expected:
        raise AssertionError(
            "%s\n  expected: %r\n  actual:   %r" % (msg or "values differ", expected, actual)
        )


def close(actual, expected, tol=1e-9, msg=""):
    bump()
    if abs(actual - expected) > tol:
        raise AssertionError(
            "%s\n  expected: %r (+/- %g)\n  actual:   %r"
            % (msg or "values differ", expected, tol, actual)
        )


def raises(exc, fn, contains=None):
    bump()
    try:
        fn()
    except exc as e:
        if contains is not None and contains not in str(e):
            raise AssertionError(
                "raised %s but message %r does not contain %r" % (exc.__name__, str(e), contains)
            )
        return e
    raise AssertionError("expected %s to be raised" % exc.__name__)


def collect(module):
    return [
        (name, getattr(module, name))
        for name in sorted(dir(module))
        if name.startswith("test_") and callable(getattr(module, name))
    ]


def run(modules, verbose=False):
    total = 0
    failures = []
    started = time.time()
    for mod in modules:
        cases = collect(mod)
        label = mod.__name__.split(".")[-1]
        line = []
        for name, fn in cases:
            total += 1
            try:
                fn()
                line.append(".")
            except Exception:
                line.append("F")
                failures.append((label, name, traceback.format_exc()))
        print("%-28s %s" % (label, "".join(line)))
        if verbose:
            for name, _ in cases:
                print("    " + name)
    elapsed = time.time() - started

    print("")
    for label, name, tb in failures:
        print("FAIL %s.%s" % (label, name))
        print(tb)
    print(
        "%d tests, %d assertions, %d failed in %.2fs"
        % (total, assert_count(), len(failures), elapsed)
    )
    return 0 if not failures else 1
