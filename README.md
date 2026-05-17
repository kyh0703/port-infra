필요 조건: Docker Compose 또는 Podman Compose

```bash
git clone --recurse-submodules git@github.com:kyh0703/dubu-infra.git
cd dubu-infra
```

submodule 없이 clone했다면:

```bash
git submodule update --init --recursive
```

실행:

```bash
docker compose up
```
