# Montarlo en un servidor gratis

Hace falta una máquina que no se duerma. WhatsApp y los avisos cada dos días se caen si el proceso se apaga a los quince minutos. Por eso no vale un plan gratuito de Render, Railway o Fly: esos se duermen.

La opción gratis que se queda encendida es **Oracle Cloud Always Free**, una máquina Ampere (ARM) con Ubuntu. El alta pide tarjeta, pero la forma Always Free no cobra si no sales de ese plan. En la región `eu-madrid-1` a veces no hay capacidad; si falla, prueba `eu-marseille-1` o `eu-frankfurt-1`. Pide al menos 2 OCPU y 12 GB de RAM: el contenedor de WhatsApp reserva 2 GB de memoria compartida.

La carta de regalos no tiene contraseña. No publiques los puertos 8090 ni 3001 en internet. Llega a ellos con **Tailscale**, que también es gratis para un uso personal: el servidor y tu ordenador quedan en una red privada, sin abrir el router.

## 1. Crear la máquina

1. Entra en [cloud.oracle.com](https://cloud.oracle.com) y crea una cuenta Always Free.
2. Compute → Instances → Create. Imagen Ubuntu 24.04 aarch64. Forma `VM.Standard.A1.Flex`, 2 OCPU, 12 GB.
3. Guarda la clave SSH que te descarga. Anota la IP pública solo para la primera conexión.
4. En el security list de la VCN deja el SSH (22) limitado a tu IP. No abras 3001 ni 8090.

La imagen de Ubuntu en Oracle trae un cortafuegos propio. Para Tailscale no hace falta abrir más puertos: sale él hacia fuera.

## 2. Docker y el código

```bash
ssh ubuntu@IP_PUBLICA
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2 git
sudo usermod -aG docker "$USER"
```

Cierra la sesión y vuelve a entrar, para que el grupo `docker` valga.

Clona el repositorio en el servidor (o copia la carpeta `waha/` con `scp` si el repo sigue solo en tu máquina). Entra en `waha/`.

```bash
cp .env.example .env
cp members.example.json members.json
chmod 600 .env
```

`.env.example` y `members.example.json` son ejemplos. Cámbialos después de copiarlos. El bot lee `.env` y `members.json`, no los ejemplos.

En `.env` pon una `WAHA_API_KEY` nueva, las contraseñas del panel, `LLM_BASE_URL`, `LLM_API_KEY` y `LLM_MODEL`. No reutilices un `.env` pegado en un chat.

En `members.json` pon los nombres y teléfonos reales, en internacional y sin `+`. El de ejemplo, con teléfonos `34600000001` y siguientes, no escribe a nadie. `system-prompt.txt` también se edita ahí. Esos dos se leen en cada mensaje.

## 3. Tailscale

En el servidor y en tu ordenador:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Anota la IP `100.x.x.x` del servidor (`tailscale ip -4`). En `.env` del servidor:

```bash
HOST_BIND=100.x.x.x
PUBLIC_HOST=100.x.x.x
```

`HOST_BIND` no puede ser `0.0.0.0`: el compose ya escucha también en `127.0.0.1` y los dos a la vez chocan. Con la IP de Tailscale, el panel solo se abre desde tus aparatos unidos a esa red.

## 4. Arrancar y enlazar WhatsApp

```bash
docker compose up -d --build
docker compose logs -f waha
```

Abre, desde un aparato con Tailscale, `http://100.x.x.x:3001/dashboard`. Usuario y contraseña son los de `.env`. Escanea el QR con el teléfono de la casa. La sesión queda en `sessions/`; no la borres y no la subas a git.

La carta: `http://100.x.x.x:8090`.

Comprueba el bot por SSH en el propio servidor:

```bash
curl -sS http://127.0.0.1:8090/health
```

## 5. Qué no se pierde al reiniciar

`restart: unless-stopped` levanta los contenedores si la máquina reboota. Reiniciar el bot no reenvía los avisos: la fecha del último aviso está en `bot-data/`.

Para cambiar solo el bot después de un `git pull`:

```bash
docker compose up -d --no-deps --build bot
```

Un cambio de `.env` sí pide recrear el contenedor: `docker compose up -d --force-recreate --no-deps bot`.

## Si Oracle no tiene capacidad

Espera o cambia de región. No bajes a una máquina de 1 GB: con la reserva de memoria de WhatsApp no arranca. Una VPS de pago pequeña (Hetzner CX22, unos 4 € al mes) vale si Always Free sigue sin sitio, con los mismos pasos de Docker y Tailscale.
