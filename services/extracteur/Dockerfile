FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10

WORKDIR /service

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Dépendances ajoutées après coup : couche séparée (l'installation de base
# reste en cache) et cache pip persistant entre les constructions.
COPY requirements-extraction.txt .
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements-extraction.txt

COPY app ./app

# Utilisateur non privilégié
RUN useradd --create-home --uid 1000 extracteur
USER extracteur

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
