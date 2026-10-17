# One image for every CASCADE app service (edge, scorer, signals, genai, api); CMD differs per service.
# Multi-arch: ubuntu:22.04 is published for s390x, x86_64 and arm64.
FROM docker.io/library/ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv python3-pip python3-numpy python3-scipy \
      python3-sklearn python3-cryptography python3-jinja2 python3-reportlab ca-certificates openssl curl \
 && rm -rf /var/lib/apt/lists/*
# XGBoost is optional: if Debian has no python3-xgboost for this architecture, CASCADE_ML=auto uses
# scikit-learn's HistGradientBoostingClassifier instead (logged in docs/DECISIONS.md when it happens).
RUN (apt-get update && apt-get install -y --no-install-recommends python3-xgboost && rm -rf /var/lib/apt/lists/*) \
 || echo "python3-xgboost not available on $(uname -m): HGB fallback will be used"

RUN python3 -m venv --system-site-packages /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY deploy/requirements-container.txt /tmp/requirements.txt
RUN pip install --prefer-binary -r /tmp/requirements.txt && rm /tmp/requirements.txt

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin cascade
WORKDIR /app
COPY common ./common
COPY datagen ./datagen
COPY edge ./edge
COPY loop1 ./loop1
COPY loop2 ./loop2
COPY genai ./genai
COPY api ./api
COPY eval ./eval
COPY data/drug_catalogue.csv data/cascades_known.csv data/pvpi_form_fields.json data/reactions.csv data/treatments.csv data/indications.csv ./data/
COPY pyproject.toml ./
RUN mkdir -p /app/data/generated /app/loop1/model /app/docs /state && chown cascade /state \
 && python -c "import numpy, scipy, sklearn, cryptography, fastapi, pydantic, redis, pg8000; print('deps ok')"

USER 10001
ENV CASCADE_STATE_DIR=/state
CMD ["python", "-m", "api.main"]
