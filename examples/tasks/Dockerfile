# The sandbox image for the real-task corpus: Axiom-1's own code runs its own tests in here, so it
# needs git (the tests build throwaway repos) and the MCP library (the runner starts servers).
# Network is available only while this image is BUILT; checks run with --network none.
# The Python packages are installed from wheels downloaded on the host (`wheels/`, not committed):
# HTTPS from inside the build failed certificate checks on the machine this was built on.
FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links /wheels mcp uvicorn && rm -rf /wheels
ENV GIT_CONFIG_NOSYSTEM=1
