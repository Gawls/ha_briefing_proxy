ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.20
FROM ${BUILD_FROM}

# Packages
RUN apk add --no-cache python3 py3-pip

WORKDIR /app

# Python deps
COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt

# App + run script
COPY app.py /app/app.py
COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
