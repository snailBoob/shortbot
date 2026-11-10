FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.title="Shortbot" \
      org.opencontainers.image.source="https://github.com/snailBoob/shortbot" \
      org.opencontainers.image.version=$VERSION \
      org.opencontainers.image.revision=$REVISION
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 STATE_PATH=/data/shortbot.sqlite3
WORKDIR /app
RUN useradd --uid 10001 --create-home shortbot && mkdir /data && chown shortbot:shortbot /data
COPY --chown=shortbot:shortbot shortbot.py exchanges.py /app/
USER shortbot
CMD ["python", "shortbot.py"]
