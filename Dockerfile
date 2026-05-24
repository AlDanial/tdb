# docker build -t tdb .
# docker run -it --rm tdb [args]

FROM ghcr.io/astral-sh/uv:python3.14-alpine
RUN adduser -D appuser
ENV PATH="/app/.venv/bin:$PATH"

COPY --chown=appuser:appuser . /app
WORKDIR /app
USER appuser

RUN ["uv", "sync", "--all-extras"]
RUN ["uv", "pip", "install", "-e", "."]

ENTRYPOINT ["tdb"]

CMD ["--help"]
