FROM python:3.12-slim

# Hugging Face runs Docker Spaces as uid 1000
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    CARPI_PUBLIC=1 \
    CARPI_LOGS=/home/user/app/drives \
    CARPI_CACHE=/tmp/carpi-cache \
    CARPI_ABOUT="Logs from Yury's 2017 Audi A3 (EA888 Gen 3, DQ250), standard OBD-II at about 7.7 samples/s from a Raspberry Pi on the CAN bus. Every drive so far is the baseline, before the PCV valve replacement."
WORKDIR $HOME/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

COPY --chown=user . .

EXPOSE 7860
# One worker so every request shares the in-memory drive cache; threads handle concurrency.
CMD ["gunicorn", "-w", "1", "--threads", "8", "--timeout", "120", "-b", "0.0.0.0:7860", "carpi_app.wsgi:server"]
