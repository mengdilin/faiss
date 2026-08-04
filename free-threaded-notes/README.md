# Free-threaded Python support for faiss — investigation notes

Working notes for adding free-threaded (PEP 703 / no-GIL) Python support to the
faiss Python extension. Captures the investigation done so far so we can pick it
up in a GitHub Codespace.

- Reference bible: <https://py-free-threading.github.io/>
- Upstream faiss feature request: <https://github.com/facebookresearch/faiss/issues/5418>
- PR/issue reference lists: [swig-port-prs.md](./swig-port-prs.md) · [numpy-port-prs.md](./numpy-port-prs.md)

## Terminology
- **FT = free-threaded** (a.k.a. no-GIL / PEP 703): a build of CPython with the GIL
  disabled, so Python bytecode can run on multiple threads in parallel.
- **GIL build / normal build**: standard CPython where the Global Interpreter Lock
  serializes Python bytecode (one thread at a time).
- **`cp3XX`**: wheel/ABI tag for the normal build (e.g. `cp313` = CPython 3.13, GIL).
- **`cp3XXt`**: wheel/ABI tag for the free-threaded build — the **`t`** suffix
  (e.g. `cp313t` = CPython 3.13 free-threaded). Separate, incompatible ABI.
- **abi3 (stable ABI)**: one wheel (tagged `cp310-abi3`) that works across many
  Python versions (3.10+). **Not available for free-threaded builds yet**, so FT
  wheels must be per-version `cp3XXt`.

---

## 1) Setup requirement — reserve a **Linux** machine (Codespace)

We should reserve a **Linux** environment (GitHub Codespaces are Linux, x86_64 by
default — good). Reasons:

