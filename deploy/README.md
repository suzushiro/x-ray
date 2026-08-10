# Tumblr共有の外部公開（Cloudflare Tunnel）

`/share` と `/share-img` だけを外に出し、X-Ray本体はLAN内に留める構成。

## 手順

### 1. トンネルを作る

```bash
cloudflared tunnel login                    # ブラウザでドメインを認可
cloudflared tunnel create x-ray-share       # TUNNEL_ID が発行される
cloudflared tunnel route dns x-ray-share share.example.com
```

### 2. 設定ファイル

systemd で常駐させる場合、cloudflared は root で動くため設定は
`/etc/cloudflared/` に置く（`~/.cloudflared/` だと root から見えない）。

```bash
sudo mkdir -p /etc/cloudflared
sudo cp cloudflared-config.example.yml /etc/cloudflared/config.yml
sudo cp ~/.cloudflared/<TUNNEL_ID>.json /etc/cloudflared/
sudo vi /etc/cloudflared/config.yml   # TUNNEL_ID・ホスト名・credentials-file を自分のものに
```

検証（`--config` は `tunnel` の直後に置く。サブコマンドの後ろだとエラーになる）:

```bash
cloudflared tunnel --config /etc/cloudflared/config.yml ingress validate
```

### 3. X-Ray側の設定

`.env` に以下を設定して `docker compose up -d`:

```
PUBLIC_SHARE_BASE_URL=https://share.example.com
PUBLIC_SHARE_HOST=share.example.com
SHARE_TOKEN_TTL_MIN=60
```

`PUBLIC_SHARE_HOST` を設定すると、そのホスト名宛のリクエストは web 側でも
`/share` 系以外を404にする（cloudflaredの制限に加えた二層目）。

### 4. 常駐化（systemd）

```bash
sudo cloudflared service install
sudo systemctl enable --now cloudflared
sudo systemctl is-active cloudflared    # active
sudo systemctl is-enabled cloudflared   # enabled
```

設定が二重に残らないよう、ユーザー側の設定は退避しておく
（`cert.pem` はトンネル管理コマンドで使うので残す）:

```bash
mv ~/.cloudflared/config.yml ~/.cloudflared/config.yml.moved-to-etc
```

以降 ingress を変えたら `sudo systemctl restart cloudflared`。
ログは `sudo journalctl -u cloudflared -f`。

## 確認

```bash
# 共有系は通る（有効なトークンが必要）
curl -s https://share.example.com/share/<token> | grep og:image

# 本体は404（外から見えない）
curl -so /dev/null -w "%{http_code}\n" https://share.example.com/manage   # 404
```

トークンの発行はUIの共有ボタンから。CLIで試すなら:

```bash
curl -s -X POST http://localhost:8501/api/share/prepare -d "tweet_id=<ID>"
```

## ハマりどころ

- **path をまとめて書くとマッチしない**。`^/share(-img)?/.*` は動かず全部404になる。
  1ルール1パターンで分けること
- **`--config` の位置**。`cloudflared tunnel --config <path> ingress validate` の順。
  サブコマンドの後ろに置くと "flag provided but not defined" になる
- **systemd化すると設定は `/etc/cloudflared/`**。`~/.cloudflared/` のままだと root から読めない
- `cloudflared tunnel run` はフォアグラウンドで常駐する。ログが流れて止まって見えるのは正常

## 注意

- 公開されるのは `/share/<token>` と `/share-img/<token>/<n>` のみ。
  token を知らないと辿れず、`SHARE_TOKEN_TTL_MIN` 後に失効する
- スクレイパー（worker）や管理画面は絶対に ingress に含めないこと
- このトンネルは閲覧トラフィック用。スクレイプの出口分離（PROXY_URL）とは別レイヤー
- systemd 化すると常時公開になる。利用頻度が低いなら手動起動のままという選択もある
