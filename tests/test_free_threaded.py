# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""
FFI-boundary stress test for free-threaded Python compatibility.

Target: the trickiest piece of the C++ <-> Python binding layer --
``faiss/python/class_wrappers.py::handle_SearchParameters.replacement_setattr``
(the ``RememberSwigOwnership`` + ``_sp_field_refs`` ownership/lifetime dance).

Why this is the trickiest seam:
When you do ``params.sel = selector``, SWIG's generated setter runs with
``SWIG_POINTER_DISOWN``, flipping ``selector.thisown`` -> False (SWIG assumes
the container now owns the sub-object). But C++ ``SearchParameters`` holds
``IDSelector* sel`` as a NON-owning raw pointer and never deletes it. So the
hand-written wrapper must do two things that SWIG does not:
  (1) RememberSwigOwnership: restore ``selector.thisown`` -> True, and
  (2) pin a Python reference: ``params._sp_field_refs['sel'] = selector``
so the selector's C++ object stays alive while ``params->sel`` points at it.

If the pin is lost (e.g. the non-atomic read-modify-write on ``_sp_field_refs``
races under a free-threaded interpreter, or a regression drops it), the C++
``SearchParameters`` holds a DANGLING ``IDSelector*`` -> use-after-free during
search -> a crash or a filter violation (ids outside the allowed set leak
through).

This test drops every local reference to the selector, forces GC, then runs
filtered searches concurrently from many threads. It PASSES on today's
GIL-enabled CPython (the C++ search runs with the GIL released, so this is a
real concurrency exercise of the FFI) and serves as a canary under a
free-threaded build.
"""

import gc
import threading
import unittest

import numpy as np

import faiss


class TestSearchParametersSelOwnership(unittest.TestCase):
    def setUp(self):
        d, nb, nlist = 32, 20000, 64
        rng = np.random.RandomState(1234)
        xb = rng.rand(nb, d).astype("float32")
        index = faiss.index_factory(d, "IVF%d,Flat" % nlist)
        index.train(xb)
        index.add(xb)
        index.nprobe = 8

        self.index = index
        self.d = d
        # allowed subset = even ids only; an odd id in a result means the
        # selector's C++ object was corrupted / freed (dangling pointer).
        self.allowed = np.arange(0, nb, 2, dtype="int64")
        self.allowed_set = set(self.allowed.tolist())

    def test_sel_survives_gc_under_threads(self):
        n_workers = 8
        iters = 50
        k = 10
        barrier = threading.Barrier(n_workers)
        errors = []

        def worker(wid):
            rng = np.random.RandomState(wid)
            try:
                barrier.wait()  # release all workers together to maximize overlap
                for _ in range(iters):
                    # params.sel = ... triggers RememberSwigOwnership + the
                    # _sp_field_refs pin. After `del sel`, the only strong ref
                    # to the selector is params._sp_field_refs['sel'].
                    params = faiss.SearchParametersIVF()
                    params.nprobe = 8
                    sel = faiss.IDSelectorBatch(self.allowed)
                    params.sel = sel
                    self.assertTrue(sel.thisown)                      # ownership restored
                    self.assertIs(params._sp_field_refs["sel"], sel)  # ref pinned
                    del sel

                    gc.collect()  # frees the selector iff the pin failed -> UAF below

                    q = rng.rand(3, self.d).astype("float32")
                    _, I = self.index.search(q, k, params=params)
                    for i in I.reshape(-1):
                        if i != -1:
                            self.assertIn(
                                int(i),
                                self.allowed_set,
                                "filter violated (dangling IDSelector*?)",
                            )
            except BaseException as e:  # noqa: B036 - propagate to main thread
                errors.append(e)

        threads = [
            threading.Thread(target=worker, args=(w,)) for w in range(n_workers)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        if errors:
            raise errors[0]


if __name__ == "__main__":
    unittest.main()
