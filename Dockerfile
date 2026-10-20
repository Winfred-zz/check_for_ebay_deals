FROM python:3.12-slim

COPY requirements.txt constraints.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY check_for_ebay_deals.py config.py acquire.py parse.py selection.py notify.py deliver.py state.py clock.py /app/

# /app stays root-owned so the running user cannot edit the code it
# executes; /data is world-writable (1777) so the state root —
# state.sqlite, bot.lock, and logs/ — is writable by the app user,
# including inside a named volume copied up from this layer.
RUN useradd --shell /bin/sh app \
    && install -d -m 1777 /data \
    && install -d -m 1777 /data/logs

USER app
WORKDIR /data
VOLUME ["/data"]

CMD ["python", "/app/check_for_ebay_deals.py", "--data-dir", "/data"]
