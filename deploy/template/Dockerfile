# CarPi Analyzer, hosted copy. Works on Render (PORT is set to 10000) and Hugging Face (7860).
FROM python:3.12-slim

RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    MALLOC_ARENA_MAX=2 \
    CARPI_PUBLIC=1 \
    CARPI_LOGS=/home/user/app/drives \
    CARPI_CACHE=/home/user/app/.cache \
    CARPI_ABOUT="Logs from Yury's 2017 Audi A3 (EA888 Gen 3, DQ250), standard OBD-II at about 7.7 samples/s from a Raspberry Pi on the CAN bus. Every drive so far is the baseline, before the PCV valve replacement."
WORKDIR $HOME/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

COPY --chown=user . .
# Parse every CSV once at build time (fast build machine) so the small runtime CPU never has to.
RUN python -m carpi_app.warm

EXPOSE 7860
# One worker so all requests share the in-memory drive cache; a few threads for concurrency.
CMD gunicorn -w 1 --threads 4 --timeout 120 -b 0.0.0.0:${PORT:-7860} carpi_app.wsgi:server
