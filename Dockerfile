FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py .
COPY templates/ templates/
COPY static/ static/

# Database is stored in a mounted volume at /data
ENV DB_PATH=/data/bills.db

VOLUME ["/data"]

ENTRYPOINT ["python", "main.py"]
CMD ["check"]
