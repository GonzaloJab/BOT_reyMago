# Carta de Reyes

WhatsApp (WAHA) más un bot que apunta la lista de regalos de la casa.

Todo se ejecuta desde esta carpeta:

```bash
cd /home/vader/ReyMago/waha
```

Si `docker` dice "permission denied", antepón `sg docker -c` al comando. Ejemplo: `sg docker -c 'docker compose ps'`.

## Relanzar el bot

El bot es el servicio que habla con el modelo y guarda las listas. No hace falta tocar WhatsApp. Reiniciarlo no vuelve a escribir a quien ya recibió un aviso: eso queda guardado y solo se repite cuando pasan los días de `NUDGE_EVERY_DAYS`.

```bash
docker compose up -d --no-deps --build bot
```

Comprobar que responde:

```bash
curl -sS http://127.0.0.1:8090/health
```

Ver qué está haciendo:

```bash
docker compose logs -f bot
```

Parar solo el bot: `docker compose stop bot`.

## Relanzar WhatsApp y el bot

```bash
docker compose up -d
```

La sesión de WhatsApp se conserva en `sessions/`. El panel de WAHA es http://10.12.2.135:3001/dashboard (usuario `waha`, contraseña en `.env`).

## Direcciones

| Qué | Dónde |
|---|---|
| Carta de regalos | http://10.12.2.135:8090 |

En cada regalo se puede anotar a mano el enlace, unas notas y si ya está comprado. Al modelo solo le llegan el nombre y las características.
| Datos de la carta | http://10.12.2.135:8090/api/wishes |
| Salud del bot | http://127.0.0.1:8090/health |
| Panel de WhatsApp | http://10.12.2.135:3001/dashboard |

Solo se ven desde esta red. No hace falta abrir puertos en el router.

## Qué se edita

`.env.example` y `members.example.json` son ejemplos. Hay que copiarlos y cambiarlos; el bot no usa esos dos archivos.

```bash
cp .env.example .env
cp members.example.json members.json
```

- `members.json`: nombres y teléfonos reales, en internacional y sin `+`. Quien no esté aquí no llega al modelo. El campo `context` es lo que el bot sabe de esa persona para recomendar. `"admin": true` deja que esa persona pregunte por las cartas de los demás, apunte regalos que ellas no ven y pida escribirle a alguien una instrucción. El mensaje no dice quién lo encargó. Se lee en cada mensaje, sin reiniciar. No se sube a git.
- `system-prompt.txt`: cómo habla y qué hace. Admite comillas y varias líneas. También se lee en cada mensaje.
- `.env`: clave de WAHA, datos del modelo y `NUDGE_EVERY_DAYS`. Los valores de `.env.example` no sirven. Un cambio aquí sí pide relanzar el bot. `0` apaga los recordatorios. No se sube a git.

El recordatorio, si la lista sigue vacía, sale cada 2 días entre las 9:00 y las 21:00 (hora de Madrid).

## Mensaje de presentación

Primero se muestra el texto y no se envía:

```bash
python3 presentacion.py
```

Cuando el texto esté bien:

```bash
python3 presentacion.py --send
```
