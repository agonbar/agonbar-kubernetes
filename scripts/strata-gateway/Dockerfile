# strata-gateway: the priority queue in front of Strata (see gateway.py). Built by build.sh.
FROM docker.io/library/python:3.13-slim
RUN pip install --no-cache-dir aiohttp==3.14.3
COPY gateway.py /app/gateway.py
USER 65534
ENV PYTHONUNBUFFERED=1
EXPOSE 8080
ENTRYPOINT ["python", "/app/gateway.py"]
