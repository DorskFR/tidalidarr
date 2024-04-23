ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim AS build
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM python:${PYTHON_VERSION}-slim AS runner
ARG PYTHON_VERSION=3.12
ARG VERSION=latest
ARG PROJECT_NAME=tidalidarr
ENV IMAGE_VERSION=${VERSION}
WORKDIR /usr/src/app
COPY --from=build /usr/local/lib/python${PYTHON_VERSION}/site-packages/ /usr/local/lib/python${PYTHON_VERSION}/site-packages/
COPY $PROJECT_NAME $PROJECT_NAME
RUN echo $VERSION > ./VERSION && \
    echo '#!/bin/sh\nexec python -m "'$PROJECT_NAME'"' > entrypoint.sh && \
    chmod +x entrypoint.sh && \
    addgroup --gid 1000 $PROJECT_NAME && \
    useradd --gid 1000 -M --uid 1000 $PROJECT_NAME
USER $PROJECT_NAME
ENTRYPOINT ["./entrypoint.sh"]
