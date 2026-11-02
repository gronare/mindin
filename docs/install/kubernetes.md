# Install on Kubernetes

`deploy/k8s/` in the repository holds plain manifests: a Deployment with a PersistentVolumeClaim mounted at `/data`, a Service on port 8765, an Ingress, and an example Secret with the settings. The Secret's example keeps the vaults in an S3 bucket; see [Storage](storage.md).

1. Copy the example Secret and fill it in. Make the master key with `notes-vault-mcp master-key generate`, or with `docker run --rm ghcr.io/gronare/notes-vault-mcp:latest master-key generate`.

   ```sh
   cp deploy/k8s/secret.example.yaml secret.yaml
   $EDITOR secret.yaml
   ```

   It needs `VAULT_PUBLIC_URL`, `VAULT_HOSTED_MASTER_KEY`, a sign-in method and the storage. [Configuration](../reference/configuration.md) lists every setting.

2. In `deploy/k8s/ingress.yaml`, set `ingressClassName`, the host, and `secretName` to a TLS certificate for that host.

3. Apply the manifests and invite yourself:

   ```sh
   kubectl apply -f secret.yaml -f deploy/k8s/deployment.yaml -f deploy/k8s/service.yaml -f deploy/k8s/ingress.yaml
   kubectl exec deploy/notes-vault-mcp -- notes-vault-mcp accounts invite you@example.com
   ```

4. Open `VAULT_PUBLIC_URL/account` and sign in.

The Deployment runs one replica with the `Recreate` strategy, as a sealed vault needs; see [One server per bucket](storage.md#one-server-per-bucket). The probes hit `/.well-known/oauth-protected-resource/mcp`, which answers without a token; when `VAULT_PUBLIC_URL` carries a path, that path goes in the probe URL too. The server also answers `GET /up` with `ok` while the process serves, and `GET /ready` with `200` when the account store answers, the storage is reachable (`account_store` and `storage`, each looked at once every 5 seconds) and every readiness check an installed extension adds passes, or `503` and a JSON body naming the checks that failed, under the same path. The container's memory limit is 512 MiB, which is enough without search by meaning; [Search by meaning](semantic-search.md#kubernetes) needs more.

The image sets `VAULT_AUTH_DIR=/data/auth` and `VAULT_CACHE_DIR=/data/cache`. Back up `/data/auth` together with the master key; see [Back up and rotate keys](../how-to/back-up-and-rotate.md).

An Ingress controller that rewrites the `Host` header needs [`VAULT_ALLOWED_HOSTS`](../how-to/reverse-proxy.md).
