# Duplicate the github actions pipeline tests:
# 
# docker build --target base -t tdb-base .
# docker run --rm --cpus=2 --memory=7g --init tdb-base \
#   uv run pytest tests/integration/test_tcsh_adapter.py -x -vv -p no:cacheprovider --no-cov


# Run tdb from a container:
#
# docker buildx build -t tdb .
# docker buildx build --target test -t tdb-test .
# docker run -i --rm tdb [args]


FROM ghcr.io/astral-sh/uv:python3.14-alpine AS base
# perl powers the Perl DAP adapter; without it the perl test suite
# silently skips in CI (and one launch-preflight test used to fail).
RUN apk add --no-cache perl bash tcsh
RUN adduser -D appuser
ENV PATH="/app/.venv/bin:$PATH"

COPY --chown=appuser:appuser . /app
WORKDIR /app
USER appuser

RUN ["uv", "sync", "--all-extras"]
RUN ["uv", "pip", "install", "-e", "."]

# - - - - - test target
FROM base AS test
RUN ["uv", "run", "pytest"]
# - - - - -

FROM base AS final
ENTRYPOINT ["tdb"]
CMD ["--help"]
