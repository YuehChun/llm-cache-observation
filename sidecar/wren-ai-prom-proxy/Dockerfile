FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install -r requirements.txt
COPY proxy.py /app/proxy.py
EXPOSE 5556
CMD ["uvicorn", "proxy:app", "--host", "0.0.0.0", "--port", "5556", "--log-level", "warning"]
