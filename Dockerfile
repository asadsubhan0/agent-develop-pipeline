FROM registry.access.redhat.com/ubi9/python-311:latest

USER 0
RUN microdnf install -y git && microdnf clean all

WORKDIR /opt/app-root/src
COPY requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY . .
RUN chmod -R g=u /opt/app-root/src

ENV PORT=8080 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
EXPOSE 8080

USER 1001
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]