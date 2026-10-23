FROM python:3.13-alpine3.22
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN apk add --no-cache tzdata
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ .
CMD ["python", "app.py"]
