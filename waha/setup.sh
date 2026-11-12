#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  echo "Instalando Docker Engine y el plugin Compose..."
  sudo apt-get update
  sudo apt-get install -y docker.io docker-compose-v2
  sudo systemctl enable --now docker
  sudo usermod -aG docker "$USER"
fi

mkdir -p sessions media
chmod 600 .env

if docker info >/dev/null 2>&1; then
  docker compose up -d
else
  echo "El daemon responde, pero este usuario aún no está en el grupo docker."
  echo "Levantando el contenedor con sudo. En un login nuevo bastará 'docker compose up -d'."
  sudo docker compose up -d
fi

echo
echo "WAHA en la IP local: http://10.12.2.135:3001"
echo "Dashboard: http://10.12.2.135:3001/dashboard"
echo "Logs:      docker compose logs -f waha"