- The py-free-threading workflow depends on a **prebuilt ThreadSanitizer (TSan)
  CPython**, distributed as **Linux Docker images** only:
  `ghcr.io/nascheme/numpy-tsan:3.14t-dev` (repo: <https://github.com/nascheme/cpython_sanity>).
- **Native TSan on Apple-Silicon macOS is broken** (NumPy hit this: <https://github.com/numpy/numpy/issues/27136>). So TSan work cannot be done natively on our MacBooks.
- Codespaces are x86_64 Linux → the `amd64` TSan images run **natively** (no slow
  emulation), and Codespaces support Docker-in-Docker.

What we need in the environment:
- A free-threaded CPython (`python3.13t` / `python3.14t`). Install via `uv python install 3.13t`, pyenv, or the prebuilt image.
- faiss rebuilt with the SWIG no-gil opt-in: **`-DSWIGPYTHON_NOGIL`** (so the
  module declares `Py_MOD_GIL_NOT_USED`; otherwise importing faiss re-enables the
  GIL process-wide).
- For TSan runs: the TSan CPython image **and** faiss rebuilt with
  `-fsanitize=thread` (both must be instrumented — see §Testing).

macOS note for local iteration: we can still build/run faiss on macOS arm64
(Apple Accelerate BLAS, Homebrew libomp) for functional/baseline testing, but the
TSan race-detection must happen in the Linux Codespace/container.

---

## 2) Guiding principle — follow py-free-threading.github.io

Adopt the same playbook NumPy/SWIG used (see reference lists). Core tenets:

1. **Declare the module free-threading-capable** — `Py_mod_gil = Py_MOD_GIL_NOT_USED`.
   For faiss this is literally adding `-DSWIGPYTHON_NOGIL` to the swigfaiss compile
   definitions (SWIG 4.4.1 already emits the machinery, gated on that macro).
2. **Add multithreaded tests from the START to establish a baseline.** Write tests
   that pass on today's GIL Python (where faiss already runs the C++ with the GIL
   released → genuine concurrency), then re-run under a free-threaded interpreter
   and under TSan to catch regressions/races. Baseline-first.
3. **ThreadSanitizer** to catch the data races that assertions miss
   (<https://py-free-threading.github.io/thread_sanitizer/>). Requires a TSan
   CPython + faiss built with `-fsanitize=thread`.
4. **pytest-run-parallel** to run the *existing* suite from many threads and mark
   thread-unsafe tests (<https://py-free-threading.github.io/testing/>).
5. **Dual wheel rollout** — ship both `cp3XX` (GIL) and `cp3XXt` (free-threaded)
   wheels. faiss's `pyproject.toml` currently *skips* free-threaded
   (`skip = "... cp3*t-*"`) and gates abi3 off for FT (`if.abi-flags = "^$"`); the
   FT wheel must be a version-specific `cp3XXt` build (no stable ABI for FT yet).

Useful pages:
- Installing FT CPython: <https://py-free-threading.github.io/installing-cpython/>
- Porting extensions: <https://py-free-threading.github.io/porting-extensions/>
- CI (dual wheels): <https://py-free-threading.github.io/ci/>
- Official C-ext HOWTO: <https://docs.python.org/3/howto/free-threading-extensions.html>

---

## 3) Dependency verification — are any a hard blocker? (No, but note OMP)

### Python-side deps — all already ship free-threaded builds ✅
- **numpy**: free-threaded (`cp313t`) wheels since numpy 2.1; support is real and
  improving (their port is the reference — see [numpy-port-prs.md](./numpy-port-prs.md)).
  **Not a blocker.**
- **swig**: free-threading support landed in **SWIG 4.4** (issue
  <https://github.com/swig/swig/issues/3121> closed via
  <https://github.com/swig/swig/pull/3137>). We already build with **swig 4.4.1**,
  and confirmed the generated `swigfaissPYTHON_wrap.cxx` emits both
  `Py_mod_gil = Py_MOD_GIL_NOT_USED` and `PyUnstable_Module_SetGIL(...)`, gated
  behind `#ifdef SWIGPYTHON_NOGIL`. **Not a blocker** — just needs the compile flag.
- setuptools / wheel / pytest / scipy: standard, FT-ready. Not blockers.

### C++-side: OpenMP (OMP) risk assessment
Intuition (confirmed): **OMP is not a hard blocker.** Rationale:
- OMP threads are **OS threads managed by libomp**, entirely independent of the
  GIL and of Python threads.
- faiss **already releases the GIL** before entering any OMP region (the global
  `%exception { Py_BEGIN_ALLOW_THREADS ... }` in `swigfaiss.swig`), so OMP already
  runs GIL-free today — free-threading changes nothing about the OMP mechanics.
- OMP usage is **internal to a single C++ call** (fork-join over the query batch,
  or over inverted lists), with **per-thread private result buffers** → lock-free
  within a call. faiss does **not** maintain multi-threaded state across Python
  call boundaries. Confirmed: no cross-call thread/state management.

Caveats to track (not blockers, but real):
- **Global mutable stats accumulators** `indexIVF_stats` (`IndexIVF.h`) and
  `hnsw_stats` (`HNSW.h`) are process-global and written during search. Because the
  GIL is released, these already race today under concurrent search (usually benign
  — counters). faiss offers a per-call `IndexIVFStats*` argument to avoid the global.
  This is the closest analog to NumPy's "global caches" work.
- **Oversubscription**: each Python thread that calls search spawns its own OMP
  team from the one shared libomp pool. Thread-per-request needs
  `faiss.omp_set_num_threads(1)` set **inside each worker** (the nthreads-var ICV is
  per-thread and NOT inherited by fresh threads).
- **BLAS thread-safety**: faiss's Linux wheels use OpenBLAS. Concurrent GEMM needs
  **OpenBLAS ≥ 0.3.33.112** (NumPy's `np.dot` race:
  <https://github.com/numpy/numpy/issues/31618>). Our macOS build uses Apple
  Accelerate (separate, unverified). Not our code to fix — a dependency version pin.

### Binding-layer audit (hand-written C++/Python) — largely FT-safe already
- Callbacks (`python_callbacks.cpp`): every `PyObject`/refcount op is under
  `PyThreadLock` (RAII `PyGILState_Ensure`/`Release`). ✅
- Interrupt callback (`PythonInterruptCallback`): acquires GIL before
  `PyErr_CheckSignals`. ✅
- `InterruptCallback::instance`: `unique_ptr` set once at import (GIL held),
  read-only afterward. ✅
- SWIG runtime glue is already FT-safe in 4.4.1 (verified `SWIG_PyDict_GetItemStringRef`,
  `SWIG_PyList_GetItemRef`, `PyImport_AddModuleRef` in the generated wrapper).
- One latent hazard: `class_wrappers.py` `_sp_field_refs` / `add_to_referenced_objects`
  do a **non-atomic read-modify-write** for lifetime pinning — safe under the GIL,
  a potential race only if the *same* object is mutated from multiple threads under
  FT. This is exactly what our baseline test targets (§4).

---

## 4) Testing — baseline test + existing coverage

### New test we wrote
- **`tests/test_free_threaded.py`** — stresses the trickiest FFI seam:
  `SearchParameters.sel` ownership in `class_wrappers.py`
  (`RememberSwigOwnership` + `_sp_field_refs`). 8 barrier-synchronized threads ×
  50 iterations; per iteration it assigns `params.sel`, asserts `thisown` was
  restored and the ref was pinned, drops the local + `gc.collect()`, then runs a
  filtered search and asserts the filter is honored (an odd id ⇒ dangling
  `IDSelector*`). **Passes on GIL CPython 3.12 today** (baseline); becomes a real
  race probe under a free-threaded interpreter.
- Run: `python -m pytest tests/test_free_threaded.py -v`

### Existing multithreaded coverage in Python (audit result)
Essentially **no real concurrent-shared-index coverage** exists today:
- **`tests/test_io.py:207`** — `ThreadPool(1)` (single worker) round-trips an index
  through an OS pipe using `PyCallbackIOReader` / `PyCallbackIOWriter`. This is the
  one genuine cross-thread test; it exercises the IO callbacks in
  `python_callbacks.cpp` (GIL reacquire), but with 1 worker — it's IO plumbing, not
  a concurrency/race test.
- **`tests/test_rabitq_fastscan.py:245` `test_thread_safety`** — **mislabeled**.
  Spawns **no** Python threads; it's a single-threaded batched `search()` relying on
  faiss's internal OMP over the query batch. A correctness smoke test, not a
  thread-safety test.
- **`tests/test_index_binary.py:390`** — only a *comment* about
  `IndexReplicas(threaded=False)`; no Python threads.

C++-side threaded tests (for context, not Python FFI):
`tests/test_threaded_index.cpp` (ThreadedIndex with mocks),
`tests/test_lowlevel_ivf.cpp` (std::thread over *disjoint* inverted lists),
`tests/test_omp_threads.cpp` (`check_openmp()`), several `#pragma omp` bench/tests.
None test concurrent access to a shared index.

### Gap to fill (next steps)
- Concurrent search on a shared index (correctness vs single-threaded reference).
- Concurrent add to separate indices.
- `IDSelectorArray` numpy-buffer lifetime pin (`referenced_objects`, the raw-pointer
  variant — different from the `IDSelectorBatch` object-lifetime case we covered).
- Interop: hammer the downcast typemap + `SWIG_Python_TypeQuery` cache concurrently.
- Run all of the above under `python3.13t/3.14t` + TSan, plus `pytest-run-parallel`
  over the existing suite.

### TSan recipe (Linux/Codespace only)
```bash
# in the TSan CPython container
docker run -it -v "$PWD":/work -w /work ghcr.io/nascheme/numpy-tsan:3.14t-dev bash
# rebuild faiss instrumented (BOTH libfaiss and the swig .so):
cmake -B build . -DFAISS_ENABLE_GPU=OFF -DFAISS_ENABLE_METAL=OFF \
  -DFAISS_OPT_LEVEL=generic -DCMAKE_BUILD_TYPE=RelWithDebInfo \
  -DCMAKE_CXX_FLAGS="-fsanitize=thread -g" \
  -DCMAKE_SHARED_LINKER_FLAGS="-fsanitize=thread" \
  -DSWIGPYTHON_NOGIL=ON
make -C build -j faiss swigfaiss && (cd build/faiss/python && python setup.py install)
TSAN_OPTIONS="suppressions=$PWD/tsan-suppressions.txt halt_on_error=1" \
  python -m pytest tests/test_free_threaded.py -v
```
Note: expect OMP/BLAS TSan noise → needs a suppressions file (start from CPython's
`Tools/tsan/suppressions_free_threading.txt` + NumPy's).

---

## 5) & 6) Port reference PRs
- SWIG free-threaded port: **[swig-port-prs.md](./swig-port-prs.md)**
- NumPy free-threaded port: **[numpy-port-prs.md](./numpy-port-prs.md)**
