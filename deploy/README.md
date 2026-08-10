# Tumblr共有の外部公開（Cloudflare Tunnel）

`/share` と `/share-img` だけを外に出し、X-Ray本体はLAN内に留める構成。

## 手順

### 1. トンネルを作る

```bash
cloudflared tunnel login                    # ブラウザでドメインを認可
cloudflared tunnel create x-ray-share       # TUNNEL_ID が発行される
```

### 2. DNSを向ける

```bash
cloudflared tunnel route dns x-ray-share share.example.com
```

### 3. 設定ファイル

`cloudflared-config.example.yml` をコピーして `<TUNNEL_ID>` と
`credentials-file` のパス、`share.example.com` を自分のものに書き換える。

```bash
cp cloudflared-config.example.yml ~/.cloudflared/config.yml
vi ~/.cloudflared/config.yml
```

### 4. X-Ray側の設定

`.env` に以下を設定:

```
PUBLIC_SHARE_BASE_URL=https://share.example.com
PUBLIC_SHARE_HOST=share.example.com
SHARE_TOKEN_TTL_MIN=60
```

`PUBLIC_SHARE_HOST` を設定すると、そのホスト名宛のリクエストは
web側でも `/share` 系以外を404にする（cloudflaredの制限に加えた二層目）。

```bash
cd ~/x-ray && docker compose up -d
```

### 5. 起動

```bash
cloudflared tunnel run x-ray-share
```

常駐させるなら systemd サービスに:

```bash
sudo cloudflared service install
```

## 確認

```bash
# 共有系は通る（有効なトークンが必要）
curl -I https://share.example.com/share/<token>

# 本体は 404（外から見えない）
curl -I https://share.example.com/manage      # → 404
curl -I https://share.example.com/            # → 404
```

## 注意

- 公開されるのは `/share/<token>` と `/share-img/<token>/<n>` のみ。
  token を知らないと辿れず、`SHARE_TOKEN_TTL_MIN` 後に失効する
- スクレイパー（worker）や管理画面は絶対に ingress に含めないこと
- このトンネルは閲覧トラフィック用。スクレイプの出口分離（PROXY_URL）とは別レイヤー
