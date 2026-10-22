FROM python:3.12-slim
WORKDIR /opt/platform
COPY vendor/wheels /wheels
COPY requirements.txt .
RUN pip install --no-index --find-links=/wheels -r requirements.txt && rm -rf /wheels
COPY app ./app
COPY tests ./tests
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8420","--ssl-keyfile","/run/secrets/server.key","--ssl-certfile","/run/secrets/server.crt"]
