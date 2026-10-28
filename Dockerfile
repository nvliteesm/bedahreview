# Exact version on purpose: a deploy must not pick up a new Python or Debian by surprise. To update, change the tag, run the tests, then deploy.
FROM python:3.12.16-slim-trixie

ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY prompts ./prompts
COPY src ./src

# Cloud Run sets PORT, which switches the app to public mode (needs BEDAHREVIEW_ACCESS_CODE).
# Keys come from the environment (Secret Manager), never from the image.
RUN useradd --create-home app && chown -R app /app
USER app
CMD ["python", "-m", "bedahreview.web"]
