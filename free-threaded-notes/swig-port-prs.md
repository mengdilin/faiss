# SWIG free-threaded port — issues & PRs

SWIG added free-threading support in the **4.4** series (merged ~April 2025). We
already build faiss with **swig 4.4.1**, which includes all of this. The opt-in is
the compile macro **`-DSWIGPYTHON_NOGIL`** (SWIG does *not* declare modules
GIL-free by default).

## The epic / tracking issue
- [#3121 — Support free-threaded Python](https://github.com/swig/swig/issues/3121) *(closed — resolved by the PRs below; opened by ngoldbaum / Quansight, same team driving NumPy's port)*

## Implementing PRs
- [#3137 — added support for Python free threading (aka no-gil)](https://github.com/swig/swig/pull/3137) — the main implementation (undefs unsafe borrowed-ref macros like `PyList_SET_ITEM`, emits the `Py_mod_gil` machinery)
- [#3215 — Python free-threading without `PYTHON_GIL=0`](https://github.com/swig/swig/pull/3215)
- [#3254 — Do not mark modules no-gil compatible by default (only if `SWIGPYTHON_NOGIL`)](https://github.com/swig/swig/pull/3254) — the opt-in default
- [#3225 — Free-threading Python: revert special treatment for `PyList_GET_ITEM` etc.](https://github.com/swig/swig/pull/3225)
- [#3226 — Revert "Restore plain python-3.12 testing without -nogil"](https://github.com/swig/swig/pull/3226)

## Thread-safety fixes to the generated runtime (the borrowed-ref → strong-ref work)
- [#3230 — Python: `SWIG_AsArgcArgv` thread safety](https://github.com/swig/swig/pull/3230) — strong-ref list access + added the `swig_run_threaded` torture-test harness (copied from NumPy)
- [#3233 — Python: Thread-safe `SWIG_runtime_data_module`](https://github.com/swig/swig/pull/3233) — `PyImport_AddModuleRef`
- [#3235 — Python: free-threaded race condition in `SWIG_TypeQuery`](https://github.com/swig/swig/pull/3235) — strong-ref dict cache (`SWIG_PyDict_GetItemStringRef`), `SWIG_`-prefixed compat shims for abi3audit
- [#3229 — Python: `argcargv.i` is broken and untested](https://github.com/swig/swig/issues/3229) *(issue behind #3230)*

## Related / open items to watch
- [#3212 — Python: `swig -threads` segfaults on single-thread code](https://github.com/swig/swig/issues/3212) *(open)*
- [#3277 — Sharing C++ types between modules is broken between SWIG 4.3 and SWIG 4.4](https://github.com/swig/swig/issues/3277) *(open — relevant: faiss builds multiple SWIG modules — swigfaiss, swigfaiss_avx2, swigfaiss_avx512, swigfaiss_sve — that share faiss C++ types; keep them all on the same SWIG version)*
- [#3503 — [Python] Fix segfault with -threads in exception handlers](https://github.com/swig/swig/pull/3503) *(open)*
- [#3214 — SWIG -threads causes silent exit when using setjmp/longjmp for exception](https://github.com/swig/swig/issues/3214)

## What we verified in faiss's own generated wrapper (swig 4.4.1)
`build/faiss/python/CMakeFiles/swigfaiss.dir/swigfaissPYTHON_wrap.cxx` already contains,
gated on `#ifdef SWIGPYTHON_NOGIL`:
- `{ Py_mod_gil, Py_MOD_GIL_NOT_USED }` module slot
- `PyUnstable_Module_SetGIL(m, Py_MOD_GIL_NOT_USED)`
- the strong-ref runtime fixes: `SWIG_PyDict_GetItemStringRef`, `SWIG_PyList_GetItemRef`, `PyImport_AddModuleRef`

→ Enabling FT on the SWIG side = add `-DSWIGPYTHON_NOGIL` to the compile flags.
**Caveat**: SWIG makes its *own glue* thread-safe and lets us *declare* safety; it
does **not** make faiss's wrapped C++ thread-safe — that's on us.
