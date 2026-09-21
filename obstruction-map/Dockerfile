FROM python:3.12-alpine
WORKDIR /app
COPY server.py /app/server.py
ENV OUT_DIR=/out
EXPOSE 9818
CMD ["python", "-u", "/app/server.py"]
