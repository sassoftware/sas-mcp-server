FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS base-builder

WORKDIR /app
COPY . .
RUN uv build

FROM python:3.12-slim-bookworm AS runner
ARG HOST_PORT=8134

LABEL maintainer="david.weik@sas.com"
LABEL org.opencontainers.image.source=https://github.com/sassoftware/sas-mcp-server
LABEL org.opencontainers.image.description="SAS MCP Server — Model Context Protocol server for SAS Viya"
LABEL org.opencontainers.image.licenses=Apache-2.0

RUN addgroup --system sas && adduser --system --ingroup sas --home /app sas

COPY --from=base-builder /app/dist/ /install

WORKDIR /app
RUN python3 -m venv .venv \
    && /app/.venv/bin/pip install --no-cache-dir /install/*.whl \
    && rm -r /install

ENV PATH="/app/.venv/bin:$PATH"

USER sas

EXPOSE ${HOST_PORT}
CMD ["app"]