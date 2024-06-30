# Tidalidarr

## tl;dr

Minimal standalone music downloader from Tidal syncing with Lidarr for macos, linux.

In its first version this program is opinionated:
- Only albums
- Only top hits
- Only FLAC

## Why use this

- Simple, straightforward
- Does only one "thing": download your lidarr missing albums from tidal
- Made to run along side Lidarr

## How to use

### Authenticating

On first run you need to authorize your device.
Credentials are then saved to `token.json` and refresh is automatic afterwards.

- On first run authentication with device will be attempted for 5 minutes
- In the logs you will see a link pointing to tidal and requiring authentication
- After the device is authorized, there is a ~30sc delay before authentication succeeds and the token is written to `token.json`

### Automatic download

Tidalidarr queries your lidarr instance for missing albums and starts searching / downloading

### Manually adding albums

Since the search functionality is so basic, I added an API endpoint to add albums manually at `/album/{album_id}`.

When browsing Tidal, replace the beginning of the URL `https://listen.tidal.com/album/1234` with your Tidalidarr URL and it should add the album to the download queue.

You can also use curl with a simple GET to add multiple albums. ex:

```bash
curl http://localhost:8000/album/1234
```

## Checking the queue

You can check the current state (queued, ready, not found) by calling your instance at `/queue` endpoint.

```bash
curl http://localhost:8000/queue
```

## Deployment

### Environment variables

The full settings are in the `BaseSettings` models.
The main variables that might require changes are:

| Variable                  | Required | Default                      | Description                                         |
| ------------------------- | -------- | ---------------------------- | --------------------------------------------------- |
| `LIDARR_API_KEY`          | `Yes`    |                              | Lidarr API key                                      |
| `LIDARR_API_URL`          | `Yes`    | http://127.0.0.1:8686/api/v1 | Lidarr API endpoint                                 |
| `TIDAL_DOWNLOAD_PATH`     | `No`     | /downloads                   | Persistent storage to save music files              |
| `TIDAL_TOKEN_PATH`        | `No`     | token.json                   | Persistent storage to save the authentication token |
| `TIDALIDARR_UVICORN_PORT` | `No`     | 8000                         | Port on which uvicorn should bind                   |
| `LOG_LEVEL`               | `No`     |                              | Python log levels: DEBUG, INFO, WARNING, ERROR      |

### Docker

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

### Docker compose

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
      - ./token.json:/token.json # needs write permission
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
            - mountPath: /config # needs write permission
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

## Disclaimer

Use responsibly and solely for private purposes. A Tidal subscription is required. Redistribution or piracy of music is prohibited. `Tidal` is a trademark of its respective owner. For terms, see https://tidal.com/terms.
