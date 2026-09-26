# FlakeHunter web demo (demo mode: replays a recorded IBM Bob run; detection and
# verification run live). Works on Hugging Face Spaces (Docker SDK) and any Docker host.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FLAKEHUNTER_MODE=demo \
    PORT=7860

# Hugging Face Spaces runs the container as user 1000
RUN useradd -m -u 1000 app
WORKDIR /home/app/flakehunter
COPY --chown=app:app . .

# Editable install so the package finds demo_originals/ and demo_data/ next to it
RUN pip install --no-cache-dir -e .

USER app
EXPOSE 7860
CMD ["sh", "-c", "flakehunter serve --host 0.0.0.0 --port ${PORT}"]
