FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core fonts-noto-core fonts-noto-cjk \
       build-essential libfreetype6-dev libjpeg62-turbo-dev zlib1g-dev libraqm-dev \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 app
WORKDIR /home/app/studio

COPY --chown=app requirements.txt .
RUN pip install --no-cache-dir --no-binary=Pillow "Pillow>=10.4.0"
RUN pip install --no-cache-dir -r requirements.txt
RUN python -c "from PIL import features; assert features.check_feature('raqm'), 'Pillow must include libraqm for shaped captions'"

COPY --chown=app . .
USER app

ENV PORT=8501 TUBEFORGE_PUBLIC_MODE=true
EXPOSE 8501
CMD streamlit run app.py --server.address=0.0.0.0 --server.port=${PORT} --server.headless=true
