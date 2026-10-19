FROM python:3.12-slim

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY check_for_ebay_deals.py config.py acquire.py parse.py selection.py notify.py deliver.py state.py clock.py /app/

RUN useradd --shell /bin/sh app \
    && install -d -m 1777 /data/logs \
    && chown -R app:app /data /app

USER app
WORKDIR /data
VOLUME ["/data"]

CMD ["python", "/app/check_for_ebay_deals.py"]
