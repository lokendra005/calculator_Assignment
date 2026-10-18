# Distroless: a Python runtime and nothing else. No shell, no package manager,
# runs as uid 65532. The app is stdlib-only, so there is no build stage.
FROM gcr.io/distroless/python3-debian13:nonroot

ARG VERSION=dev
LABEL org.opencontainers.image.source="https://github.com/lokendra005/calculator_Assignment" \
      org.opencontainers.image.revision="${VERSION}"
ENV APP_VERSION="${VERSION}" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /app
COPY server.py .
USER 65532:65532
EXPOSE 8080
CMD ["server.py"]
