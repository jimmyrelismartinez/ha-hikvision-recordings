ARG BUILD_FROM
FROM ${BUILD_FROM}

# ffmpeg is used for stream-copy remuxing only (MPEG-PS -> fragmented MP4).
# No transcoding, so no hardware acceleration is needed even on a Pi.
RUN apk add --no-cache ffmpeg

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY app/ /app/hikvision_recordings/app/
RUN touch /app/hikvision_recordings/__init__.py
COPY www/ /www/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    ADDON_WWW_DIR=/www \
    ADDON_OPTIONS_PATH=/data/options.json

CMD ["/run.sh"]
