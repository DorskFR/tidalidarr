# Tidalidarr

## tl;dr

Minimal standalone music downloader from Tidal syncing with Lidarr for macos, linux.

In its first version this program is opinionated:
- Only albums
- Only top hits
- Only FLAC

## Installation

### Environment variables

The full settings are in the `BaseSettings` models.
The main variables that might require changes are:

| Variable              | Required | Default                      | Description                                         |
| --------------------- | -------- | ---------------------------- | --------------------------------------------------- |
| `TIDAL_DOWNLOAD_PATH` | `No`     | /downloads                   | Persistent storage to save music files              |
| `TIDAL_TOKEN_PATH`    | `No`     | token.json                   | Persistent storage to save the authentication token |
| `LIDARR_API_URL`      | `Yes`    | http://127.0.0.1:8686/api/v1 | Lidarr API endpoint                                 |
| `LIDARR_API_KEY`      | `Yes`    |                              | Lidarr API key                                      |


### Docker (pull)

```bash
docker run --rm --name tidalidarr \
  -e TIDAL_DOWNLOAD_PATH=/downloads \
  -e TIDAL_TOKEN_PATH=/token.json \
  -e LIDARR_API_URL=http://127.0.0.1:8686/api/v1 \
  -e LIDARR_API_KEY=${LIDARR_API_KEY} \
  -v ${PWD}/downloads:/downloads \
  -v ${PWD}/token.json:/usr/src/app/token.json \
  ghcr.io/dorskfr/tidalidarr:latest
```

### Docker compose (pull)

Setup environment variables via `.env` or else and:

```yaml
# compose.yaml
version: "3.8"

services:
  tidalidarr:
    image: ghcr.io/dorskfr/tidalidarr:latest
    environment:
      - TIDAL_DOWNLOAD_PATH=/downloads
      - TIDAL_TOKEN_PATH=token.json
      - LIDARR_API_URL=http://127.0.0.1:8686/api/v1
      - LIDARR_API_KEY=${LIDARR_API_KEY}
    volumes:
      - ./downloads:/downloads
      - ./token.json:/token.json
```

```bash
docker compose up
```

### Kubernetes

```yaml
# kustomization.yaml
namespace: <your namespace>
resources:
  - deployment.yaml
  - pvc.yaml
configMapGenerator:
  - literals:
      - LIDARR_API_URL=<API_URL>
      - LIDARR_API_KEY=<API_KEY>
      - LIDARR_DOWNLOAD_PATH=/downloads
      - TIDAL_DOWNLOAD_PATH=/downloads
      - TIDAL_TOKEN_PATH=/config/token.json
    name: tidalidarr
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
labels:
  - includeSelectors: true
    pairs:
      app.kubernetes.io/component: tidalidarr
      app.kubernetes.io/name: tidalidarr
---
# deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: tidalidarr
spec:
  template:
    spec:
      containers:
        - name: tidalidarr
          image: ghcr.io/dorskfr/tidalidarr:latest
          envFrom:
            - configMapRef:
                name: tidalidarr
          volumeMounts:
            - mountPath: /config
                name: tidalidarr-config-volume
            - mountPath: /downloads
              name: shared-download-volume
      volumes:
        - name: tidalidarr-config-volume
          persistentVolumeClaim:
            claimName: tidalidarr-config-volume
        - name: shared-download-volume
          persistentVolumeClaim:
            claimName: shared-download-volume
---
# pvc.yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: tidalidarr-config-volume
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 1Gi
  storageClassName: <your storage class>
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: shared-download-volume
spec:
  accessModes:
    - ReadWriteMany
  resources:
    requests:
      storage: 1Gi
  storageClassName: <your storage class>
```

## Alternatives

- https://github.com/RandomNinjaAtk/arr-scripts Ex lidarr-extended. Runs a setup bash script that loads other scripts dynamically.
- https://github.com/yaronzz/Tidal-Media-Downloader Utilized internally by the aforementioned scripts. It packages binaries as is and uses unconventional dependencies.
- https://github.com/exislow/tidal-dl-ng a cleaner and more modern approach to the above, still uses the same tidal api package.
- https://github.com/tamland/python-tidal the tidal api package internally used by the projects above, has [non straightforward code](https://github.com/tamland/python-tidal/blob/288fc1ea53d6ca0a23424795ecae3a09b0ec43a3/tidalapi/session.py#L141).
- https://github.com/ramok0/tidal-rs a clean library to interact with the tidal API, used as a reference in this project

## Why use this

- Simple, straightforward
- Does only one "thing": download your lidarr missing albums from tidal
- Made to run along side Lidarr

## Development

### Setup dependencies

```bash
git clone https://github.com/DorskFR/tidalidarr.git
make setup # assuming python 3.12 is installed
source .env # if using a .env
make run
```

### Docker (build)

To build the image, export the `REPOSITORY_URL` environment variable and:

```bash
make docker/build
make docker/push
make docker/run
```
