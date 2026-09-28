"""Import-safety of `ProjectManagementService`'s annotations.

The class defines a staticmethod named `list`, which shadows the builtin
`list` inside the class body. Evaluating a bare `list[int]` annotation in
that namespace raises "'staticmethod' object is not subscriptable" -- and
each Python line evaluates annotations at a different moment:

* Python <= 3.13 evaluates eagerly at `def` time, so any method *after*
  `.list` with a `list[...]` annotation crashes the whole import. That took
  the production backend (Python 3.13) down on 2026-09-28, while the dev
  machine on 3.14 imported the same file and passed every test.
* Python 3.14 (PEP 649) defers evaluation, but resolves against the
  *completed* class namespace -- so accessing `__annotations__` of any
  method with a `list[...]` annotation crashes, even ones before `.list`.

`from __future__ import annotations` in the module keeps every annotation
an inert string on every version. These tests fail if that import is ever
removed, instead of production failing at the next deploy.
"""
import unittest

from app.services import project_management as module
from app.services.project_management import ProjectManagementService


class EagerAnnotationTests(unittest.TestCase):
    def test_module_keeps_deferred_annotations(self):
        import __future__
        feature = getattr(module, "annotations", None)
        self.assertIsInstance(
            feature, __future__._Feature,
            "app/services/project_management.py must keep `from __future__ import "
            "annotations`: the class's own `list` staticmethod shadows the builtin, "
            "and evaluated annotations crash the import on Python <= 3.13.",
        )

    def test_every_method_annotation_is_accessible_without_evaluating(self):
        # On 3.14 a deferred annotation evaluates on first __annotations__
        # access, against the finished class namespace where `list` is the
        # staticmethod. With the __future__ import they are plain strings and
        # this loop cannot raise.
        for name, member in vars(ProjectManagementService).items():
            func = member.__func__ if isinstance(member, staticmethod) else member
            if not callable(func):
                continue
            try:
                annotations = dict(getattr(func, "__annotations__", {}) or {})
            except TypeError as exc:
                self.fail(f"{name}: reading __annotations__ evaluated an annotation "
                          f"in the class namespace: {exc}")
            for target, value in annotations.items():
                self.assertIsInstance(
                    value, str,
                    f"{name}.{target} is an evaluated annotation object; the module's "
                    f"deferred-annotations guard is not in effect",
                )

    def test_the_shadowing_hazard_is_still_real(self):
        # The exact expression that took production down must still fail when
        # evaluated in the class namespace -- if this ever stops raising, the
        # guard above is no longer load-bearing and can be removed.
        with self.assertRaises(TypeError):
            eval("Optional[list[int]]", vars(module), dict(vars(ProjectManagementService)))


if __name__ == "__main__":
    unittest.main()
