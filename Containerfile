# The uni-mail image: uni-maild, the uni-mail CLI with the dispatcher, and
# uni-mail-mcp. Standard library only, plus the MCP SDK for uni-mail-mcp.
#
#   podman build -t localhost/uni-mail:latest .
#
# Run it with the Quadlet units of deploy/podman/ (docs/CONTAINER.md).

FROM docker.io/library/python:3.14-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY uni_mail ./uni_mail
RUN python -m venv /opt/uni-mail \
 && /opt/uni-mail/bin/pip install --no-cache-dir --disable-pip-version-check ".[mcp]"

FROM docker.io/library/python:3.14-slim
COPY --from=build /opt/uni-mail /opt/uni-mail
# One unprivileged user for every process. The directories are the mount
# points of the volumes: a new named volume takes their owner and mode.
RUN groupadd --gid 10001 uni-mail \
 && useradd --uid 10001 --gid 10001 --home-dir /var/lib/uni-mail --no-create-home \
            --shell /usr/sbin/nologin uni-mail \
 && install -d -m 0700 -o 10001 -g 10001 /var/lib/uni-mail /var/lib/uni-mail-dispatch /run/uni-mail \
 && install -d -m 0755 /etc/uni-mail
ENV PATH=/opt/uni-mail/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UNI_MAIL_CONFIG=/etc/uni-mail/config.json
USER 10001:10001
WORKDIR /var/lib/uni-mail
LABEL org.opencontainers.image.title="uni-mail" \
      org.opencontainers.image.source="https://github.com/JuanDelPueblo/uni-mail" \
      org.opencontainers.image.licenses="MIT"
CMD ["uni-maild"]
