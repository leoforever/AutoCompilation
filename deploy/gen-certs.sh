#!/usr/bin/env bash
# compile-platform 证书两段式（HTTPS 方案 §二-2；setup.sh install 复用本脚本）
# CA 存在即复用（叶子重签不影响已装 CA 的客户端）；叶子 SAN 含全部入口 IP。
set -euo pipefail
SSL_DIR=/etc/nginx/ssl
CA_KEY="$SSL_DIR/platform-ca.key"; CA_CRT="$SSL_DIR/platform-ca.crt"
CRT="$SSL_DIR/compile-platform.crt"; KEY="$SSL_DIR/compile-platform.key"
D=$(date +%Y%m%d)

[ -d "$SSL_DIR" ] || sudo mkdir -p "$SSL_DIR"

if [ ! -f "$CA_CRT" ]; then
    echo "[cert] 生成自签 CA（一次生成永久不动）"
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
        -keyout "$CA_KEY" -out "$CA_CRT" \
        -subj "/CN=compile-platform-ca" 2>/dev/null
else
    echo "[cert] CA 已存在，复用：$CA_CRT"
fi

# SAN 动态：机器当前 IP 全收（172.18/10.88 容器网段除外）+ localhost + 127.0.0.1
# （首切实测教训：只带 DNS:localhost 时 curl --cacert https://127.0.0.1 校验失败
# ——域名验证按 IP 字面走，DNS 名不救 IP 口）；每次 install 按现 IP 重签叶子自愈
SANS="DNS:localhost,IP:127.0.0.1"
for ip in $(hostname -I | tr ' ' '\n' | grep -vE '^(172\.18\.|10\.88\.)' | grep -vE '^$'); do
    SANS="$SANS,IP:$ip"
done
echo "[cert] 叶子 SAN: $SANS"

openssl req -newkey rsa:2048 -nodes -keyout "$KEY" -out /tmp/cp-leaf.csr \
    -subj "/CN=compile-platform" 2>/dev/null
printf "subjectAltName=%s\nbasicConstraints=critical,CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n" "$SANS" > /tmp/cp-san.ext
openssl x509 -req -in /tmp/cp-leaf.csr -CA "$CA_CRT" -CAkey "$CA_KEY" -CAcreateserial \
    -out "$CRT" -days 825 -extfile /tmp/cp-san.ext 2>/dev/null
rm -f /tmp/cp-leaf.csr /tmp/cp-san.ext

sudo chmod 600 "$CA_KEY" "$KEY"; sudo chmod 644 "$CA_CRT" "$CRT"
echo "[cert] OK: $CRT ($D)"
openssl x509 -in "$CRT" -noout -subject -issuer -enddate -ext subjectAltName | head -5
