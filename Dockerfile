# Duplicate the github actions pipeline tests (the ptrace/seccomp flags
# are REQUIRED — without them lldb-dap cannot launch a debuggee and the
# native lldb-dap tests fail with "DAP configurationDone failed"; see
# .github/workflows/test.yml):
#
# docker build --target base -t tdb-base .
# docker run --rm --cap-add=SYS_PTRACE --security-opt seccomp=unconfined \
#   tdb-base uv run pytest
#
# Narrow it to one file the same way, e.g.:
# docker run --rm --cap-add=SYS_PTRACE --security-opt seccomp=unconfined \
#   tdb-base uv run pytest tests/integration/test_tcsh_adapter.py -x -vv \
#   -p no:cacheprovider --no-cov


# Run tdb from a container:
#
# docker buildx build -t tdb .
# docker buildx build --target test -t tdb-test .
# docker run -i --rm tdb [args]


FROM ghcr.io/astral-sh/uv:python3.14-alpine AS base
# perl/bash/tcsh power their DAP adapters; ruby + the debug gem power
# the rdbg proxy (the gem has a C extension, hence the build deps).
# gdb (>= 14, DAP mode) and rust/rustc let CI run the C/C++ and Rust
# integration suites for real (Alpine's gdb package bundles gdbserver
# as of 16.3-r2, so the gdb remote-stub attach tests run here even
# though most hosts skip them; layout-specific Rust concurrency
# evidence skips unless Alpine's rustc matches the supported stable
# series — see rust_adapter_harness.require_supported_rust_concurrency).
RUN apk add --no-cache perl bash tcsh ruby ruby-dev make gcc gdb rust musl-dev \
 && gem install debug --no-document
# OCaml: native debugging goes through lldb-dap (this image's `lldb` package
# also covers C/C++). `ocaml5` (5.4.0) is Alpine's >=5.0 package -- the
# plain `ocaml` apk package is 4.14 and has no Domain/multicore support,
# which the OCaml profile's thread-as-domain feature depends on.
# `ocaml5-compiler-libs` is required separately (opam's `ocaml-system`
# switch, and several of ocamlearlybird's own dependencies, need
# compiler-libs/toplevel .cmi/.cmx files that aren't in the base `ocaml5`
# package). `opam` builds ocamlearlybird (bytecode debugging) from source
# below; `m4` is a transitive build dependency of some of its opam packages.
# `py3-lldb` supplies lldb's Python bindings for its embedded script
# interpreter — without it `command script import` fails inside Alpine's
# lldb (`ModuleNotFoundError: No module named 'lldb'`) and the Rust
# concurrency probe can never register.
RUN apk add --no-cache ocaml5 ocaml5-compiler-libs opam lldb py3-lldb m4 \
 && rm -rf /var/cache/apk/*
RUN adduser -D appuser
ENV PATH="/app/.venv/bin:$PATH"

COPY --chown=appuser:appuser . /app
WORKDIR /app
USER appuser

# opam must be initialized as a non-root user. `--packages=ocaml-system`
# reuses the apk-installed 5.4.0 compiler above instead of building one
# from source (much faster, and matches the exact toolchain combination
# the OCaml support feature was probe-verified against -- see
# docs/superpowers/specs/2026-08-22-ocaml-support-design.md's
# "Probe-verified facts" section). If a future Alpine release drops the
# `ocaml5` package below 5.0, replace `--packages=ocaml-system` with an
# explicit `opam switch create 5.2.0` (compiles a compiler from source,
# much slower) per that spec.
RUN opam init --disable-sandboxing -y --bare \
 && opam switch create default --packages=ocaml-system \
 && eval $(opam env) \
 && opam install -y earlybird
ENV PATH="/home/appuser/.opam/default/bin:$PATH"

RUN ["uv", "sync", "--all-extras"]
RUN ["uv", "pip", "install", "-e", "."]

# - - - - - test target
# NOTE (OCaml native / lldb-dap): this target's build-time `RUN` below
# fails on the 5 native OCaml lldb-dap tests (and the cpp lldb-dap pause
# test) if you build it directly with plain `docker build --target test .`
# -- BuildKit's default RUN sandbox has no CAP_SYS_PTRACE and denies the
# personality()/ptrace syscalls lldb-dap needs to launch a native
# debuggee, with no opt-in short of BuildKit's "insecure" entitlement
# (not used here). This is why CI (.github/workflows/test.yml) does NOT
# use this target: it builds only through `base`, then runs pytest via a
# separate `docker run --cap-add=SYS_PTRACE --security-opt
# seccomp=unconfined ... uv run pytest` step instead, which grants those
# syscalls (verified: 7 passed, 1 xfailed for `-k ocaml` alone; full
# suite passes with these tests included). This target is kept for local
# convenience only (`docker build --target test .`); it will still hit
# the same native-lldb-dap failures unless you build+run it the same way
# CI does, e.g. `docker build --target base -t tdb-test . && docker run
# --rm --cap-add=SYS_PTRACE --security-opt seccomp=unconfined tdb-test
# uv run pytest`.
FROM base AS test
RUN ["uv", "run", "pytest"]
# - - - - -

FROM base AS final
ENTRYPOINT ["tdb"]
CMD ["--help"]
