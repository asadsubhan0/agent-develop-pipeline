FROM registry.access.redhat.com/ubi9/ubi:latest

USER 0
RUN dnf install -y git python3.11 python3.11-pip && dnf clean all

WORKDIR /opt/app-root/src
COPY requirements.txt ./requirements.txt
RUN python3.11 -m pip install --no-cache-dir -r requirements.txt
COPY . .
RUN chmod -R g=u /opt/app-root/src

ENV PORT=8080 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
EXPOSE 8080

USER 1001
CMD ["python3.11", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]