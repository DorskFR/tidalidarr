ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-alpine AS build
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM python:${PYTHON_VERSION}-alpine AS runner
ARG PYTHON_VERSION=3.12
ARG VERSION=latest
ARG PROJECT_NAME=tidalidarr
ENV IMAGE_VERSION=${VERSION}
WORKDIR /usr/src/app
COPY --from=build /usr/local/lib/python${PYTHON_VERSION}/site-packages/ /usr/local/lib/python${PYTHON_VERSION}/site-packages/
COPY $PROJECT_NAME $PROJECT_NAME
RUN apk upgrade --no-cache && \
    echo $VERSION > ./VERSION && \
    printf '#!/bin/sh\nexec python -m "%s"\n' "$PROJECT_NAME" > entrypoint.sh && \
    chmod +x entrypoint.sh && \
    addgroup -g 1000 $PROJECT_NAME && \
    adduser -D -H -G $PROJECT_NAME -u 1000 $PROJECT_NAME
USER $PROJECT_NAME
ENTRYPOINT ["./entrypoint.sh"]
