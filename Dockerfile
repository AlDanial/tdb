# docker build -t tdb .
# docker build --target test -t tdb-test .
# docker run -it --rm tdb [args]

FROM ghcr.io/astral-sh/uv:python3.14-alpine AS base
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
