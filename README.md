# aarm-publicador

Publica automáticamente en el Instagram de **@aarm.arquitectos** lo que hay en Google Drive
(carpeta `POST-CARRUSELES`), sin apps de pago y sin que la PC esté encendida.

## Cómo funciona

1. En Drive cada semana tiene sus filas, y cada post es una carpeta con sus imágenes o su video y un `descripcion.txt`:
   `Semana 05 - … / Fila 1 / 1 - Mar 17 nov 2026 - … / (15-01.png, 15-02.png, …, descripcion.txt)`
2. El número y la fecha salen del nombre de la carpeta del post (`1 - Mar 17 nov 2026 - …`).
3. Todos los días a las 9:30 a. m. (Ciudad de México) GitHub revisa si hay posts con la fecha de hoy
   en las **semanas activas** (`config.json` → `semanas_activas`) y los publica en orden:
   el post 1 a las 10:00, el 2 a las 10:05 y el 3 a las 10:10. Si hay un cuarto, sale a las 10:15.
4. El 2 solo sale cuando Instagram confirma el 1, y así sucesivamente. Si uno falla, se detiene ahí
   para que nada salga fuera de orden, y GitHub manda un correo con el error.
5. Hay dos respaldos (10:35 y 12:35) por si la ejecución de las 9:30 no corrió. Nada se publica dos veces:
   lo publicado queda anotado en `estado/publicados.json`.

## Tipos de post

| Contenido de la carpeta | Se publica como |
|---|---|
| 1 imagen | Post de imagen |
| 1 video | Reel (si hay una imagen llamada `portada`, se usa de portada) |
| 2 a 10 archivos | Carrusel, en orden de nombre (`15-01`, `15-02`, …) |

Las imágenes se convierten a JPEG (lo único que acepta Instagram). Si Instagram no acepta la proporción 3:4,
se usa una versión 4:5 con franjas laterales del color de `config.json` → `color_franjas`;
en la cuadrícula del perfil esas franjas no se ven.

## Activar una semana

Agregar el inicio del nombre de la carpeta a `semanas_activas` en `config.json`, por ejemplo `"Semana 05"`.
Al guardar el cambio se ejecuta sola una **revisión** (pestaña *Actions*) que lista cada post con su
hora, tipo, número de archivos y largo del texto, sin publicar nada.

## Secretos (Settings → Secrets and variables → Actions)

| Nombre | Qué es |
|---|---|
| `IG_TOKEN` | Permiso de Instagram de la app de Meta. Si es de *Unified login* (usuario del sistema) no caduca; si es de *Instagram Login* (empieza con `IG`) se renueva solo. |
| `GOOGLE_SA_JSON` | Llave JSON de la cuenta de servicio de Google con acceso de lectura a la carpeta de Drive. |

Si el permiso es de *Instagram Login*, la versión renovada se guarda cifrada en `estado/token.enc`.
Con *Unified login* el publicador busca la cuenta sola; si no la encuentra, se pone su id en `config.json` → `ig_user_id`.
