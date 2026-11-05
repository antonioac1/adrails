# AdRails MCP server (stdio). Starts with zero setup: with no config mounted it scaffolds an OFFLINE demo
# (no credentials, commits off) so directory "does it start" checks and `tools/list` work.
# Real use: mount your config dir and point ADSOPS_CONFIG at it, e.g.
#   docker run -i --rm -v ~/adrails/acme:/data/acme -e ADSOPS_CONFIG=/data/acme/config.json ghcr.io/antonioac1/adrails
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/* \
 && git config --system user.name "adrails" && git config --system user.email "adrails@localhost" \
 && useradd -m -u 10001 adrails
COPY . /src
RUN pip install --no-cache-dir /src && rm -rf /src
COPY docker/entrypoint.sh /usr/local/bin/adrails-entrypoint
RUN chmod 0755 /usr/local/bin/adrails-entrypoint
USER adrails
WORKDIR /home/adrails
ENTRYPOINT ["adrails-entrypoint"]
