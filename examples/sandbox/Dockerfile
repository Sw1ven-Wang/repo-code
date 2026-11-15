# Supply a real pinned Python image; no floating default.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
# Install task dependencies HERE under operator review, not through the Agent.
# For an API demo one may use a separately pinned dependency lock containing
# FastAPI/Pydantic/pytest and add a controlled pip install build layer.
WORKDIR /workspace
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1
USER 65534:65534
