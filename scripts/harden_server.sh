#!/usr/bin/env bash
# Базовая защита сервера Ubuntu 22.04/24.04:
#   1) вход по SSH только по ключу (пароли отключаются, root — только по ключу);
#   2) fail2ban — блокирует адреса, которые подбирают пароль к SSH;
#   3) файрвол ufw — открыты только порты 22 (SSH), 80 и 443 (календарь);
#   4) автоматическая установка обновлений безопасности.
#
# Запускать от root ТОЛЬКО ПОСЛЕ того, как вы проверили вход на сервер по SSH-ключу,
# иначе после отключения паролей можно потерять доступ (останется только веб-консоль reg.ru).
#
#   bash /opt/guesthouse/scripts/harden_server.sh
set -euo pipefail

# Пути можно переопределить (используется в тестах)
AUTH_KEYS="${AUTH_KEYS:-/root/.ssh/authorized_keys}"
SSHD_DROPIN="${SSHD_DROPIN:-/etc/ssh/sshd_config.d/00-hardening.conf}"
F2B_JAIL="${F2B_JAIL:-/etc/fail2ban/jail.d/sshd-hardening.local}"
AUTO_UPGRADES="${AUTO_UPGRADES:-/etc/apt/apt.conf.d/20auto-upgrades}"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die() { printf '\n\033[31mОШИБКА: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "запустите от root"

say "1/5 Проверяю SSH-ключ"
if [ ! -s "$AUTH_KEYS" ] || ! grep -qE '^(ssh-ed25519|ssh-rsa|ecdsa-sha2-[a-z0-9-]+|sk-ssh-ed25519@openssh.com) ' "$AUTH_KEYS"; then
  die "в $AUTH_KEYS нет SSH-ключа. Сначала добавьте ключ и проверьте вход по нему (см. README, раздел «Защита сервера»)."
fi
echo "Ключей в $AUTH_KEYS: $(grep -cE '^(ssh-|ecdsa-|sk-)' "$AUTH_KEYS")"
if ! journalctl -u ssh -u sshd --since "-2 days" --no-pager 2>/dev/null | grep -q "Accepted publickey"; then
  echo "Не вижу в журнале ни одного входа по ключу за последние 2 дня."
  read -r -p "Вы точно проверили вход по SSH-ключу в отдельном окне? Напишите yes, чтобы продолжить: " answer
  [ "$answer" = "yes" ] || die "остановлено — сначала проверьте вход по ключу"
fi

say "2/5 Устанавливаю fail2ban, ufw и автообновления"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq fail2ban python3-systemd ufw unattended-upgrades >/dev/null

say "3/5 SSH: только вход по ключу"
mkdir -p "$(dirname "$SSHD_DROPIN")"
cat > "$SSHD_DROPIN" <<'CONF'
# Создано scripts/harden_server.sh. Файл с префиксом 00- читается первым и имеет приоритет
# над настройками облачного образа (например, 50-cloud-init.conf с PasswordAuthentication yes).
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
MaxAuthTries 4
LoginGraceTime 30
CONF
if ! sshd -t; then
  rm -f "$SSHD_DROPIN"
  die "проверка настроек SSH не прошла — изменения отменены"
fi
systemctl restart ssh.service 2>/dev/null || systemctl restart sshd.service
echo "Пароли для SSH отключены. Текущее подключение не разрывается."

say "4/5 fail2ban: блокировка подбора паролей"
mkdir -p "$(dirname "$F2B_JAIL")"
cat > "$F2B_JAIL" <<'CONF'
# Создано scripts/harden_server.sh
[sshd]
enabled  = true
backend  = systemd
maxretry = 5
findtime = 10m
bantime  = 1h
CONF
fail2ban-client -t >/dev/null || die "проверка настроек fail2ban не прошла"
systemctl enable --now fail2ban >/dev/null 2>&1
systemctl restart fail2ban

say "5/5 Файрвол и автообновления"
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow 22/tcp comment "SSH" >/dev/null
ufw allow 80/tcp comment "HTTP (Caddy)" >/dev/null
ufw allow 443/tcp comment "HTTPS (Caddy)" >/dev/null
ufw --force enable >/dev/null
# Порты контейнеров Docker публикует в обход ufw; у нас наружу открыт только Caddy (80, 443) — это и нужно.

cat > "$AUTO_UPGRADES" <<'CONF'
// Создано scripts/harden_server.sh: ежедневно обновлять списки пакетов и ставить обновления безопасности
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
CONF
systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true

say "Готово. Проверка:"
echo "SSH:      $(sshd -T 2>/dev/null | grep -E '^(passwordauthentication|permitrootlogin) ' | tr '\n' ' ')"
echo "fail2ban: $(fail2ban-client status sshd 2>/dev/null | grep -E 'Currently banned' | sed 's/^[^:]*:\s*/заблокировано адресов: /')"
echo "Файрвол:"
ufw status | sed 's/^/  /'
echo
echo "ВАЖНО: не закрывая это окно, откройте новое окно PowerShell и проверьте вход: ssh root@IP_СЕРВЕРА"
echo "Если войти не получится — исправить можно через веб-консоль в панели reg.ru."
