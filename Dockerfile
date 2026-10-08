FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt camoufox[geoip] && python -m camoufox fetch
RUN python -m playwright install chromium --with-deps 2>/dev/null || python -m playwright install chromium
COPY server.py dashboard.html gmaps.py .
EXPOSE 8777
CMD ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8777"]
